"""Small terminal dashboard; detailed records belong in rotating log files."""
import logging
import re
import shutil
import sys
import threading
import time
from collections import deque


class ConsoleDashboard(logging.Handler):
    def __init__(self, provider=None, stream=None):
        super().__init__()
        self.stream = stream or sys.stdout
        self.provider = provider or (lambda: {})
        self.events = deque(maxlen=8)
        self.started = time.time()
        self.stop_event = threading.Event()
        self.thread = None
        self.tty = self.stream.isatty()

    def start(self):
        if self.tty and self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return self

    def _run(self):
        while not self.stop_event.wait(1):
            self.render()

    @staticmethod
    def clean(text):
        return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(text))

    def emit(self, record):
        if record.levelno < logging.WARNING and not getattr(record, "console", False):
            return
        # Formatter still redacts secrets, but traceback stays in the file.
        import copy
        short = copy.copy(record)
        short.exc_info = short.exc_text = short.stack_info = None
        self.add(self.format(short))

    def add(self, message):
        with self.lock:
            line = f"{time.strftime('%H:%M:%S')} {self.clean(message)}"
            self.events.append(line)
            try:
                if self.tty:
                    self.render()
                else:
                    self.stream.write(line + "\n")
                    self.stream.flush()
            except (OSError, ValueError):
                pass

    def lines(self, data):
        gauges, counts = data.get("gauges", {}), data.get("counters", {})
        fresh = data.get("session_status") != "finished" and data.get("updated_at", 0) >= self.started - 2 and time.time() - data.get("updated_at", 0) < 15
        if not fresh:
            return ["Radio 345 | Очікування приймача / немає актуальних даних", "Докладний журнал: radio.log | Ctrl+C — зупинити"]
        uptime = max(0, int(time.time() - data.get("started_at", self.started)))
        state = {"listening": "Приймання", "paused": "Пауза", "stopped": "Зупинено"}.get(gauges.get("receiver.state"), "Запуск")
        ready = counts.get("asr.outcome.ready", 0) + counts.get("asr.outcome.ready_fallback", 0)
        return [f"Radio 345 | {state} | Робота {uptime // 3600:02}:{uptime // 60 % 60:02}:{uptime % 60:02}",
                f"Розпізнано: {ready} | Черга аудіо: {gauges.get('audio.segments_queue', 0)} | Черга Telegram: {gauges.get('telegram.queue_depth', 0)}",
                f"Втрачено кадрів / сегментів: {gauges.get('audio.frames_dropped', 0)} / {gauges.get('audio.segments_dropped', 0)} | radio.log | Ctrl+C — вихід"]

    def render(self):
        with self.lock:
            try:
                data = self.provider()
            except Exception:
                data = {}
            size = shutil.get_terminal_size((100, 24))
            rows = self.lines(data) + ["─" * min(size.columns - 1, 80), "Останні важливі події:"]
            rows += list(self.events)[-max(1, size.lines - len(rows) - 2):]
            frame = "\n".join(self.clean(row)[:max(1, size.columns - 1)] for row in rows[:max(1, size.lines - 1)])
            try:
                self.stream.write("\x1b[H\x1b[J" + frame + "\n")
                self.stream.flush()
            except (OSError, ValueError):
                pass

    def close(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        super().close()
