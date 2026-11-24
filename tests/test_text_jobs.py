"""Фонові текстові прогони: тимчасові бази, без аудіо й моделей."""
import json
import sqlite3
import subprocess
import sys
import time

import pytest

from shared import panel_jobs as text_jobs
from shared.dictionary_store import init_db
from shared.recordings import RecordingStore
from shared.text_lists import ensure_tables


def setup_job(tmp_path, monkeypatch):
    db_path = tmp_path / "events.db"
    init_db(db_path)
    with sqlite3.connect(db_path) as db:
        ensure_tables(db)
    store = RecordingStore(tmp_path / "archive")
    with store.connect():
        pass
    monkeypatch.setattr(text_jobs, "_launch", lambda root: None)
    return db_path, store.root


def test_job_is_deduplicated_persisted_and_resumable(tmp_path, monkeypatch):
    db, archive = setup_job(tmp_path, monkeypatch)
    first = text_jobs.submit(db, archive, "impact")
    assert text_jobs.submit(db, archive, "impact")["id"] == first["id"]
    assert first["status"] == "queued" and "db_path" not in first
    text_jobs.work(text_jobs.directory(db))
    result = text_jobs.read(db, first["id"])
    assert result["status"] == "done" and result["result"]["records"] == 0
    assert text_jobs.read(db)[0]["id"] == first["id"]
    assert "result" not in text_jobs.read(db)[0]
    assert (text_jobs.directory(db) / (first["id"] + ".json")).stat().st_mode & 0o777 == 0o600


def test_queued_cancel_invalid_input_and_failure_are_terminal(tmp_path, monkeypatch):
    db, archive = setup_job(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        text_jobs.submit(db, archive, "evaluate", list_type="phrase_patterns", entries=[{"raw_line": "{Х}"}])
    job = text_jobs.submit(db, archive, "impact")
    assert text_jobs.cancel(db, job["id"])["status"] == "cancelled"
    text_jobs.work(text_jobs.directory(db))
    assert text_jobs.read(db, job["id"])["status"] == "cancelled"
    next_job = text_jobs.submit(db, archive, "impact")
    def fail(*args):
        raise RuntimeError("private text must not appear in error")
    monkeypatch.setattr(text_jobs, "_execute", fail)
    text_jobs.work(text_jobs.directory(db))
    result = text_jobs.read(db, next_job["id"])
    assert result["status"] == "failed" and "private text" not in result["error"]
    with pytest.raises(LookupError):
        text_jobs.read(db, "../events.db")


def test_worker_progress_cancel_and_api_independence(tmp_path, monkeypatch):
    db, archive = setup_job(tmp_path, monkeypatch)
    job = text_jobs.submit(db, archive, "impact")
    root = text_jobs.directory(db)
    # Справжній дочірній процес тримає flock; довга робота замінена керованим циклом.
    code = '''
import sys, time
from pathlib import Path
from shared import panel_jobs as text_jobs
root = Path(sys.argv[1])
def execute(job, progress):
    (root / 'ready').touch()
    while True:
        progress(1, 2)
        time.sleep(.02)
text_jobs._execute = execute
text_jobs.work(root)
'''
    process = subprocess.Popen([sys.executable, "-c", code, str(root)], cwd=text_jobs.ROOT)
    try:
        deadline = time.monotonic() + 5
        while not (root / "ready").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (root / "ready").exists()
        result = text_jobs.read(db, job["id"])
        assert result["status"] == "running"
        text_jobs.cancel(db, job["id"])
        process.wait(timeout=5)
        assert text_jobs.read(db, job["id"])["status"] == "cancelled"
    finally:
        if process.poll() is None:
            process.kill(); process.wait()


def test_dead_worker_is_reported_and_new_job_can_run(tmp_path, monkeypatch):
    db, archive = setup_job(tmp_path, monkeypatch)
    job = text_jobs.submit(db, archive, "impact")
    path = text_jobs.directory(db) / (job["id"] + ".json")
    state = json.loads(path.read_text()); state["status"] = "running"
    text_jobs._write(path, state)
    assert text_jobs.read(db, job["id"])["status"] == "interrupted"
    retry = text_jobs.submit(db, archive, "impact")
    text_jobs.work(text_jobs.directory(db))
    assert text_jobs.read(db, retry["id"])["status"] == "done"


@pytest.mark.parametrize('kind',['blacklist','short_garbage'])
def test_filter_draft_evaluates_speech_loss_and_false_text_without_saving(tmp_path,monkeypatch,kind):
    db_path,archive=setup_job(tmp_path,monkeypatch)
    store=RecordingStore(archive)
    for label,reference in [('speech','синтетичний шелест'),('noise','')]:
        rid=store.save(b'\0\0'*1600,16000,{'status':'ready','raw_text':'синтетичний шелест'})
        store.review(rid,reference,label,0)
    with sqlite3.connect(db_path) as db:
        before=db.execute('SELECT * FROM text_list_entries').fetchall()
    job=text_jobs.submit(db_path,archive,'evaluate',list_type=kind,entries=[{'raw_line':'синтетичний шелест','enabled':True}])
    text_jobs.work(text_jobs.directory(db_path))
    done=text_jobs.read(db_path,job['id'])
    assert done['status']=='done',done
    result=done['result']
    assert result['before']['no_edits']==1 and result['after']['no_edits']==0
    assert result['before']['false_text']==1 and result['after']['false_text']==0
    assert result['after']['missed']==1
    with sqlite3.connect(db_path) as db:
        assert db.execute('SELECT * FROM text_list_entries').fetchall()==before


def test_started_side_effect_job_cannot_be_cancelled_and_needs_a_current_admin(tmp_path, monkeypatch):
    from shared.user_store import init_users_db, bootstrap_admin
    db, archive = setup_job(tmp_path, monkeypatch)
    init_users_db(db); bootstrap_admin(db, 1001)
    job = text_jobs.submit(db, archive, 'backup', actor_id=1001, options={'backup_dir':str(tmp_path/'backups')})
    path = text_jobs.directory(db) / (job['id'] + '.json')
    state = json.loads(path.read_text()); state['status'] = 'running'; text_jobs._write(path,state)
    with pytest.raises(ValueError):
        text_jobs.cancel(db,job['id'])
    assert 'cancel' not in json.loads(path.read_text())
    state['status'] = 'queued'; text_jobs._write(path,state)
    with sqlite3.connect(db) as connection:
        connection.execute('UPDATE telegram_users SET enabled=0 WHERE user_id=1001')
    text_jobs.work(text_jobs.directory(db))
    assert text_jobs.read(db,job['id'])['status'] == 'failed'
    assert not (tmp_path/'backups').exists()
