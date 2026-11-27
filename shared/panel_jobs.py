"""Збережені фонові завдання панелі; один ізольований процес, без моделей."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from shared.paths import place, root_of

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = ("queued", "running")
LISTS = ("phrase_templates", "phrase_patterns", "callsign_aliases", "aliases", "blacklist", "short_garbage")
KEEP_FINISHED = 5   # завершених завдань кожного виду
KINDS = ("impact", "evaluate", "suggestions", "notification_evaluation", "backup", "archive_inventory", "archive_learning", "training_export", "sync_events")
NON_CANCELLABLE = ("backup", "archive_learning", "training_export", "sync_events")


class QueueFull(ValueError):
    pass


def directory(db_path):
    root = place(root_of(db_path), ".local-text-checks")
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def _write(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, ensure_ascii=False, allow_nan=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _locked(root):
    with (root / "state.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _path(root, job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise LookupError("Перевірку не знайдено")
    return root / (job_id + ".json")


def _items(root):
    return sorted((json.loads(p.read_text()) for p in root.glob("*.json")), key=lambda j: j["created"], reverse=True)


def _launch(root):
    # flock у виконавці не допускає двох обчислень водночас.
    subprocess.Popen([sys.executable, "-m", "shared.panel_jobs", str(root)], cwd=ROOT,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def submit(db_path, archive, kind, *, list_type=None, entries=None, deep=False, options=None, actor_id=None):
    if kind not in KINDS or (kind == "evaluate" and list_type not in LISTS):
        raise ValueError("Невідомий вид перевірки")
    if kind == "evaluate":
        from shared.text_lists import parse
        _, errors = parse(list_type, [(n, e["raw_line"]) for n, e in enumerate(entries or [], 1)
                                     if e.get("enabled", True) and e["raw_line"].strip()])
        if errors:
            raise ValueError("; ".join(errors[:5]))
    root = directory(db_path)
    with _locked(root):
        jobs = _items(root)
        active = [j for j in jobs if j["status"] in ACTIVE]
        same = next((j for j in active if j["kind"] == kind and j.get("list_type") == list_type
                     and j.get("entries") == entries and j.get("deep", False) == deep
                     and j.get("options") == options and j.get("actor_id") == actor_id), None)
        if same:
            return public(same)
        if len(active) >= 8:
            raise QueueFull("У черзі вже 8 завдань; дочекайтеся завершення або скасуйте зайві")
        job = dict(id=uuid.uuid4().hex, kind=kind, list_type=list_type, entries=entries,
                   db_path=str(Path(db_path).resolve()), archive=str(Path(archive).resolve()),
                   deep=deep, options=options, actor_id=actor_id, status="queued", created=time.time(), progress={"done": 0, "total": None})
        _write(_path(root, job["id"]), job)
        # Ліміт — для кожного виду окремо: автоматичні «suggestions» щопівгодини не мають
        # витісняти за ніч результат довгої ручної перевірки (контр-перевірки тощо).
        kept = {}
        for old in [j for j in jobs if j["status"] not in ACTIVE]:
            kept[old["kind"]] = kept.get(old["kind"], 0) + 1
            if kept[old["kind"]] > KEEP_FINISHED:
                _path(root, old["id"]).unlink(missing_ok=True)
                (root / (old["id"] + ".result")).unlink(missing_ok=True)
    _launch(root)
    return public(job)


def public(job, *, result=False):
    keys = ("id", "kind", "list_type", "status", "created", "started", "finished", "progress", "error")
    return {**{key: job[key] for key in keys if key in job}, "cancellable": job["kind"] not in NON_CANCELLABLE,
            **({"result": job["result"]} if result and "result" in job else {})}


def read(db_path, job_id=None, *, include_result=True):
    root = directory(db_path)
    worker_active = False
    with _locked(root):
        # Після аварії процесу не залишаємо вічне «виконується».
        with (root / "worker.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                worker_active = True
            else:
                for job in _items(root):
                    if job["status"] == "running":
                        job.update(status="interrupted", finished=time.time(), error=_interrupted(job))
                        _write(_path(root, job["id"]), job)
        if job_id:
            path = _path(root, job_id)
            if not path.is_file():
                raise LookupError("Перевірку не знайдено")
            jobs = [json.loads(path.read_text())]
        else:
            jobs = _items(root)
    if not worker_active and any(j["status"] == "queued" for j in jobs):
        _launch(root)
    if job_id and include_result and jobs[0]["status"] == "done":
        try:
            jobs[0]["result"] = json.loads((root / (job_id + ".result")).read_text())
        except (OSError, ValueError):
            jobs[0].update(status="failed", error="Збережений звіт недоступний. Запустіть перевірку повторно.")
    return public(jobs[0], result=True) if job_id else [public(j) for j in jobs]


def cancel(db_path, job_id):
    root = directory(db_path)
    with _locked(root):
        path = _path(root, job_id)
        if not path.exists():
            raise LookupError("Перевірку не знайдено")
        job = json.loads(path.read_text())
        if job["status"] == "running" and job["kind"] in NON_CANCELLABLE:
            raise ValueError("Це завдання вже виконує зміни; дочекайтеся результату. Скасування доступне лише до початку.")
        if job["status"] in ACTIVE:
            job["cancel"] = True
            if job["status"] == "queued":
                job.update(status="cancelled", finished=time.time())
            _write(path, job)
    return public(job)


class Cancelled(BaseException):
    """Кероване скасування не повинні ковтати обробники помилок окремих правил."""
    pass


def _interrupted(job):
    if job["kind"] in NON_CANCELLABLE:
        return "Завдання перервано. Частина змін могла зберегтися; перевірте результат перед повторним запуском."
    return "Завдання перервано перезапуском. Запустіть повторно."


def _execute(job, progress):
    from shared.panel_tasks import execute
    return execute(job, progress)


def work(root):
    root = Path(root)
    with (root / "worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # flock гарантує, що знімки іншого активного виконавця тут не видаляються.
        import shutil
        for old in root.glob("snapshot-*"):
            if old.is_dir() and not old.is_symlink():
                shutil.rmtree(old)
        with _locked(root):
            for old in _items(root):
                if old["status"] == "running":
                    old.update(status="interrupted", finished=time.time(), error=_interrupted(old))
                    _write(_path(root, old["id"]), old)
        while True:
            with _locked(root):
                pending = [j for j in reversed(_items(root)) if j["status"] == "queued"]
                if not pending:
                    return
                job = pending[0]
                path = _path(root, job["id"])
                job.update(status="running", started=time.time())
                _write(path, job)
            last = 0
            def progress(done, total):
                nonlocal last
                now = time.monotonic()
                if now - last < .5 and (not total or done != total):
                    return
                last = now
                with _locked(root):
                    state = json.loads(path.read_text())
                    if state.get("cancel"):
                        raise Cancelled()
                    state["progress"] = {"done": done, "total": total}
                    _write(path, state)
            try:
                result = _execute(job, progress)
                status, error = "done", None
            except Cancelled:
                result, status, error = None, "cancelled", None
            except Exception as exc:
                result, status, error = None, "failed", f"Завдання не завершено ({type(exc).__name__}). Перевірте стан даних перед повтором."
            with _locked(root):
                state = json.loads(path.read_text())
                state.update(status="cancelled" if state.get("cancel") else status, finished=time.time(), error=error)
                if result is not None and not state.get("cancel"):
                    # Звіти можуть містити багато прикладів: опитування черги читає лише метадані.
                    _write(root / (job["id"] + ".result"), result)
                _write(path, state)


if __name__ == "__main__":
    os.nice(10)
    work(sys.argv[1])
