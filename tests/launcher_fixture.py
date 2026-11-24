"""Тестові служби: тимчасові TCP-порти, SQLite, flock і штучна черга Telegram."""

import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import sys
import threading
import time


def event(root, value):
    with (root / "events.txt").open("a") as file:
        file.write(value + "\n")


def service(name, root, port, hung=False):
    stopped = threading.Event()
    for signo in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signo, signal.SIG_IGN if hung else lambda *_: stopped.set())
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen()
    database = None
    lock = None
    if name == "radio":
        lock = os.open(root / ".radio345.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        database = sqlite3.connect(root / "test-only.db")
        database.execute("CREATE TABLE IF NOT EXISTS result (value INTEGER)")
        database.commit()
        database.execute("INSERT INTO result VALUES (1)")
    (root / f"{name}.ready").write_text(json.dumps({"pid": os.getpid(), "group": os.getpgrp()}))
    while not stopped.wait(0.02):
        try:
            print(f"{name}: alive", flush=True)
        except OSError:
            pass
    event(root, name + ".stopping")
    server.close()
    if database is not None:
        from services.telegram import TelegramService

        sent = []
        telegram = TelegramService("test", 16000, ":memory:", __import__("logging").getLogger("fixture"))

        def send(*_, **__):
            time.sleep(0.03)
            sent.append(True)

        telegram.send_voice = send
        for _ in range(3):
            telegram.notify("test", "", "", "synthetic", b"\0\0", recipients=[{"chat_id": 1}])
        assert telegram.shutdown(timeout=2) and len(sent) == 3
        event(root, "radio.queue_drained")
        database.commit()
        database.close()
        os.close(lock)
    event(root, name + ".done")


def parent(root, ports, *, slow_start=False, hung=False):
    import start_radio345 as launcher
    from shared.launcher_lifecycle import LauncherSession

    launcher.ROOT = root
    names = ["api", "ngrok", "radio"]
    for name in names + ["parent"]:
        (root / f"{name}.ready").unlink(missing_ok=True)
    with LauncherSession(root, names, log=launcher.output, grace={n: 2.0 for n in names}) as session:
        if "interrupt-register" in sys.argv:
            register = session.register

            def interrupted(name, process):
                register(name, process)
                (root / "registration.ready").touch()
                raise KeyboardInterrupt

            session.register = interrupted
        processes = []
        try:
            for name, port in zip(names, ports):
                command = [sys.executable, __file__, "service", name, str(root), str(port)]
                if hung and name == "radio":
                    command.append("hung")
                process = launcher.ManagedProcess(name, command, session=session)
                processes.append(process)
                deadline = time.monotonic() + 5
                while not (root / f"{name}.ready").exists():
                    if process.process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("test service did not start")
                    time.sleep(0.01)
                if slow_start and name == "api":
                    (root / "startup.ready").touch()
                    time.sleep(30)
            (root / "parent.ready").touch()
            while all(p.process.poll() is None for p in processes):
                time.sleep(0.02)
        except KeyboardInterrupt:
            pass
        finally:
            session.begin_shutdown()
            for process in reversed(processes):
                process.stop()


if __name__ == "__main__":
    if sys.argv[1] == "service":
        service(sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]), hung="hung" in sys.argv)
    else:
        if "tty" in sys.argv:
            import termios
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        parent(Path(sys.argv[2]), list(map(int, sys.argv[3:6])),
               slow_start="slow" in sys.argv, hung="hung" in sys.argv)
