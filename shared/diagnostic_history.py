"""Приватні атомарні знімки сесій діагностики, без доступу до робочої SQLite."""

from datetime import datetime, timezone
import errno
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from shared.paths import place, root_of

DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_SESSIONS = 100
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 2_000_000
SESSION_RE = re.compile(r"[0-9a-f]{32}\Z")
FILE_RE = re.compile(r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{32}\.json\Z")
FIELDS = frozenset({
    "schema", "session", "started_at", "updated_at", "session_status", "ended_at",
    "counters", "gauges", "timings", "asr_latency", "notification_spectral",
})


def history_directory(latest_path):
    return place(root_of(latest_path), ".local-diagnostics")


def _timestamp(value):
    return type(value) in (float, int) and math.isfinite(value) and 0 <= value < 253402300799


def checked_snapshot(data):
    if (not isinstance(data, dict) or data.get("schema") != 1
            or not isinstance(data.get("session"), str) or not SESSION_RE.fullmatch(data["session"])
            or not _timestamp(data.get("started_at")) or not _timestamp(data.get("updated_at"))):
        raise ValueError("Непридатний знімок сесії")
    if "ended_at" in data and not _timestamp(data["ended_at"]):
        raise ValueError("Некоректний час завершення сесії")
    result = {key: value for key, value in data.items() if key in FIELDS}
    for key in ("counters", "gauges", "timings", "asr_latency", "notification_spectral"):
        if key in result and not isinstance(result[key], dict):
            raise ValueError("Некоректна структура метрик")
    if result.get("session_status") not in ("running", "finished"):
        result.pop("session_status", None)
    return result


def snapshot_filename(data):
    stamp = datetime.fromtimestamp(data["started_at"], timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{data['session']}.json"


def read_snapshot(path):
    # Не читаємо посилання, пристрої або необмежений файл під виглядом JSON.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SNAPSHOT_BYTES:
            raise ValueError("Непридатний або завеликий знімок")
        content = source.read(MAX_SNAPSHOT_BYTES + 1)
    if len(content) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Знімок завеликий")
    return checked_snapshot(json.loads(content))


def atomic_write(path, data):
    """Старий валідний файл лишається до завершення запису нового; права 0600."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".radio-diagnostics-", delete=False) as output:
            temporary = output.name
            json.dump(data, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class DiagnosticsHistory:
    def __init__(self, directory, *, retention_days=DEFAULT_RETENTION_DAYS,
                 max_sessions=DEFAULT_MAX_SESSIONS, max_bytes=DEFAULT_MAX_BYTES):
        for value in (retention_days, max_sessions, max_bytes):
            if type(value) is not int or value < 1:
                raise ValueError("Ліміти історії мають бути додатними цілими числами")
        self.directory = Path(directory)
        self.retention_days = retention_days
        self.max_sessions = max_sessions
        self.max_bytes = max_bytes

    def _check_directory(self, *, create=False):
        if self.directory.is_symlink():
            raise OSError(errno.ELOOP, "Каталог історії не може бути посиланням")
        if create:
            self.directory.mkdir(mode=0o700, exist_ok=True)
            self.directory.chmod(0o700)
        elif not self.directory.exists():
            return False
        if not self.directory.is_dir():
            raise OSError(errno.ENOTDIR, "Каталог історії недоступний")
        return True

    def _files(self):
        if not self._check_directory():
            return []
        result = []
        for path in self.directory.iterdir():
            if not FILE_RE.fullmatch(path.name):
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                result.append((path, info))
        return result

    def save(self, data, *, preserve_newer=False):
        data = checked_snapshot(data)
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > min(MAX_SNAPSHOT_BYTES, self.max_bytes):
            raise ValueError("Знімок перевищує ліміт історії")
        self._check_directory(create=True)
        path = self.directory / snapshot_filename(data)
        if preserve_newer:
            try:
                existing = read_snapshot(path)
            except (FileNotFoundError, ValueError):
                existing = None
            if existing and existing["updated_at"] >= data["updated_at"]:
                return path
        atomic_write(path, data)
        self.prune(protected=path)
        return path

    def preserve_latest(self, latest_path, current_session):
        try:
            previous = read_snapshot(latest_path)
        except (FileNotFoundError, ValueError, UnicodeError):
            # Відсутній/зіпсований знімок не заважає новому запуску.
            return
        if previous["session"] != current_session:
            self.save(previous, preserve_newer=True)

    def prune(self, *, protected=None, now=None):
        now = time.time() if now is None else now
        files = sorted(self._files(), key=lambda item: (item[1].st_mtime, item[0].name), reverse=True)
        if protected is not None:
            files.sort(key=lambda item: item[0] != protected)
        count = size = 0
        for path, info in files:
            keep = (info.st_mtime >= now - self.retention_days * 86400
                    and count < self.max_sessions and size + info.st_size <= self.max_bytes)
            if path == protected or keep:
                count += 1
                size += info.st_size
            else:
                path.unlink(missing_ok=True)
        # SIGKILL міг залишити незавершений тимчасовий файл. Свіжі не чіпаємо.
        for path in self.directory.glob(".radio-diagnostics-*"):
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_mtime < now - 3600:
                path.unlink(missing_ok=True)

    def list_sessions(self, *, limit=100):
        if type(limit) is not int or limit < 1:
            raise ValueError("Некоректний ліміт сесій")
        items = []
        for path, info in sorted(self._files(), key=lambda item: item[0].name, reverse=True):
            try:
                data = read_snapshot(path)
            except (OSError, ValueError, UnicodeError):
                continue
            if snapshot_filename(data) != path.name:
                continue
            items.append({"session": data["session"], "started_at": data["started_at"],
                          "updated_at": data["updated_at"], "ended_at": data.get("ended_at"),
                          "session_status": data.get("session_status", "unknown"), "bytes": info.st_size,
                          "measured_segments": (data.get("counters", {}).get("asr.measured_segments", 0)
                                                if type(data.get("counters", {}).get("asr.measured_segments", 0)) is int else 0)})
            if len(items) >= limit:
                break
        return {"items": items, "retention_days": self.retention_days,
                "max_sessions": self.max_sessions, "max_bytes": self.max_bytes}

    def load(self, session):
        if not SESSION_RE.fullmatch(session):
            raise ValueError("Некоректний ідентифікатор сесії")
        for path, _ in self._files():
            if not path.name.endswith(f"-{session}.json"):
                continue
            data = read_snapshot(path)
            if data["session"] == session and snapshot_filename(data) == path.name:
                return data
        raise FileNotFoundError("Сесію не знайдено")
