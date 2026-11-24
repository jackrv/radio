"""Контракт Pi ↔ вузол розшифровки (Mac). Запит: 4 байти довжини, JSON, звук float32 LE.

Звук — саме підготовлений float32 без втрат, щоб текст збігався з локальним розпізнаванням.
"""
import json
import struct

import numpy as np

VERSION = 1
PORT = 8347
MAX_BODY = 16 * 1024 * 1024   # ~4 хв float32 16 кГц
SEGMENT_FIELDS = ("start", "end", "text", "avg_logprob", "no_speech_prob", "compression_ratio", "temperature")


def pack_request(audio, *, prompt, language, sample_rate):
    head = json.dumps({"version": VERSION, "prompt": prompt, "language": language,
                       "sample_rate": sample_rate}, ensure_ascii=False).encode()
    return struct.pack("<I", len(head)) + head + np.asarray(audio, dtype="<f4").tobytes()


def unpack_request(body):
    if len(body) < 4:
        raise ValueError("Запит закороткий")
    size = struct.unpack_from("<I", body)[0]
    if size > len(body) - 4:
        raise ValueError("Пошкоджений заголовок запиту")
    head = json.loads(body[4:4 + size])
    if not isinstance(head, dict) or head.get("version") != VERSION:
        raise ValueError("Несумісна версія контракту")
    raw = body[4 + size:]
    if not raw or len(raw) % 4:
        raise ValueError("Пошкоджений звук")
    audio = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    if not np.isfinite(audio).all():
        raise ValueError("Звук містить нечислові значення")
    return head, audio


def pack_result(result):
    segments = [{k: segment[k] for k in SEGMENT_FIELDS if k in segment} for segment in result.get("segments", [])]
    return json.dumps({"version": VERSION, "text": result.get("text", ""), "segments": segments},
                      ensure_ascii=False, default=float).encode()


def unpack_result(body):
    result = json.loads(body)
    if (not isinstance(result, dict) or result.get("version") != VERSION
            or not isinstance(result.get("segments"), list)):
        raise ValueError("Несумісна відповідь вузла розшифровки")
    return result
