"""Відібрані фрази від розпізнавання до ефіру: файл = фраза, ефір не відкриває базу.

Пише служба розпізнавання (єдиний власник бази), читає вузол ефіру. Номер фрази й сеанс живуть
у файлах, тож перезапуск будь-якої зі служб не повторює фрази й сповіщення на телефоні.
"""
import json
import logging
import os
import struct
import threading
import time
import uuid
from pathlib import Path

from services.node_control import read_json, write_json

KEEP = 30   # як HISTORY у radio_stream
MODES = ("all", "watch", "commands")


def feed_dir(node_dir):
    return Path(node_dir) / "phrases"


def _entries(folder):
    """[(номер, шлях)] за зростанням; сторонні файли пропускаються."""
    found = []
    for path in Path(folder).glob("*.phr"):
        head = path.name.split("-", 1)[0]
        if head.isdigit():
            found.append((int(head), path))
    return sorted(found)


def _session(folder):
    value = (read_json(Path(folder) / "session.json") or {}).get("session")
    return value if isinstance(value, str) and value else None


class PhraseOutbox:
    """Бік розпізнавання; той самий інтерфейс, що RadioStream для EventProcessor."""

    def __init__(self, folder, *, mode, sample_rate, current=lambda: (None, None), keep=KEEP, clock=time.time):
        if mode not in MODES:
            raise ValueError(f"Невідомий режим потоку: {mode}")
        self.folder, self.mode, self.sample_rate = Path(folder), mode, sample_rate
        self.current, self.keep, self.clock = current, keep, clock   # current() → (record_id, час кінця)
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        for leftover in self.folder.glob("*.tmp"):
            leftover.unlink(missing_ok=True)
        if _session(self.folder) is None:
            write_json(self.folder / "session.json", {"session": uuid.uuid4().hex[:12]})

    def set_signal_matcher(self, matcher, mode_getter):
        """Фільтр «наживо» вузол має власний."""

    def wants(self, wants):
        return bool(wants(self.mode))

    def publish(self, pcm, caption):
        record_id, ended = self.current()
        entries = _entries(self.folder)
        if record_id and any(path.stem.split("-", 1)[1] == record_id for _, path in entries):
            return None   # збій між публікацією й архівом: фразу вже віддано
        number = entries[-1][0] + 1 if entries else 1
        head = json.dumps({"at": ended or self.clock(), "caption": caption, "sample_rate": self.sample_rate}).encode()
        name = f"{number:010d}-{record_id or uuid.uuid4().hex}"
        temp = self.folder / (name + ".tmp")
        with open(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
            out.write(struct.pack("<I", len(head)) + head + bytes(pcm))
            out.flush()
            os.fsync(out.fileno())
        os.rename(temp, self.folder / (name + ".phr"))
        fd = os.open(self.folder, os.O_RDONLY)
        try:
            os.fsync(fd)   # номер не має повторитися після вимкнення живлення
        finally:
            os.close(fd)
        for _, path in _entries(self.folder)[:-self.keep]:
            path.unlink(missing_ok=True)
        return number


class PhraseInbox:
    """Бік ефіру: лише читає. Наявне при запуску йде в список без звуку, нове — звучить."""

    def __init__(self, folder, stream, log=None):
        self.folder, self.stream, self.log = Path(folder), stream, log or logging.getLogger("radio.node")
        self._session, self._last, self._primed = None, 0, False

    @staticmethod
    def _read(path):
        raw = path.read_bytes()
        try:
            size = struct.unpack_from("<I", raw)[0]
            head = json.loads(raw[4:4 + size])
            at, caption, rate = float(head["at"]), head["caption"], int(head["sample_rate"])
        except (struct.error, KeyError, TypeError, ValueError) as exc:
            raise ValueError("Пошкоджена фраза") from exc
        pcm = raw[4 + size:]
        if not pcm or len(pcm) % 2 or not isinstance(caption, str):
            raise ValueError("Пошкоджена фраза")
        return pcm, caption, at, rate

    def step(self):
        session = _session(self.folder)
        if session is None:
            self._primed = True   # служба ще не стартувала: усе, що з'явиться, — нове
            return
        if session != self._session:
            self.stream.reset_history(session)
            self._session, self._last = session, 0
        for number, path in _entries(self.folder):
            if number <= self._last:
                continue
            try:
                pcm, caption, at, rate = self._read(path)
                if rate != self.stream.sample_rate:
                    raise ValueError("Інша частота фрази")
            except FileNotFoundError:
                continue   # щойно прибрано як найстарішу
            except ValueError:
                self.log.warning("Фразу для телефону пропущено: файл пошкоджено")
                self._last = number
                continue
            self.stream.publish(pcm, caption, clip_id=number, at=at, play=self._primed)
            self._last = number
        self._primed = True

    def run(self, stop, interval=0.3):
        failing = False
        while not stop.wait(interval):
            try:
                self.step()
                failing = False
            except Exception:
                if not failing:
                    self.log.exception("Фрази для телефону тимчасово недоступні; повторюю")
                failing = True

    def start(self, stop):
        try:
            self.step()   # список готовий до першого запиту телефона
        except Exception:
            self.log.exception("Збережені фрази не прочитано")
        thread = threading.Thread(target=self.run, args=(stop,), name="phrase-inbox", daemon=True)
        thread.start()
        return thread
