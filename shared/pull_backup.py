"""Бекап баз Pi на Mac: знімок через SQLite Backup API на Pi → перевірка тут → ~/Library/Application Support/radio345.

    .venv/bin/python -m tools.pull_backup                       # jackrv@radio345 задає RADIO345_PI_SSH або --host
    .venv/bin/python -m tools.pull_backup --host pi@radio345 --root radio345-hub
    … -m tools.pull_backup --host jackrv@radio345 --install-agent   # щодня (спроба щогодини, доки не вийде)
    … -m tools.pull_backup --status | --remove-agent

Агент працює з копією цього файла поза ~/Documents: туди macOS фонові процеси не пускає.
Тому тут лише стандартна бібліотека. Після зміни скрипта повторіть --install-agent.

Pi нічого не встановлює: скрипт іде через ssh у стандартний python3. Живу базу не зачіпає,
тимчасові знімки на Pi прибирає. Аудіо не копіює.
"""
import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home() / "Library" / "Application Support" / "radio345"
OUT = HOME / "pi-backups"
LABEL = "local.radio345.pi-backup"
EVERY_S, DUE_H, STALE_DAYS = 3600, 20, 3
KEEP = 3
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
    for name, source in (("events.db", next((p for p in (".local-data/events.db", "events.db") if os.path.isfile(os.path.join(root, p))), ".local-data/events.db")), ("index.sqlite3", next((p for p in (".local-data/recordings/index.sqlite3", ".local-recordings/index.sqlite3") if os.path.isfile(os.path.join(root, p))), ".local-recordings/index.sqlite3"))):
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
        for stale in folder.glob(".partial-*"):   # вимкнення живлення посеред копії; замок наш
            if stale.is_dir() and not stale.is_symlink():
                shutil.rmtree(stale, ignore_errors=True)
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


def latest(folder):
    """(каталог, вік у годинах) останньої цілої копії або (None, None)."""
    folder = Path(folder)
    done = sorted(p for p in folder.iterdir() if p.is_dir() and STAMP.fullmatch(p.name)) if folder.is_dir() else []
    if not done:
        return None, None
    at = datetime.strptime(done[-1].name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return done[-1], (datetime.now(timezone.utc) - at).total_seconds() / 3600


def warn_stale(folder, notify=None, now=time.time):
    """Не частіше разу на добу, якщо давно немає копії. Без вмісту помилки й адрес."""
    folder = Path(folder)
    _, age = latest(folder)
    started, warned = folder / ".installed", folder / ".warned"
    if age is None:
        age = (now() - started.stat().st_mtime) / 3600 if started.exists() else 0
    if age < STALE_DAYS * 24 or (warned.exists() and now() - warned.stat().st_mtime < 86400):
        return False
    warned.touch()
    (notify or mac_notify)(f"Немає свіжої копії баз Pi: {int(age // 24)} дн.")
    return True


def mac_notify(text):
    subprocess.run(["/usr/bin/osascript", "-e", "on run a", "-e", 'display notification (item 1 of a) with title "Radio 345"',
                    "-e", "end run", text], check=False, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stable_python():
    """Не .venv (лежить у ~/Documents) і не Cellar із версією (зникне після brew upgrade)."""
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for path in (f"/opt/homebrew/bin/{version}", f"/usr/local/bin/{version}", "/opt/homebrew/bin/python3"):
        if os.access(path, os.X_OK):
            return path
    return os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)


def agent_plist(host, root, out, script, log, label=LABEL):
    python = stable_python()
    return {"Label": label, "RunAtLoad": True, "StartInterval": EVERY_S, "ProcessType": "Background", "Umask": 0o077,
            "ProgramArguments": [python, "-I", str(script), "--host", host, "--root", root, "--out", str(out), "--scheduled"],
            "WorkingDirectory": str(Path(script).parent), "StandardOutPath": str(log), "StandardErrorPath": str(log),
            "EnvironmentVariables": {"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"}}


def agent_paths(label=LABEL):
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist", HOME / "pull_backup.py", HOME / "pi-backup.log"


def launchctl(*args, check=False):
    return subprocess.run(["/bin/launchctl", *args], check=check, capture_output=True, timeout=30)


def install_agent(host, root, out, label=LABEL):
    plist, script, log = agent_paths(label)
    for folder in (HOME, Path(out)):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    plist.parent.mkdir(parents=True, exist_ok=True)
    if Path(__file__).resolve() != script.resolve():
        shutil.copyfile(__file__, script)
    script.chmod(0o600)
    (Path(out) / ".installed").touch()
    plist.write_bytes(plistlib.dumps(agent_plist(host, root, out, script, log, label)))
    launchctl("bootout", f"gui/{os.getuid()}/{label}")
    launchctl("bootstrap", f"gui/{os.getuid()}", str(plist), check=True)


def remove_agent(label=LABEL):
    launchctl("bootout", f"gui/{os.getuid()}/{label}")
    agent_paths(label)[0].unlink(missing_ok=True)


def overview(out=OUT):
    """Для панелі: чи ввімкнено, скільки копій і місця. Нічого не створює."""
    out = Path(out)
    copies = [p for p in out.iterdir() if p.is_dir() and STAMP.fullmatch(p.name)] if out.is_dir() else []
    size = sum(f.stat().st_size for p in copies for f in p.iterdir() if f.is_file())
    return {"enabled": agent_paths()[0].exists(), "count": len(copies), "bytes": size, "age_h": latest(out)[1], "keep": KEEP}


def status(out):
    plist, script, _ = agent_paths()
    path, age = latest(out)
    print(f"Остання копія: {path.name if path else 'немає'}" + (f" ({age:.0f} год тому)" if path else ""))
    installed = plist.exists()
    print("Розклад: " + ("увімкнено" if installed else "вимкнено"))
    if installed and (not script.exists() or script.read_bytes() != Path(__file__).read_bytes()):
        print("Скрипт змінився: повторіть --install-agent")
    return 0 if path and age < STALE_DAYS * 24 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Бекап баз Pi на Mac")
    parser.add_argument("--host", default=os.environ.get("RADIO345_PI_SSH", "radio345"))
    parser.add_argument("--root", default="radio345-hub", help="каталог проєкту на Pi (від домашнього)")
    parser.add_argument("--out", type=Path, default=OUT)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--install-agent", action="store_true", help="увімкнути щоденну копію")
    action.add_argument("--remove-agent", action="store_true", help="вимкнути (копії лишаються)")
    action.add_argument("--status", action="store_true")
    action.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.host.startswith("-") or not re.fullmatch(r"[\w.@-]+", args.host):
        raise SystemExit("Некоректна адреса Pi")
    if args.install_agent or args.remove_agent:
        if sys.platform != "darwin":
            raise SystemExit("Розклад — лише на Mac")
        if args.install_agent:
            install_agent(args.host, args.root, args.out.expanduser().resolve())
            print(f"Увімкнено: копія раз на добу в {args.out}")
        else:
            remove_agent()
            print("Вимкнено; копії лишилися")
        return 0
    if args.status:
        return status(args.out)
    if args.scheduled:
        _, age = latest(args.out)
        if age is not None and 0 <= age < DUE_H:
            return 0
        print(datetime.now().strftime("%Y-%m-%d %H:%M"), flush=True)
    try:
        path = pull(ssh_command(args.host, args.root), args.out)
    except (OSError, ValueError, RuntimeError, tarfile.TarError, sqlite3.Error, subprocess.SubprocessError) as exc:
        if args.scheduled:
            warn_stale(args.out)
        raise SystemExit(f"Бекап не зроблено: {type(exc).__name__}: {exc}") from None
    print(f"{path}: {', '.join(f'{p.name} {p.stat().st_size // 1024} КБ' for p in sorted(path.iterdir()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
