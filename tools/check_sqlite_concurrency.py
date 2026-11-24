"""Синтетичний стрес паузи/hold/запису/розмітки; виключно тимчасові SQLite.

Коротко: .venv/bin/python -m tools.check_sqlite_concurrency
Тривало: .venv/bin/python -m tools.check_sqlite_concurrency --seconds 120 --interval .02
Шлях до робочої бази навмисно не приймається.
"""

import argparse
import json
import math
import multiprocessing as mp
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.receiver_control import reception_request, set_listening_hold, set_pause_minutes
from server.receiver_status import init_receiver_db, record_heartbeat
from shared.recordings import RecordingStore
from shared.sqlite_connection import get_db

ROLES = ("hold", "receiver", "review", "read")


def worker(role, root, iterations, seconds, interval, results):
    root = Path(root)
    path, store = root / "events.db", RecordingStore(root / "archive")
    stopped = threading.Event()
    failures, background_ops = [], []

    def background():
        # Одночасні з'єднання одного процесу, як heartbeat та читання панелі.
        try:
            while not stopped.wait(max(interval, .005)):
                if role == "receiver":
                    record_heartbeat(path, "synthetic", state="listening")
                else:
                    with store.connect() as db:
                        db.execute("SELECT COUNT(*) FROM recordings").fetchone()
                background_ops.append(True)
        except BaseException as exc:
            failures.append(exc)
            stopped.set()

    thread = None
    if role in ("receiver", "review"):
        thread = threading.Thread(target=background, daemon=True)
        thread.start()
    started, count = time.monotonic(), 0
    try:
        while count < iterations or time.monotonic() - started < seconds:
            if failures:
                raise failures[0]
            if role == "hold":
                set_pause_minutes(path, 5, now=1100)
                set_listening_hold(path, 10, now=1100)
                assert reception_request(path, now=1100) == "pause"
                set_listening_hold(path, 0, now=1100)
                assert reception_request(path, now=1100) == "pause"
                set_pause_minutes(path, 0, now=1100)
                assert reception_request(path, now=1100) is None
            elif role == "receiver":
                with get_db(path) as db:
                    db.execute("INSERT INTO synthetic_events VALUES(?)", (count,))
                store.save(b"\0\0" * 1600, 16000, {"status": "ready", "raw_text": "Клен 12, прийом"})
            elif role == "review":
                item = store.listing(limit=1)["items"][0]
                store.review(item["id"], "Клен 12, прийом", "speech", item["revision"])
                with get_db(path) as db:
                    db.execute("INSERT INTO synthetic_reviews VALUES(?)", (count,))
            else:
                with get_db(path) as db:
                    db.execute("SELECT COUNT(*) FROM synthetic_events").fetchone()
                store.summary()
                store.listing(limit=5)
            count += 1
            if interval:
                stopped.wait(interval)
    finally:
        stopped.set()
        if thread is not None:
            thread.join(timeout=15)
            if thread.is_alive():
                raise RuntimeError("Фонове з'єднання не завершилося")
    if failures:
        raise failures[0]
    results.put({"role": role, "completed": count, "background_ops": len(background_ops)})


def run_stress(*, iterations=200, seconds=0, interval=0, timeout=None):
    if type(iterations) is not int or iterations < 1:
        raise ValueError("iterations має бути додатним цілим")
    if not all(math.isfinite(value) and value >= 0 for value in (seconds, interval)):
        raise ValueError("seconds/interval мають бути скінченними й невід'ємними")
    timeout = max(60, seconds + 30, iterations * interval + 30) if timeout is None else timeout
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout має бути додатним і скінченним")
    context = mp.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="radio-sqlite-concurrency-") as tmp:
        root, started = Path(tmp), time.monotonic()
        path, store = root / "events.db", RecordingStore(root / "archive")
        with get_db(path) as db:
            db.execute("CREATE TABLE synthetic_events(i INTEGER PRIMARY KEY)")
            db.execute("CREATE TABLE synthetic_reviews(i INTEGER PRIMARY KEY)")
        set_pause_minutes(path, 60, now=1000)
        init_receiver_db(path)
        store.save(b"\0\0" * 1600, 16000, {"status": "ready", "raw_text": "Клен 12, прийом"})
        results = context.Queue()
        processes = [context.Process(target=worker, args=(role, tmp, iterations, seconds, interval, results))
                     for role in ROLES]
        try:
            for process in processes:
                process.start()
            deadline = started + timeout
            while any(process.is_alive() for process in processes):
                for process in processes:
                    process.join(timeout=.05)
                if any(process.exitcode not in (None, 0) for process in processes):
                    raise RuntimeError(f"Помилка дочірнього процесу: {[p.exitcode for p in processes]}")
                if time.monotonic() > deadline:
                    raise TimeoutError("Стрес-перевірка перевищила загальний ліміт часу")
            if any(process.exitcode != 0 for process in processes):
                raise RuntimeError(f"Помилка дочірнього процесу: {[p.exitcode for p in processes]}")
            completed = {}
            for _ in ROLES:
                try:
                    item = results.get(timeout=2)
                except queue.Empty:
                    raise RuntimeError("Немає результату дочірнього процесу") from None
                completed[item["role"]] = item
            with get_db(path) as db:
                assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
                counts = [db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                          for table in ("synthetic_events", "synthetic_reviews")]
            with store.connect() as db:
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                archive_count = db.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
            assert counts == [completed["receiver"]["completed"], completed["review"]["completed"]]
            assert archive_count == counts[0] + 1
            return {"processes": len(processes), "iterations_min": iterations, "seconds_min": seconds,
                    "roles": completed, "committed_counts": counts, "recordings": archive_count,
                    "integrity": "ok", "exitcodes": [p.exitcode for p in processes],
                    "elapsed_s": round(time.monotonic() - started, 3)}
        finally:
            # Виняток/timeout/Ctrl+C теж не лишає процесів з відкритими БД.
            for process in processes:
                if process.pid is not None and process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
                    if process.is_alive():
                        process.kill()
                        process.join(timeout=5)
            results.close()
            results.join_thread()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=200, help="мінімум циклів на кожний процес")
    parser.add_argument("--seconds", type=float, default=0, help="мінімальна тривалість кожного процесу")
    parser.add_argument("--interval", type=float, default=0, help="пауза між циклами для обмеження навантаження")
    args = parser.parse_args()
    print(json.dumps(run_stress(iterations=args.iterations, seconds=args.seconds, interval=args.interval)), flush=True)


if __name__ == "__main__":
    main()
