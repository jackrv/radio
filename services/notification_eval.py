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
# Ліміт сторонньої енергії під сигналом: шум ефіру під сигналом його підвищує. Мовлення
# відсікає передусім «звук поза збігом» (у мовлення — секунди), тож ліміт можна послаблювати,
# але не далі 20 %.
EXTRA_GRID = (0.02, 0.04, 0.06, 0.08, 0.10, 0.15, 0.20)
NEAR_BEST = 0.9      # найсуворіший ліміт, що дає щонайменше 90 % найкращого результату
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

    def found(group, threshold, max_extra=None):
        extra = matcher.max_extra if max_extra is None else max_extra
        return sum(i["score"] >= threshold and i["outside_s"] <= matcher.max_outside_s and i["extra_ratio"] <= extra
                   for i in group)

    table = [{"threshold": t, "notifications_found": found(groups["notification"], t),
              "speech_dropped": found(groups["speech"], t), "noise_dropped": found(groups["noise"], t)} for t in GRID]
    # Той самий підбір порогу для кожного ліміту сторонньої енергії.
    extras = []
    for extra in EXTRA_GRID:
        rows = [{"threshold": t, "notifications_found": found(groups["notification"], t, extra),
                 "speech_dropped": found(groups["speech"], t, extra)} for t in GRID]
        pick = _pick(rows)
        if pick:
            extras.append({"max_extra": extra, "threshold": pick["threshold"],
                           "notifications_found": pick["notifications_found"]})
    result = {"notifications": len(groups["notification"]), "speech": len(groups["speech"]),
              "noise": len(groups["noise"]), "table": table,
              "current_threshold": matcher.threshold, "recommended_threshold": None,
              "current": {"notifications_found": found(groups["notification"], matcher.threshold),
                          "speech_dropped": found(groups["speech"], matcher.threshold),
                          "noise_dropped": found(groups["noise"], matcher.threshold)},
              "current_max_extra": matcher.max_extra, "extras": extras, "best": None}
    if result["notifications"] < MIN_NOTIFICATIONS or result["speech"] < MIN_SPEECH:
        result["status"] = "need_more"
        return result
    recommended = _pick(table)
    if recommended is None:
        result["status"] = "not_separable"
        return result
    result.update(status="ok", recommended_threshold=recommended["threshold"], recommended=recommended)
    # Найкраще поєднання порогу й ліміту: найсуворіший ліміт, що дає ≥ 90 % максимуму.
    top = max((e["notifications_found"] for e in extras), default=0)
    if top:
        result["best"] = next(e for e in extras if e["notifications_found"] >= NEAR_BEST * top)
    return result


def _pick(table):
    """Поріг із безпечного діапазону (жодного відкинутого мовлення) або None.

    Діапазон — від найнижчого безпечного порогу до найвищого, що ще знаходить
    максимум сповіщень. Середина — запас з обох боків.
    """
    safe = [i for i, row in enumerate(table) if row["speech_dropped"] == 0]   # вищий поріг — менше відкинутого
    best = max((table[i]["notifications_found"] for i in safe), default=0)
    if not safe or best == 0:
        return None
    high = max(i for i in safe if table[i]["notifications_found"] == best)
    return table[(safe[0] + high + 1) // 2]


def evaluate(store, samples_dir, *, threshold, max_outside_s, max_extra, log=None, progress=None):
    """Оцінка спектрального фільтра на розмічених записах архіву й рекомендований поріг."""
    log = log or logging.getLogger(__name__)
    templates = load_templates(samples_dir, SAMPLE_RATE, log)
    digests = [template_hash(t) for t in templates]
    matcher = SpectralNotificationMatcher(templates, SAMPLE_RATE, prepared=True, threshold=threshold,
                                          max_outside_s=max_outside_s, max_extra=max_extra)
    items = []
    rows = _labelled(store)
    for index, row in enumerate(rows):
        if progress:
            progress(index, len(rows))
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
    if progress:
        progress(len(rows), len(rows))
    return {"templates": len(templates), "items": items, "summary": _summary(items, matcher),
            "limits": {"max_outside_s": max_outside_s, "max_extra": max_extra}}
