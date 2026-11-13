import time


class Cooldown:
    def __init__(self, cooldown_s):
        self.cooldown_s = cooldown_s
        self.last_seen = {}

    def fresh(self, key) -> bool:
        """True, если такой записи не было последние cooldown_s секунд."""
        now = time.monotonic()

        if now - self.last_seen.get(key, 0) < self.cooldown_s:
            return False

        self.last_seen[key] = now
        return True

    def rollback(self, key):
        """Отменяет отметку cooldown после неудачной операции."""
        self.last_seen.pop(key, None)
