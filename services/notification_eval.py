"""Розмітка реальних фрагментів («сповіщення» / «мовлення») і оцінка фільтрів на ній.

Записи лежать лише локально (каталог 0700, файли 0600) і ніколи не
публікуються. Зразки фільтра — це еталон; розмітка — перевірочний набір.
Запис, з якого зроблено зразок, не порівнюється сам із собою.
"""

import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time
import wave

import numpy as np

from core.notification_filter import NotificationSoundFilter, _prepare_template, load_templates
from core.notification_spectral import MAX_SCORE_S, SpectralNotificationMatcher, template_hash
from services.notification_samples import _locked

SAMPLE_RATE = 16000
MAX_LABELS = 300
LABELS = ("notification", "speech")
FIX_ID = re.compile(r"(?:db:[1-9][0-9]{0,17}|mem:[0-9a-f]{32})\Z")
CLIP_NAME = re.compile(r"[0-9a-f]{32}\.wav\Z")
INDEX = "labels.json"
GRID = tuple(round(0.60 + 0.02 * step, 2) for step in range(20))
MIN_NOTIFICATIONS = 3
MIN_SPEECH = 10


def _read_index(root):
    path = Path(root) / INDEX
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return {}
    return {key: value for key, value in items.items()
            if FIX_ID.fullmatch(str(key)) and isinstance(value, dict)
            and value.get("label") in LABELS and CLIP_NAME.fullmatch(str(value.get("file", "")))}


def _write_index(root, items):
    temporary = Path(root) / f".{INDEX}.{secrets.token_hex(4)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump({"version": 1, "items": items}, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, Path(root) / INDEX)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _pcm_from_wav(content):
    try:
        with wave.open(io.BytesIO(content), "rb") as audio:
            if (audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != SAMPLE_RATE
                    or not 0 < audio.getnframes() <= MAX_SCORE_S * SAMPLE_RATE):
                raise ValueError(f"Потрібен моно WAV PCM16, 16000 Гц, до {MAX_SCORE_S:g} секунд")
            raw = audio.readframes(audio.getnframes())
            if len(raw) != audio.getnframes() * 2:
                raise ValueError("Неповний WAV")
            return raw
    except (wave.Error, EOFError) as exc:
        raise ValueError("Непридатний WAV") from exc


def read_labels(directory):
    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        return {}
    return {key: value["label"] for key, value in _read_index(root).items()}


def label_counts(directory):
    labels = list(read_labels(directory).values())
    return {name: labels.count(name) for name in LABELS}


def set_label(directory, fix_id, label, wav_content):
    if not FIX_ID.fullmatch(fix_id or ""):
        raise ValueError("Некоректний ідентифікатор фрази")
    if label not in LABELS:
        raise ValueError("Невідома позначка")
    raw = _pcm_from_wav(wav_content)
    with _locked(directory) as root:
        items = _read_index(root)
        previous = items.get(fix_id)
        if previous is None and len(items) >= MAX_LABELS:
            raise ValueError(f"Дозволено не більше {MAX_LABELS} позначок. Зніміть зайві.")
        name = previous["file"] if previous else secrets.token_hex(16) + ".wav"
        path = root / name
        temporary = root / f".{name}.{secrets.token_hex(4)}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as output, wave.open(output, "wb") as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(SAMPLE_RATE)
                audio.writeframes(raw)
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        items[fix_id] = {"label": label, "file": name, "at": int(time.time())}
        _write_index(root, items)
    return {"fix_id": fix_id, "label": label, "duration_s": len(raw) / 2 / SAMPLE_RATE}


def remove_label(directory, fix_id):
    if not FIX_ID.fullmatch(fix_id or ""):
        raise ValueError("Некоректний ідентифікатор фрази")
    root = Path(directory)
    if not root.is_dir():
        return {"fix_id": fix_id, "label": None}
    with _locked(root) as root:
        items = _read_index(root)
        previous = items.pop(fix_id, None)
        if previous:
            _write_index(root, items)
            (root / previous["file"]).unlink(missing_ok=True)
    return {"fix_id": fix_id, "label": None}


def _summary(items, matcher):
    notifications = [i for i in items if i["label"] == "notification" and i.get("score") is not None]
    speech = [i for i in items if i["label"] == "speech" and i.get("score") is not None]

    def found(group, threshold):
        return sum(matcher.is_candidate(i["score"], i["outside_s"], i["extra_ratio"], threshold) for i in group)

    table = [{"threshold": t, "notifications_found": found(notifications, t), "speech_dropped": found(speech, t)}
             for t in GRID]
    result = {"notifications": len(notifications), "speech": len(speech), "table": table,
              "current_threshold": matcher.threshold, "recommended_threshold": None}
    current = {"notifications_found": found(notifications, matcher.threshold),
               "speech_dropped": found(speech, matcher.threshold)}
    result["current"] = current
    if len(notifications) < MIN_NOTIFICATIONS or len(speech) < MIN_SPEECH:
        result["status"] = "need_more"
        return result
    safe = [row for row in table if row["speech_dropped"] == 0]
    if not safe:
        result["status"] = "not_separable"
        return result
    # Безпечний діапазон: від найнижчого порогу без хибних на мовленні до найвищого,
    # що ще знаходить максимум сповіщень. Беремо середину — запас з обох боків.
    best_found = max(row["notifications_found"] for row in safe)
    if best_found == 0:
        result["status"] = "not_separable"
        return result
    low = GRID.index(safe[0]["threshold"])
    high = max(GRID.index(row["threshold"]) for row in safe if row["notifications_found"] == best_found)
    recommended = table[(low + high + 1) // 2]
    result["status"] = "ok"
    result["recommended_threshold"] = recommended["threshold"]
    result["recommended"] = recommended
    return result


def evaluate(eval_dir, samples_dir, *, threshold, max_outside_s, max_extra, log=None):
    """Оцінки обох фільтрів на всіх розмічених записах і рекомендований поріг."""
    log = log or logging.getLogger(__name__)
    templates = load_templates(samples_dir, SAMPLE_RATE, log)
    digests = [template_hash(t) for t in templates]
    matcher = SpectralNotificationMatcher(templates, SAMPLE_RATE, prepared=True, threshold=threshold,
                                          max_outside_s=max_outside_s, max_extra=max_extra)
    waveform = NotificationSoundFilter([], SAMPLE_RATE)
    root = Path(eval_dir)
    index = _read_index(root) if root.is_dir() and not root.is_symlink() else {}
    items = []
    for fix_id, entry in sorted(index.items(), key=lambda pair: pair[1].get("at", 0), reverse=True):
        item = {"fix_id": fix_id, "label": entry["label"], "at": entry.get("at")}
        try:
            raw = _pcm_from_wav((root / entry["file"]).read_bytes())
        except (OSError, ValueError):
            item["error"] = "Запис недоступний"
            items.append(item)
            continue
        audio = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768
        item["duration_s"] = round(len(audio) / SAMPLE_RATE, 2)
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
        waveform.templates = tuple(t for t, d in zip(templates, digests) if d not in exclude)
        item["waveform_match"] = bool(waveform.is_notification(raw))
        items.append(item)
    return {"templates": len(templates), "items": items, "summary": _summary(items, matcher),
            "limits": {"max_outside_s": max_outside_s, "max_extra": max_extra}}
