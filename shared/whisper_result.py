"""Розбір результату Whisper без MLX: спільний для локального й мережевого розпізнавача."""

COMPRESSION_LIMIT = 2.4   # вище — Whisper зациклився («Грозу, Грозу, Грозу…»): сегмент відкидається


class RecognizerUnavailable(Exception):
    """Вузол розшифровки недоступний чи зайнятий: фразу треба відкласти, а не втратити."""


def is_looped(result):
    """Whisper зациклився хоча б в одному сегменті (після всіх температур)."""
    return isinstance(result, dict) and any(
        isinstance(s, dict) and (s.get("compression_ratio") or 0) > COMPRESSION_LIMIT for s in result.get("segments", ()))


def extract_text(result):
    """Отбирает пригодные для дальнейшей обработки фрагменты."""
    parts = [
        segment["text"].strip()
        for segment in result.get("segments", [])
        if not (
            segment.get("no_speech_prob", 0.0) > 0.6
            and segment.get("avg_logprob", 0.0) < -0.8
        )
        and segment.get("compression_ratio", 0) <= COMPRESSION_LIMIT
    ]
    return " ".join(parts).strip()
