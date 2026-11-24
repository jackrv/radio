"""Ключ PTT рації: що «натискає» передачу, поки iPhone передає звук.

Налаштування `RADIO_PTT` (`.env` має пріоритет над config.py):

- `none` — ключа немає (зараз: звук на динаміках Mac, або VOX рації);
- `gpio:17` чи `gpio:gpiochip0:17` — лінія GPIO (Raspberry Pi; бібліотека gpiod v2).
  Лише через оптопару/транзистор, не напряму на PTT рації;
- `serial:/dev/ttyUSB0:rts` чи `…:dtr` — лінія послідовного порту (USB-serial, Digirig; pyserial).

Додатково `!` після типу — інверсія (`gpio!:17`): активний низький рівень.

Захист від «залиплої» передачі: ключ відпускається в `finally` сесії, сторожем через
`max_hold_s` незалежно від сесії та при виході процесу (atexit). Власний TOT рації
лишається обов'язковим останнім рубежем.
"""

import atexit
import threading
import time


class NoKey:
    name = "немає"

    def press(self):
        pass

    def release(self):
        pass

    def close(self):
        pass


class GpioKey:
    def __init__(self, line, chip="/dev/gpiochip0", active_low=False):
        import gpiod
        from gpiod.line import Direction, Value
        self._value = Value
        self.line = int(line)
        self.active_low = active_low
        settings = gpiod.LineSettings(direction=Direction.OUTPUT, active_low=active_low,
                                      output_value=Value.INACTIVE)
        self.request = gpiod.request_lines(chip, consumer="radio345-ptt", config={self.line: settings})
        self.name = f"GPIO {self.line}{' (інверсія)' if active_low else ''}"

    def press(self):
        self.request.set_value(self.line, self._value.ACTIVE)

    def release(self):
        self.request.set_value(self.line, self._value.INACTIVE)

    def close(self):
        try:
            self.release()
        finally:
            self.request.release()


class SerialKey:
    def __init__(self, port, signal="rts", inverted=False):
        import serial
        if signal not in ("rts", "dtr"):
            raise ValueError("лінія послідовного порту: rts або dtr")
        self.signal = signal
        self.inverted = inverted
        self.port = serial.Serial()
        self.port.port = port
        # Лінії виставляються до відкриття, щоб порт не «клацнув» передачею при старті.
        self.port.rts = inverted if signal == "rts" else False
        self.port.dtr = inverted if signal == "dtr" else False
        self.port.open()
        self.name = f"{port} {signal.upper()}{' (інверсія)' if inverted else ''}"

    def _set(self, active):
        setattr(self.port, self.signal, active != self.inverted)

    def press(self):
        self._set(True)

    def release(self):
        self._set(False)

    def close(self):
        try:
            self.release()
        finally:
            self.port.close()


def make_key(spec):
    spec = (spec or "none").strip()
    if spec in ("", "none", "off"):
        return NoKey()
    kind, _, rest = spec.partition(":")
    inverted = kind.endswith("!")
    kind = kind.rstrip("!")
    if kind == "gpio":
        parts = rest.split(":")
        if len(parts) == 1:
            return GpioKey(parts[0], active_low=inverted)
        chip = parts[0] if parts[0].startswith("/") else f"/dev/{parts[0]}"
        return GpioKey(parts[1], chip=chip, active_low=inverted)
    if kind == "serial":
        port, _, signal = rest.rpartition(":")
        if not port:
            port, signal = rest, "rts"
        return SerialKey(port, signal.lower(), inverted)
    raise ValueError(f"Невідомий ключ PTT: {spec}")


class GuardedKey:
    """Обгортка з тайм-аутом утримання та відпусканням при виході процесу."""

    def __init__(self, key, max_hold_s, log=None, clock=time.monotonic):
        self.key = key
        self.max_hold_s = max_hold_s
        self.log = log
        self.clock = clock
        self.lock = threading.Lock()
        self.pressed_at = None
        self._timer = None
        atexit.register(self.close)

    @property
    def name(self):
        return self.key.name

    @property
    def pressed(self):
        return self.pressed_at is not None

    def press(self):
        with self.lock:
            if self.pressed_at is not None:
                return
            self.key.press()
            self.pressed_at = self.clock()
            self._timer = threading.Timer(self.max_hold_s, self._expire)
            self._timer.daemon = True
            self._timer.start()

    def release(self):
        with self.lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self.pressed_at is None:
                return
            self.pressed_at = None
            try:
                self.key.release()
            except Exception:
                if self.log:
                    self.log.exception("Не вдалося відпустити PTT")
                raise

    def _expire(self):
        if self.log:
            self.log.error("PTT утримується довше %.0f с — примусово відпускаю", self.max_hold_s,
                           extra={"console": True})
        self.release()

    def close(self):
        try:
            self.release()
        finally:
            try:
                self.key.close()
            except Exception:
                pass
