import time


class RestartPolicy:
    """Ограничивает частоту и количество перезапусков."""

    def __init__(self, delays=(5.0, 15.0, 45.0), clock=None):
        if not delays or any(delay <= 0 for delay in delays):
            raise ValueError("Затримки мають бути додатними")

        self.delays = tuple(delays)
        self.clock = clock or time.monotonic
        self.attempts = 0
        self.next_retry_at = None

    @property
    def exhausted(self):
        return self.attempts >= len(self.delays)

    def should_restart(self):
        """True, если пришло время очередной попытки."""

        if self.exhausted:
            return False

        now = self.clock()

        if self.next_retry_at is None:
            self.next_retry_at = now + self.delays[self.attempts]
            return False

        if now < self.next_retry_at:
            return False

        self.attempts += 1
        self.next_retry_at = None

        return True

    def reset(self):
        """Сбрасывает историю попыток перезапуска."""
        self.attempts = 0
        self.next_retry_at = None
