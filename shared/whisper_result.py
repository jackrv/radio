"""Розбір результату Whisper без MLX: спільний для локального й мережевого розпізнавача."""

COMPRESSION_LIMIT = 2.4   # вище — Whisper зациклився («Грозу, Грозу, Грозу…»): сегмент відкидається
NO_SPEECH_PROB = 0.6      # шматок-тиша: no_speech_prob вище І avg_logprob нижче — відкидається
NO_SPEECH_LOGPROB = -0.8


class RecognizerUnavailable(Exception):
    """Вузол розшифровки недоступний чи зайнятий: фразу треба відкласти, а не втратити."""


def is_looped(result, compression_limit=COMPRESSION_LIMIT):
    """Whisper зациклився хоча б в одному сегменті (після всіх температур); 0 — перевірку вимкнено."""
    return bool(compression_limit) and isinstance(result, dict) and any(
        isinstance(s, dict) and (s.get("compression_ratio") or 0) > compression_limit for s in result.get("segments", ()))


def dropped_reason(segment, compression_limit=COMPRESSION_LIMIT, no_speech_prob=NO_SPEECH_PROB,
                   no_speech_logprob=NO_SPEECH_LOGPROB):
    """Чому extract_text відкидає шматок: "loop", "silence" або None. 0 у порозі — правило вимкнено."""
    if compression_limit and (segment.get("compression_ratio") or 0) > compression_limit:
        return "loop"
    if no_speech_prob and segment.get("no_speech_prob", 0.0) > no_speech_prob \
            and segment.get("avg_logprob", 0.0) < no_speech_logprob:
        return "silence"
    return None


def extract_text(result, **limits):
    """Текст пригодних шматків; ``limits`` — пороги dropped_reason (панель «Налаштування»)."""
    parts = [segment["text"].strip() for segment in result.get("segments", []) if dropped_reason(segment, **limits) is None]
    return " ".join(parts).strip()
