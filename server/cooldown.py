import time
from collections import deque


class Cooldown:
    def __init__(self, cooldown_s):
        self.cooldown_s = cooldown_s
        self.last_seen = {}
        self._timeline = deque()

    def fresh(self, key) -> bool:
        """True, если такой записи не было последние cooldown_s секунд."""

        now = time.monotonic()

        # Удаляем только записи, у которых истёк интервал.
        # Полный перебор словаря не требуется.
        while (
            self._timeline
            and now - self._timeline[0][0] >= self.cooldown_s
        ):
            timestamp, old_key = self._timeline.popleft()

            # После rollback() ключ мог быть зарегистрирован
            # повторно с более новым временем.
            if self.last_seen.get(old_key) == timestamp:
                del self.last_seen[old_key]

        if key in self.last_seen:
            return False

        self.last_seen[key] = now
        self._timeline.append((now, key))
        return True

    def rollback(self, key):
        """Отменяет отметку cooldown после неудачной операции."""
        self.last_seen.pop(key, None)
