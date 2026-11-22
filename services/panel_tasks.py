"""Доменні операції панелі. Виконуються лише серверним фоновим виконавцем."""
from pathlib import Path
import tempfile


def execute(job, progress):
    from services.sqlite_connection import read_only
    from services.text_replay import compare, countercheck, load_records, load_setup
    from services.recordings import RecordingStore
    options = job.get("options") or {}
    actor = job.get("actor_id")
    if job["kind"] in ("backup", "archive_inventory", "archive_learning", "training_export", "sync_events") and actor is None:
        raise PermissionError("Для операції потрібен адміністратор")
    if actor is not None:
        from contextlib import closing
        with closing(read_only(job["db_path"])) as db:
            allowed = db.execute("SELECT 1 FROM telegram_users WHERE user_id=? AND role='admin' AND enabled=1", (actor,)).fetchone()
        if not allowed:
            raise PermissionError("Доступ адміністратора вже недійсний")
    store = RecordingStore(job["archive"])
    progress(0, 0)
    if job['kind']=='archive_learning':
        from services.archive_learning import learn_saved_archive
        return learn_saved_archive(job['db_path'],store,actor,progress=progress)
    if job["kind"] == "archive_inventory":
        from services.archive_inventory import inventory
        return inventory(store.root)
    if job["kind"] == "backup":
        from services.db_backup import create_backup
        path = create_backup(job["db_path"], options["backup_dir"])
        return {"name": path.name}
    if job["kind"] == "training_export":
        from services.training_set import export
        return export(store, Path(job["db_path"]).resolve().parent / ".local-training", progress=progress)
    if job["kind"] == "notification_evaluation":
        from services.notification_eval import evaluate
        return evaluate(store, options["samples_dir"], progress=progress, **options["limits"])
    if job["kind"] == "sync_events":
        from services.archive_event_sync import sync_event_from_reference
        from services.suggestions import reference_checks, refresh
        result = {"updated": 0, "same": 0, "hidden": 0, "failed": 0, "none": 0}
        items = [i for i in reference_checks(job["db_path"], store) if i["kind"] == "ref_event"]
        for number, item in enumerate(items):
            progress(number, len(items))
            try:
                outcome = sync_event_from_reference(job["db_path"], store.get(item["record"]), actor)
            except LookupError:
                outcome = None
            result[outcome or "none"] += 1
        refresh(job["db_path"], store)
        return result
    if job["kind"] == "suggestions":
        from services.suggestions import refresh
        current, fresh = refresh(job["db_path"], RecordingStore(job["archive"]), deep=job.get("deep", False),
                                 progress=lambda *_: progress(0, 0))
        progress(1, 1)
        return {"total": len(current), "new": len(fresh)}
    # Незмінні знімки: оператор може продовжувати розмітку й редагувати словники.
    with tempfile.TemporaryDirectory(prefix="snapshot-", dir=Path(job["db_path"]).parent / ".local-text-checks") as tmp:
        import sqlite3
        root = Path(tmp)
        for source, name in ((job["db_path"], "events.db"), (Path(job["archive"]) / "index.sqlite3", "index.sqlite3")):
            if not Path(source).is_file() and name == "index.sqlite3":
                continue
            src = read_only(source)
            dst = sqlite3.connect(root / name)
            try:
                src.backup(dst)
            finally:
                src.close(); dst.close()
        progress(0, 1)
        if job["kind"] == "impact":
            return countercheck(root, root / "events.db", progress=progress)
        from services.text_lists import parse
        dictionaries, garbage, values = load_setup(root / "events.db")
        before = {"phrases": values.get("phrase_templates", ()), "patterns": values.get("phrase_patterns", ()),
                  "aliases": (values.get("aliases", {}), values.get("callsign_aliases", {}))}
        parsed, _ = parse(job["list_type"], [(n, e["raw_line"]) for n, e in enumerate(job["entries"], 1)
                                           if e.get("enabled", True) and e["raw_line"].strip()])
        after = dict(before)
        filters = {}
        if job["list_type"] in ("phrase_templates", "phrase_patterns"):
            after["phrases" if job["list_type"] == "phrase_templates" else "patterns"] = parsed
        elif job["list_type"] in ("blacklist", "short_garbage"):
            filters['after_blacklist' if job['list_type']=='blacklist' else 'after_short_garbage'] = parsed
        else:
            after["aliases"] = (parsed, before["aliases"][1]) if job["list_type"] == "aliases" else (before["aliases"][0], parsed)
        return compare(load_records(root, include_non_speech=True), dictionaries, garbage,
                       before=before, after=after, show=15, progress=progress, blacklist=values.get("blacklist", ()), **filters)
