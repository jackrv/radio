"""Зайнято на іншому пристрої: служба завершується цим кодом, і її НЕ перезапускають самі (r345, systemd)."""
import sys
import time

EXIT = 75          # EX_TEMPFAIL; у deploy/hub — RestartPreventExitStatus=75
BUSY_HITS = 2      # 409 підтверджується двічі
STABLE_S = 120.0   # довше — бот уже «господар»: поступається новий, а не він
GAP_S = 180.0      # без успішних опитувань (сон, мережа) господарем більше не вважаємося


def stop(text):
    """Завершити процес без автоматичного повтору."""
    print(text, file=sys.stderr, flush=True)
    raise SystemExit(EXIT)


def bot_busy(poll, tries=4):
    """poll() → HTTP-код короткого getUpdates без offset (нічого не підтверджує); мережевий збій — «не зайнято»."""
    hits = 0
    for _ in range(tries):
        try:
            code = poll()
        except OSError:
            return False   # requests.RequestException — підклас OSError; без мережі перевірить слухач
        hits += code == 409
        if hits >= BUSY_HITS:
            return True
    return False


class BotOwner:
    """Чи поступитися, коли Telegram відповідає 409: так — якщо ми щойно почали або довго не опитували."""

    def __init__(self, clock=time.time):   # wall clock: сон Mac має бути видно
        self.clock = clock
        self.since = self.last_ok = None
        self.hits = 0

    def ok(self):
        now = self.clock()
        if self.last_ok is None or now - self.last_ok > GAP_S:
            self.since = now
        self.last_ok, self.hits = now, 0

    def conflict(self):
        """True — поступитися й більше не пробувати."""
        now = self.clock()
        if self.last_ok is not None and now - self.last_ok <= GAP_S and self.last_ok - self.since >= STABLE_S:
            self.hits = 0
            return False
        self.hits += 1
        return self.hits >= BUSY_HITS
