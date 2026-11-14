#!/usr/bin/env python3
"""Перевірка Radio 345 без зміни даних. Звіт містить лічильники без фраз, персональних даних і токенів."""

import argparse
from collections import Counter
from contextlib import closing
from importlib.metadata import version, PackageNotFoundError
import json
from pathlib import Path
import re
import sqlite3
import platform
import shutil
import tempfile
import time

ROOT = Path(__file__).resolve().parent


def audit_database(path, *, verify_backup=False):
    # mode=ro also prevents accidentally creating a new empty production DB.
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        integrity = list(db.execute("PRAGMA integrity_check"))
        checks = {"integrity_ok": integrity == [("ok",)],
                  "foreign_key_violations": len(list(db.execute("PRAGMA foreign_key_check"))),
                  "journal_mode": db.execute("PRAGMA journal_mode").fetchone()[0]}
        required = {"radio_events", "word_corrections", "word_correction_votes", "dictionary_entries", "telegram_users"}
        checks["missing_required_tables"] = sorted(required - tables)
        counts = {table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                  for table in sorted(required & tables)}
        if {"word_corrections", "word_correction_votes"} <= tables:
            checks["confirmation_mismatches"] = db.execute("""
                SELECT COUNT(*) FROM word_corrections w WHERE confirmations <>
                (SELECT COUNT(*) FROM word_correction_votes v WHERE v.original=w.original AND v.corrected=w.corrected)
            """).fetchone()[0]
            checks["votes_without_rule"] = db.execute("""SELECT COUNT(*) FROM word_correction_votes v
                WHERE NOT EXISTS (SELECT 1 FROM word_corrections w WHERE w.original=v.original AND w.corrected=v.corrected)""").fetchone()[0]
            checks["active_without_confirmations"] = db.execute("SELECT COUNT(*) FROM word_corrections WHERE status='active' AND confirmations<2").fetchone()[0]
            checks["active_with_conflicting_votes"] = db.execute("""SELECT COUNT(*) FROM word_corrections w
                WHERE w.status='active' AND EXISTS (SELECT 1 FROM word_corrections other
                WHERE other.original=w.original AND other.corrected<>w.corrected
                AND other.status<>'disabled' AND other.confirmations>0)""").fetchone()[0]
        if {"radio_events", "word_correction_votes"} <= tables:
            checks["db_votes_without_event"] = db.execute("""SELECT COUNT(*) FROM word_correction_votes v
                WHERE v.source_id LIKE 'db:%' AND NOT EXISTS
                (SELECT 1 FROM radio_events e WHERE 'db:' || e.id=v.source_id)""").fetchone()[0]
        if "telegram_recent_phrases" in tables:
            total, expired, future = db.execute("""SELECT COUNT(*),
                COALESCE(SUM(created_at<=?),0), COALESCE(SUM(created_at>?),0)
                FROM telegram_recent_phrases""", (int(time.time()) - 86400, int(time.time()) + 60)).fetchone()
            counts.update(temporary_total=total, temporary_expired=expired, temporary_future=future)
        if "telegram_phrase_messages" in tables:
            counts["linked_audio"] = db.execute("SELECT COUNT(*) FROM telegram_phrase_messages WHERE voice_file_id IS NOT NULL AND state='visible'").fetchone()[0]
            counts["unfinished_deletions"] = db.execute("SELECT COUNT(*) FROM telegram_phrase_messages WHERE state='deleting'").fetchone()[0]
        if "telegram_users" in tables:
            counts["active_admins"] = db.execute("SELECT COUNT(*) FROM telegram_users WHERE role='admin' AND enabled=1").fetchone()[0]
        if "receiver_runtime" in tables:
            row = db.execute("SELECT state,updated_at FROM receiver_runtime WHERE id=1").fetchone()
            if row:
                counts["receiver"] = {"state": row[0], "heartbeat_age_s": round(time.time() - row[1], 1)}
        if "dictionary_entries" in tables:
            counts["enabled_dictionary_lines"] = dict(db.execute("SELECT dictionary_type, COUNT(*) FROM dictionary_entries WHERE enabled=1 GROUP BY dictionary_type"))
        if verify_backup:
            # Local private temporary copy only; its contents never enter the report.
            with tempfile.TemporaryDirectory(prefix="radio345-backup-check-") as folder:
                with closing(sqlite3.connect(str(Path(folder) / "snapshot.db"))) as restored:
                    db.backup(restored)
                    checks["backup_integrity_ok"] = list(restored.execute("PRAGMA integrity_check")) == [("ok",)]
                    checks["backup_counts_match"] = all(
                        restored.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == counts[table]
                        for table in required & tables)
        db.rollback()
    return {"checks": checks, "counts": counts}


def environment_report():
    packages = {}
    for name in ("mlx", "mlx-whisper", "mlx-lm", "sounddevice", "webrtcvad", "webrtcvad-wheels", "requests", "fastapi"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = "not_installed"
    if packages["webrtcvad"] == "not_installed" and packages["webrtcvad-wheels"] != "not_installed":
        packages["webrtcvad"] = "provided_by_webrtcvad-wheels"
    return {"python": platform.python_version(), "system": platform.system(),
            "architecture": platform.machine(), "sqlite": sqlite3.sqlite_version,
            "ffmpeg_available": shutil.which("ffmpeg") is not None,
            "free_disk_bytes": shutil.disk_usage(ROOT).free, "packages": packages}


LOG_PATTERNS = {
    "polling_errors": r"(?:Сбой связи|Збій зв’язку) в callback_listener|Telegram getUpdates: HTTP",
    "polling_recovered": r"(?:Связь с Telegram восстановлена|Зв’язок із Telegram відновлено)",
    "voice_attempt_failed": r"(?:Попытка \d/3 отправить голосовое не удалась|Спроба \d/3 надіслати голосове не вдалася)",
    "connection_reset": r"ConnectionResetError|Connection reset by peer",
    "read_timeouts": r"Read timed out|ReadTimeout",
    "voice_exhausted": r"(?:Все попытки исчерпаны|Усі спроби вичерпано)",
    "text_send_failed": r"Telegram \(текст\) (?:не отправлен|не надіслано)",
    "queue_overflow": r"(?:Очередь Telegram переполнена|Чергу Telegram переповнено)",
    "audio_drops": r"(?:Потери аудио:|Втрати аудіо:)",
    "shutdown_pending": r"Telegram: (?:завершение с \d+ неотправленными|завершення з \d+ ненадісланими)",
    "llm_rejected": r"(?:LLM отклонена|LLM відхилено)",
    "whisper_results": r"(?:Распознано \(сырое\)|Розпізнано \(початкове\))",
    "whisper_errors": r"(?:Ошибка распознавания Whisper|Помилка розпізнавання Whisper)",
    "event_errors": r"(?:Ошибка обработки события|Ошибка сохранения события|Помилка обробки події|Помилка збереження події)",
}


def audit_logs(paths):
    patterns = {k: re.compile(v) for k, v in LOG_PATTERNS.items()}
    counts = Counter({k: 0 for k in patterns})
    first = last = None
    for path in paths:
        with Path(path).open(encoding="utf-8", errors="replace") as file:
            for line in file:
                if re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", line):
                    date = line[:19]
                    first = min(first, date) if first else date
                    last = max(last, date) if last else date
                for key, pattern in patterns.items():
                    counts[key] += bool(pattern.search(line))
    return {"first_log_time": first, "last_log_time": last, "counts": dict(counts),
            "note": "Лічильники відображають записи журналу, а не доводять доставку чи якість розпізнавання."}


def probe_network():
    """Only read-only endpoints; never getUpdates or send a test message."""
    import requests
    from dotenv import dotenv_values
    from start_radio345 import read_expected_api_url

    token = dotenv_values(ROOT / ".env").get("TELEGRAM_BOT_TOKEN", "")
    checks = {}
    targets = [("local_api", "http://127.0.0.1:8765/api/status", 401)]
    public = read_expected_api_url()
    if public:
        targets.append(("https_api", public + "/api/status", 401))
    if token:
        targets.append(("telegram_getMe", f"https://api.telegram.org/bot{token}/getMe", 200))
    for name, url, expected in targets:
        started = time.monotonic()
        try:
            response = requests.get(url, headers={"ngrok-skip-browser-warning": "true"},
                                    timeout=(3, 5), allow_redirects=False)
            ok = response.status_code == expected
            if name == "telegram_getMe":
                ok = ok and response.json().get("ok") is True
            checks[name] = {"ok": ok, "http_status": response.status_code}
        except (requests.RequestException, ValueError):
            checks[name] = {"ok": False, "error": "Мережевий запит не вдався"}
        checks[name]["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
    if not token:
        checks["telegram_getMe"] = {"skipped": "Токен не налаштовано"}
    checks["note"] = "Одна проба на endpoint не доводить тривалу доступність або доставку повідомлень."
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "events.db")
    parser.add_argument("--verify-backup", action="store_true")
    parser.add_argument("--logs", type=Path, nargs="*")
    parser.add_argument("--network", action="store_true", help="GET-перевірки getMe та доступності API без зміни даних")
    args = parser.parse_args()
    report = {"generated_at": time.time(), "environment": environment_report()}
    if args.network:
        report["network"] = probe_network()
    try:
        report["database"] = audit_database(args.db, verify_backup=args.verify_backup)
    except (sqlite3.Error, OSError):
        report["database"] = {"error": "База недоступна або несумісна; дані не змінено."}
    try:
        report["logs"] = audit_logs(args.logs if args.logs is not None else sorted(ROOT.glob("radio*.log")))
    except OSError:
        report["logs"] = {"error": "Журнал недоступний"}
    snapshot = ROOT / "diagnostics.json"
    if snapshot.is_file():
        try:
            data = json.loads(snapshot.read_text())
            report["runtime"] = {"age_s": round(time.time() - data["updated_at"], 1), "metrics": data}
        except (OSError, ValueError, KeyError):
            report["runtime"] = {"error": "Некоректний знімок робочих метрик"}
    else:
        report["runtime"] = {"note": "Вимірювання будуть доступні після перезапуску оновленого приймача."}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    checks = report["database"].get("checks", {})
    errors = (report["database"].get("error") or not checks.get("integrity_ok")
              or any(checks.get(key) for key in ("missing_required_tables", "foreign_key_violations", "confirmation_mismatches",
                      "votes_without_rule", "active_without_confirmations", "active_with_conflicting_votes",
                      "db_votes_without_event")))
    if args.verify_backup:
        errors = errors or not checks.get("backup_integrity_ok") or not checks.get("backup_counts_match")
    if args.network:
        errors = errors or any(isinstance(item, dict) and item.get("ok") is False for item in report["network"].values())
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
