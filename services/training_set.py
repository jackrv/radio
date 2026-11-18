"""Набір для дообучення Whisper з розмітки «Аудіоархіву»: готовність і вивантаження.

Лише локально: вивантаження — каталог .local-training/<час>/ з копіями WAV і
metadata.jsonl (audio, text, duration, split). Нікуди не надсилається.

Придатний запис: «Мовлення» з еталоном, до 30 с (межа вікна Whisper). Записи з
кількома передачами входять — у них еталон з « / », який перед навчанням стає пробілом.
Відкладена вибірка (test) — цілі дні, щоб модель не бачила під час навчання той самий ефір.
"""
import json
import shutil
import time
from pathlib import Path

MAX_SECONDS = 30.0
TARGET_SECONDS = (2 * 3600, 5 * 3600)   # орієнтири: помітний ефект — від ~2 год, добрий — ~5 год
TEST_SHARE = 0.1


def _rows(store):
    with store.connect() as db:
        rows = db.execute("""SELECT id, created, reference,
            COALESCE(json_extract(data,'$.signal.duration_s'), json_extract(data,'$.audio_duration_s')) AS duration
            FROM recordings WHERE label='speech' AND TRIM(COALESCE(reference,'')) <> ''
            ORDER BY created""").fetchall()
    return [(r[0], r[1], r[2], r[3] or 0.0) for r in rows]


def readiness(store):
    """Скільки годин розмічено для дообучення й скільки ще до орієнтирів."""
    rows = _rows(store)
    usable = [r for r in rows if 0 < r[3] <= MAX_SECONDS]
    seconds = sum(r[3] for r in usable)
    return {"records": len(usable), "seconds": round(seconds, 1), "too_long": len(rows) - len(usable),
            "targets": [{"seconds": t, "share": min(1.0, seconds / t)} for t in TARGET_SECONDS]}


def _split(rows):
    """Останні дні (≈10 % записів) — у test; решта — train."""
    days = {}
    for row in rows:
        days.setdefault(time.strftime("%Y-%m-%d", time.localtime(row[1])), []).append(row)
    test, need = set(), max(1, round(len(rows) * TEST_SHARE)) if len(days) > 1 else 0
    for day in sorted(days, reverse=True):
        if len(test) >= need:
            break
        test |= {r[0] for r in days[day]}
    return test


def export(store, destination):
    """Копіює придатні WAV і пише metadata.jsonl. Повертає шлях і підсумок."""
    rows = [r for r in _rows(store) if 0 < r[3] <= MAX_SECONDS]
    if not rows:
        raise ValueError("Немає розмічених записів мовлення для вивантаження")
    target = Path(destination) / time.strftime("%Y%m%dT%H%M%S")
    (target / "audio").mkdir(mode=0o700, parents=True)
    target.chmod(0o700)
    test = _split(rows)
    with open(target / "metadata.jsonl", "w", encoding="utf-8") as out:
        for record_id, _, reference, duration in rows:
            source = store.root / f"{record_id}.wav"
            if not source.is_file():
                continue
            shutil.copyfile(source, target / "audio" / source.name)
            text = " ".join(reference.replace(" / ", " ").replace("|", " ").split())
            out.write(json.dumps({"audio": f"audio/{source.name}", "text": text, "duration": round(duration, 2),
                                  "split": "test" if record_id in test else "train"}, ensure_ascii=False) + "\n")
    written = [json.loads(line) for line in (target / "metadata.jsonl").read_text(encoding="utf-8").splitlines()]
    return {"path": str(target), "records": len(written),
            "train": sum(r["split"] == "train" for r in written), "test": sum(r["split"] == "test" for r in written),
            "seconds": round(sum(r["duration"] for r in written), 1)}
