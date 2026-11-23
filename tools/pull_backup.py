"""Бекап баз Pi на Mac: знімок через SQLite Backup API на Pi → перевірка тут → .local-backups/pi/.

    .venv/bin/python -m tools.pull_backup                       # jackrv@radio345 задає RADIO345_PI_SSH або --host
    .venv/bin/python -m tools.pull_backup --host pi@radio345 --root radio345-hub

Pi нічого не встановлює: скрипт іде через ssh у стандартний python3. Живу базу не зачіпає,
тимчасові знімки на Pi прибирає. Аудіо не копіює.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEEP = 14
NAMES = ("events.db", "index.sqlite3")
STAMP = re.compile(r"\d{8}T\d{6}Z")
MAX_BYTES = 2 * 1024 ** 3

REMOTE = r'''
import hashlib, io, json, os, shutil, sqlite3, sys, tarfile, tempfile
root = os.path.expanduser(sys.argv[1] if os.path.isabs(sys.argv[1]) else "~/" + sys.argv[1])
work = tempfile.mkdtemp(prefix="r345-backup-", dir=root)
os.chmod(work, 0o700)
try:
    info = {}
    for name, source in (("events.db", "events.db"), ("index.sqlite3", ".local-recordings/index.sqlite3")):
        path = os.path.join(root, source)
        if not os.path.isfile(path):
            if name == "events.db":
                sys.exit("events.db not found")
            continue
        a = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)
        target = os.path.join(work, name)
        b = sqlite3.connect(target)
        a.backup(b)
        b.execute("PRAGMA journal_mode=DELETE")
        if b.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            sys.exit("integrity " + name)
        b.close(); a.close()
        digest = hashlib.sha256()
        with open(target, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                digest.update(block)
        info[name] = {"sha256": digest.hexdigest(), "bytes": os.path.getsize(target)}
    out = tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz")
    data = json.dumps(info).encode()
    item = tarfile.TarInfo("manifest.json"); item.size = len(data)
    out.addfile(item, io.BytesIO(data))
    for name in info:
        out.add(os.path.join(work, name), arcname=name)
    out.close()
finally:
    shutil.rmtree(work, ignore_errors=True)
'''


def ssh_command(host, root):
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "--", host,
            "python3 -I - " + shlex_quote(root)]


def shlex_quote(value):
    import shlex
    return shlex.quote(value)


def unpack(stream, folder):
    """Лише очікувані звичайні файли; розмір обмежено."""
    total, info = 0, None
    with tarfile.open(fileobj=stream, mode="r|gz") as tar:
        for item in tar:
            if not item.isfile() or item.name not in (*NAMES, "manifest.json"):
                raise ValueError("Неочікуваний файл у бекапі")
            total += item.size
            if total > MAX_BYTES:
                raise ValueError("Бекап завеликий")
            source = tar.extractfile(item)
            if item.name == "manifest.json":
                info = json.loads(source.read(1 << 20))
                continue
            fd = os.open(folder / item.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(source, out, 1 << 20)
    if not isinstance(info, dict) or "events.db" not in info or not set(info) <= set(NAMES):
        raise ValueError("Неповний бекап")
    return info


def verify(folder, info):
    for name, expected in info.items():
        digest = hashlib.sha256()
        with open(folder / name, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                digest.update(block)
        if digest.hexdigest() != expected["sha256"]:
            raise ValueError(f"{name}: контрольна сума не збігається")
        db = sqlite3.connect(f"file:{folder / name}?mode=ro", uri=True)
        try:
            if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError(f"{name}: пошкоджено")
        finally:
            db.close()


def prune(folder, keep=KEEP):
    done = sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink() and STAMP.fullmatch(p.name))
    for path in done[:-keep]:
        for name in NAMES:
            (path / name).unlink(missing_ok=True)
        try:
            path.rmdir()
        except OSError:
            pass   # у каталозі є щось чуже — не чіпаємо


def pull(command, folder, *, keep=KEEP, now=None):
    folder = Path(folder)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(folder / ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Бекап уже виконується") from None
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
        final = folder / stamp
        if final.exists():
            raise RuntimeError("Бекап із такою міткою вже є")
        work = Path(tempfile.mkdtemp(prefix=".partial-", dir=folder))
        try:
            with tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
                try:
                    process.stdin.write(REMOTE.encode())
                    process.stdin.close()
                    info = unpack(process.stdout, work)
                    process.stdout.read()
                    code = process.wait(60)
                except BaseException:
                    process.kill()
                    process.wait()
                    errors.seek(0)
                    detail = errors.read(400).decode("utf-8", "replace").strip()
                    if detail:
                        print(detail, file=sys.stderr)
                    raise
                if code:
                    raise RuntimeError(f"Pi повернула код {code}")
            verify(work, info)
            os.rename(work, final)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        prune(folder, keep)
    return final


def main(argv=None):
    parser = argparse.ArgumentParser(description="Бекап баз Pi на Mac")
    parser.add_argument("--host", default=os.environ.get("RADIO345_PI_SSH", "radio345"))
    parser.add_argument("--root", default="radio345-hub", help="каталог проєкту на Pi (від домашнього)")
    parser.add_argument("--out", type=Path, default=ROOT / ".local-backups" / "pi")
    args = parser.parse_args(argv)
    if args.host.startswith("-") or not re.fullmatch(r"[\w.@-]+", args.host):
        raise SystemExit("Некоректна адреса Pi")
    try:
        path = pull(ssh_command(args.host, args.root), args.out)
    except (OSError, ValueError, RuntimeError, tarfile.TarError, sqlite3.Error, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Бекап не зроблено: {type(exc).__name__}: {exc}") from None
    print(f"{path}: {', '.join(f'{p.name} {p.stat().st_size // 1024} КБ' for p in sorted(path.iterdir()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
