"""Позначки сегмента для аудіоархіву.

Ядро (розпізнавання, обробка подій) лише позначає поля поточного сегмента через
``note``; збирає й зберігає їх архів (shared/recordings.py). Поза записом
(``trace`` = None) позначки нічого не роблять.
"""
from contextvars import ContextVar

_trace = ContextVar("recording_trace", default=None)


def note(**fields):
    trace = _trace.get()
    if trace is not None:
        trace.update(fields)
