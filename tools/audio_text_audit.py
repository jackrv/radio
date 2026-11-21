"""Лише читання: перевірка акустичної місткості збереженого тексту без ASR.

.venv/bin/python -m tools.audio_text_audit
.venv/bin/python -m tools.audio_text_audit --record <id> --min-words 8
JSON містить тільки агрегати, ID лише за явного --record; тексту/аудіо немає.
Приймач не зупиняти; висновки не є дозволом автоматично відкидати записи.
"""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import wave

from core.audio_text_consistency import capacity_checks, measure_audio, word_count
from services.sqlite_connection import read_only
from tools.asr_lab import split_of

def audit(archive, *, record_id=None, min_words=12, max_words_per_s=5.0, extra_words=3):
    archive = Path(archive)
    if record_id is not None and not re.fullmatch(r'[0-9a-f]{32}', record_id):
        raise ValueError('Некоректний ID запису')
    # Валідація бюджету навіть для порожнього архіву.
    capacity_checks(0, {}, min_words=min_words, max_words_per_s=max_words_per_s, extra_words=extra_words)
    with closing(read_only(archive / 'index.sqlite3')) as db:
        query = 'SELECT id,created,label,reference,status,data FROM recordings'
        rows = db.execute(query + (' WHERE id=?' if record_id else
                          " WHERE reference IS NOT NULL AND label IN ('speech','unclear','noise','notification','room')"),
                          (record_id,) if record_id else ()).fetchall()
    if record_id is not None and not rows:
        raise LookupError('Запис не знайдено')
    totals, splits, failures, comparisons = Counter(), {}, Counter(), {}
    result = {'mode': 'analysis_only', 'budget': {'min_words': min_words,
              'max_words_per_s': max_words_per_s, 'extra_words': extra_words}}
    for rid, created, label, reference, status, encoded in rows:
        try:
            if not isinstance(rid, str) or not re.fullmatch(r'[0-9a-f]{32}', rid):
                raise ValueError('Некоректний ID запису')
            data = json.loads(encoded)
            with wave.open(str(archive / (rid + '.wav')), 'rb') as audio:
                if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
                    raise ValueError('Потрібен WAV PCM16 mono')
                metrics = measure_audio(audio.readframes(audio.getnframes()), audio.getframerate())
            words = word_count(data.get('raw_text') or '')
            checks = capacity_checks(words, metrics, min_words=min_words,
                                     max_words_per_s=max_words_per_s, extra_words=extra_words)
        except (OSError, EOFError, wave.Error, ValueError, TypeError, AttributeError) as exc:
            if record_id is not None:
                raise ValueError('Не вдалося прочитати запис: ' + type(exc).__name__) from exc
            failures[type(exc).__name__] += 1
            continue
        label = label or 'unreviewed'
        totals[label] += 1
        split = split_of(created)
        splits.setdefault(split, Counter())[label] += 1
        for name, check in checks.items():
            comparison = comparisons.setdefault(name, {s: {'flagged': Counter(), 'ready_negative': 0}
                                                       for s in ('all', 'tune', 'holdout')})
            if check['exceeds']:
                for subset in ('all', split):
                    comparison[subset]['flagged'][label] += 1
                    comparison[subset]['ready_negative'] += int(label in ('noise', 'notification', 'room') and status == 'ready')
        if record_id:
            result['record'] = {'id': rid, 'label': label, 'status': status, 'split': split,
                                'words': words, 'metrics': metrics, 'checks': checks}
    result.update(records=sum(totals.values()), labels=dict(totals), splits=splits,
                  failures=dict(failures), comparisons=comparisons)
    return result


def main():
    import config
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, default=config.RECORDINGS_DIR)
    parser.add_argument('--record')
    parser.add_argument('--min-words', type=int, default=12)
    parser.add_argument('--max-words-per-s', type=float, default=5.0)
    parser.add_argument('--extra-words', type=int, default=3)
    args = parser.parse_args()
    try:
        result = audit(args.archive, record_id=args.record, min_words=args.min_words,
                       max_words_per_s=args.max_words_per_s, extra_words=args.extra_words)
    except (ValueError, LookupError, OSError, sqlite3.Error) as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
