"""Попередження адміністратору від служби розпізнавання: модель, ефір, розрив циклу, вузол Mac.

Кожна проблема — одне повідомлення й одне про відновлення. Працює в окремому потоці, тож
помічає й зависання самого розпізнавання. Без бота попередження лишаються в журналі.
"""
import time
from datetime import datetime

from server.node_control import read_json

NODE_FRESH_S = 15.0
NODE_DOWN_S = 120.0

RECOGNITION = {
    "stuck": lambda v: ("⚠️ <b>Розпізнавання не відповідає</b>\n"
                        f"Обробка фрази триває вже {max(1, round(v / 60))} хв. Ефір працює, фрази чекають у черзі."),
    "failing": lambda v: ("⚠️ <b>Розпізнавання не працює</b>\n"
                          f"Помилка на {v} фразах поспіль. Перевірте вузол Whisper на Mac."),
}


class Alerts:
    def __init__(self, notify, log, health, state_path, *, offline_since=lambda: None, offline_alert_s=0.0,
                 clock=time.time, monotonic=time.monotonic):
        self.notify, self.log, self.health, self.state_path = notify, log, health, state_path
        self.offline_since, self.offline_alert_s = offline_since, offline_alert_s
        self.clock, self.monotonic = clock, monotonic
        self.started = clock()
        self.recognition = None
        self.node_bad_since, self.node_seen, self.node_alerted = None, False, False
        self.last_gap = 0.0
        self.offline_alerted = False

    def send(self, text):
        self.log.warning("Попередження: %s", text.replace("\n", " "))
        try:
            self.notify(text)
        except Exception:
            self.log.exception("Попередження не передано")

    def tick(self):
        self._recognition()
        state = read_json(self.state_path) or {}
        self._node(state)
        self._gap(state)
        self._offline()

    def _recognition(self):
        problem = self.health.problem()
        if problem is not None and self.recognition is None:
            self.recognition = problem[0]
            self.send(RECOGNITION[problem[0]](problem[1]))
        elif problem is None and self.recognition is not None:
            self.recognition = None
            self.send("✅ Розпізнавання знову працює.")

    def _node(self, state):
        at = state.get("at")
        fresh = type(at) in (int, float) and -5 <= self.clock() - at <= NODE_FRESH_S
        if fresh and state.get("state") in ("listening", "paused"):
            self.node_seen, self.node_bad_since = True, None
            if self.node_alerted:
                self.node_alerted = False
                self.send("✅ Ефір знову слухається.")
            return
        # Вузол працює без входу — попереджаємо; вузла ніколи не було (запущено лише asr) — ні.
        if not fresh and not self.node_seen:
            return
        now = self.monotonic()
        self.node_bad_since = now if self.node_bad_since is None else self.node_bad_since
        if not self.node_alerted and now - self.node_bad_since >= NODE_DOWN_S:
            self.node_alerted = True
            reason = "вузол працює, але звуковий вхід не відкрито" if fresh else "вузол не відповідає"
            self.send(f"⚠️ <b>Ефір не слухається</b>\nУже {round(NODE_DOWN_S / 60)} хв: {reason}.")

    def _gap(self, state):
        gap = state.get("gap")
        if not isinstance(gap, dict):
            return
        at, seconds = gap.get("at"), gap.get("seconds")
        if not (type(at) in (int, float) and type(seconds) in (int, float)) or at <= max(self.last_gap, self.started):
            return
        self.last_gap = at
        begin, end = datetime.fromtimestamp(at - seconds), datetime.fromtimestamp(at)
        self.send("⚠️ <b>Розрив приймання</b>\n"
                  f"З {begin:%d.%m %H:%M} до {end:%d.%m %H:%M} ефір не записувався. Причину не встановлено: "
                  "можливі сон системи або затримка процесу.")

    def _offline(self):
        """Вузол Mac недоступний: це звичайний сон, тож типово мовчимо (RADIO_ASR_OFFLINE_ALERT_MIN)."""
        since = self.offline_since()
        if since is None:
            if self.offline_alerted:
                self.offline_alerted = False
                self.send("✅ Вузол розшифровки знову доступний.")
        elif self.offline_alert_s and not self.offline_alerted and self.monotonic() - since >= self.offline_alert_s:
            self.offline_alerted = True
            self.send("⚠️ <b>Вузол розшифровки недоступний</b>\nФрази відкладаються в архів і будуть розпізнані пізніше.")

    def run(self, stop, interval=5.0):
        while not stop.wait(interval):
            try:
                self.tick()
            except Exception:
                self.log.exception("Збій перевірки попереджень")
