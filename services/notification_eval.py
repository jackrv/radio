"""Оцінка фільтрів звуків сповіщень на розмітці локального аудіоархіву.

Мітки ставляться в «Аудіоархіві» (services/recordings.py): там справжній PCM
приймача, зокрема відкинуті сегменти, а не стиснені голосові Telegram.

- «Сигнал» — фільтр має знайти такий запис;
- «Мовлення» й «Нерозбірливо» — відкидати не можна (втрата мовлення);
- «Шум ефіру» й «Кімната / клавіатура» — показуються окремо: відкинути шум не шкодить.

Зразки фільтра — еталон; запис, з якого зроблено зразок, сам із собою не
порівнюється. Оцінка лише читає архів і нічого в ньому не змінює.
"""

import io
import logging
import wave

import numpy as np

from core.notification_templates import _prepare_template, load_templates
from core.notification_spectral import MAX_SCORE_S, SpectralNotificationMatcher, template_hash

SAMPLE_RATE = 16000
POSITIVE = ("notification",)
NEGATIVE = ("speech", "unclear")
NEUTRAL = ("noise", "room")  # шум ефіру й кімнати: відкинути його не шкодить
GRID = tuple(round(0.60 + 0.02 * step, 2) for step in range(20))
MIN_NOTIFICATIONS = 3
MIN_SPEECH = 10
MAX_ITEMS = 2000


def _labelled(store, limit=MAX_ITEMS):
    labels = (*POSITIVE, *NEGATIVE, *NEUTRAL)
    marks = ", ".join("?" * len(labels))
    with store.connect() as db:
        return db.execute(
            f"SELECT id, created, label FROM recordings WHERE reference IS NOT NULL AND label IN ({marks}) "
            "ORDER BY created DESC LIMIT ?", (*labels, limit)).fetchall()


def label_counts(store):
    """Скільки розмічених записів кожної групи є в архіві."""
    counts = {"notification": 0, "speech": 0, "noise": 0}
    try:
        with store.connect() as db:
            rows = db.execute("SELECT label, COUNT(*) FROM recordings WHERE reference IS NOT NULL GROUP BY label").fetchall()
    except Exception:
        return counts
    for label, count in rows:
        group = "notification" if label in POSITIVE else "speech" if label in NEGATIVE else "noise" if label in NEUTRAL else None
        if group:
            counts[group] += count
    return counts


def _pcm(content):
    with wave.open(io.BytesIO(content), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != SAMPLE_RATE:
            raise ValueError("Непідтримуваний формат запису")
        raw = audio.readframes(audio.getnframes())
    if not raw or len(raw) % 2:
        raise ValueError("Порожній запис")
    return raw


def _summary(items, matcher):
    groups = {"notification": [i for i in items if i["label"] in POSITIVE and i.get("score") is not None],
              "speech": [i for i in items if i["label"] in NEGATIVE and i.get("score") is not None],
              "noise": [i for i in items if i["label"] in NEUTRAL and i.get("score") is not None]}

    def found(group, threshold):
        return sum(matcher.is_candidate(i["score"], i["outside_s"], i["extra_ratio"], threshold) for i in group)

    table = [{"threshold": t, "notifications_found": found(groups["notification"], t),
              "speech_dropped": found(groups["speech"], t), "noise_dropped": found(groups["noise"], t)} for t in GRID]
    result = {"notifications": len(groups["notification"]), "speech": len(groups["speech"]),
              "noise": len(groups["noise"]), "table": table,
              "current_threshold": matcher.threshold, "recommended_threshold": None,
              "current": {"notifications_found": found(groups["notification"], matcher.threshold),
                          "speech_dropped": found(groups["speech"], matcher.threshold),
                          "noise_dropped": found(groups["noise"], matcher.threshold)}}
    if result["notifications"] < MIN_NOTIFICATIONS or result["speech"] < MIN_SPEECH:
        result["status"] = "need_more"
        return result
    safe = [row for row in table if row["speech_dropped"] == 0]
    best = max((row["notifications_found"] for row in safe), default=0)
    if not safe or best == 0:
        result["status"] = "not_separable"
        return result
    # Безпечний діапазон: від найнижчого порогу без жодного відкинутого мовлення
    # до найвищого, що ще знаходить максимум сповіщень. Середина — запас з обох боків.
    low = GRID.index(safe[0]["threshold"])
    high = max(GRID.index(row["threshold"]) for row in safe if row["notifications_found"] == best)
    recommended = table[(low + high + 1) // 2]
    result.update(status="ok", recommended_threshold=recommended["threshold"], recommended=recommended)
    return result


def evaluate(store, samples_dir, *, threshold, max_outside_s, max_extra, log=None):
    """Оцінка спектрального фільтра на розмічених записах архіву й рекомендований поріг."""
    log = log or logging.getLogger(__name__)
    templates = load_templates(samples_dir, SAMPLE_RATE, log)
    digests = [template_hash(t) for t in templates]
    matcher = SpectralNotificationMatcher(templates, SAMPLE_RATE, prepared=True, threshold=threshold,
                                          max_outside_s=max_outside_s, max_extra=max_extra)
    items = []
    for row in _labelled(store):
        item = {"record_id": row["id"], "created": row["created"], "label": row["label"]}
        try:
            raw = _pcm(store.audio(row["id"]))
        except (LookupError, OSError, ValueError, wave.Error, EOFError):
            item["error"] = "Аудіо недоступне"
            items.append(item)
            continue
        audio = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768
        item["duration_s"] = round(len(audio) / SAMPLE_RATE, 2)
        if len(audio) > MAX_SCORE_S * SAMPLE_RATE:
            item["score"] = None
            item["error"] = f"довший за {MAX_SCORE_S:g} с — фільтр такі не оцінює"
            items.append(item)
            continue
        try:
            own = template_hash(_prepare_template(audio, SAMPLE_RATE))
        except ValueError:
            own = None
        exclude = {own} if own in digests else set()
        item["self_template"] = bool(exclude)
        result = matcher.score(audio, exclude)
        if result is None:
            item["score"] = None
        else:
            item.update({key: result[key] for key in ("score", "outside_s", "extra_ratio", "candidate")})
        items.append(item)
    return {"templates": len(templates), "items": items, "summary": _summary(items, matcher),
            "limits": {"max_outside_s": max_outside_s, "max_extra": max_extra}}
