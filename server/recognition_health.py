"""Чи розпізнавання справді працює, а не лише живий його потік.

Потік може жити, але зависнути всередині моделі або отримувати помилку
Whisper на кожному сегменті. Тиша й відкинуті за гучністю сегменти
несправністю не вважаються: рахуються лише сегменти, що дійшли до Whisper.
"""

import threading
import time

STUCK_AFTER_S = 180.0
MAX_CONSECUTIVE_ERRORS = 5


class RecognitionHealth:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._busy_since = None
        self._errors = 0

    def started(self):
        with self._lock:
            self._busy_since = self._clock()

    def finished(self, whisper_ok):
        """``whisper_ok``: True — успіх, False — помилка Whisper, None — Whisper не викликано."""
        with self._lock:
            self._busy_since = None
            if whisper_ok is True:
                self._errors = 0
            elif whisper_ok is False:
                self._errors += 1

    def reset(self):
        """Новий робочий потік: стан попереднього не переноситься."""
        with self._lock:
            self._busy_since = None
            self._errors = 0

    def problem(self, stuck_after_s=STUCK_AFTER_S, max_errors=MAX_CONSECUTIVE_ERRORS):
        """None або (``stuck``, секунди) / (``failing``, кількість помилок поспіль)."""
        with self._lock:
            if self._busy_since is not None:
                busy = self._clock() - self._busy_since
                if busy >= stuck_after_s:
                    return "stuck", busy
            if self._errors >= max_errors:
                return "failing", self._errors
        return None
