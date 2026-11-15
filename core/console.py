"""Small terminal dashboard; detailed records belong in rotating log files."""
import logging
import json
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
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
        self.screen_active = False
        self.event_levels = deque(maxlen=8)
        self.admin_url = None
        self._painted_rows = {}
        self._painted_size = None

    def start(self):
        if self.tty and self.thread is None:
            with self.lock:
                self.stream.write("\x1b[?1049h\x1b[?25l\x1b[?7l")
                self.stream.flush()
                self.screen_active = True

            self.render()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return self

    def _run(self):
        while not self.stop_event.wait(1):
            self.render()

    def set_admin_url(self, url):
        with self.lock:
            self.admin_url = url
            if self.tty:
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
        message = self.format(short)
        if os.environ.get("RADIO345_MANAGED_CONSOLE"):
            self.stream.write("RADIO345_EVENT " + json.dumps({"message": message, "level": record.levelno}, ensure_ascii=False) + "\n")
            self.stream.flush()
        else:
            self.add(message, record.levelno)

    def add(self, message, level=logging.INFO):
        with self.lock:
            line = f"{time.strftime('%H:%M:%S')} {self.clean(message)}"
            self.events.append(line)
            self.event_levels.append(level)
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

    @staticmethod
    def fit(text, columns):
        """Limit terminal cells, including double-width emoji and combining marks."""
        result, used = [], 0
        for char in ConsoleDashboard.clean(text):
            width = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
            if used + width > columns:
                break
            result.append(char)
            used += width
        return "".join(result)

    def terminal_size(self):
        # Read the actual output TTY, not stale COLUMNS/LINES or __stdout__.
        try:
            return os.get_terminal_size(self.stream.fileno())
        except (AttributeError, OSError, ValueError):
            return shutil.get_terminal_size((100, 24))

    def render(self):
        with self.lock:
            try:
                data = self.provider()
            except Exception:
                data = {}
            size = self.terminal_size()
            rows = self.lines(data)
            link_row = None
            if self.admin_url:
                link_row = len(rows)
                rows.append(f"Адмін-панель: {self.admin_url}")
            rows += ["─" * min(size.columns - 1, 80), "Останні важливі події:"]
            colors = ["36"] * len(rows)
            if data.get("gauges", {}).get("receiver.state") == "listening":
                colors[0] = "32"
            else:
                colors[0] = "33"
            count = max(1, size.lines - len(rows) - 2)
            rows += list(self.events)[-count:]
            colors += ["31" if level >= logging.ERROR else "33" if level >= logging.WARNING else "37" for level in list(self.event_levels)[-count:]]
            # Position every row explicitly. No LF, CR or automatic wrapping:
            # even a resize or a wide emoji must never scroll the terminal.
            frame = []
            if size != self._painted_size:
                self._painted_rows.clear()
            painted = {}
            for index in range(max(1, size.lines)):
                text = rows[index] if index < len(rows) else ""
                color = colors[index] if index < len(colors) else "0"
                text = self.fit(text, max(0, size.columns - 1))
                if index == link_row:
                    # Keep the literal URL for terminals with automatic URL detection;
                    # OSC 8 also makes it a link in terminals supporting hyperlinks.
                    text = f"\x1b]8;;{self.admin_url}\x1b\\\x1b[4m{text}\x1b]8;;\x1b\\"
                painted[index] = (text, color)
                if self._painted_rows.get(index) != painted[index]:
                    frame.append(f"\x1b[{index + 1};1H\x1b[{color}m" + text + "\x1b[0m\x1b[K")
            try:
                if frame:
                    self.stream.write("".join(frame) + "\x1b[H")
                    self.stream.flush()
                self._painted_rows = painted
                self._painted_size = size
            except (OSError, ValueError):
                pass

    def close(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        with self.lock:
            if self.screen_active:
                try:
                    self.stream.write("\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l")
                    self.stream.flush()
                except (OSError, ValueError):
                    pass
                self.screen_active = False
        super().close()


def child_event(source, line):
    """Only explicit application events reach the dashboard; raw output stays in logs."""
    if line.startswith("RADIO345_EVENT "):
        try:
            event = json.loads(line.removeprefix("RADIO345_EVENT "))
            if isinstance(event["message"], str) and type(event["level"]) is int:
                return event["message"], event["level"]
        except (ValueError, KeyError, TypeError):
            pass
        return None
    # Third-party progress bars, blank lines and traceback continuations are diagnostics.
    match = re.match(r"(?:INFO|WARNING|ERROR|CRITICAL):\s+(.+)", line.strip())
    if match and line.strip().startswith(("WARNING:", "ERROR:", "CRITICAL:")):
        level = logging.WARNING if line.strip().startswith("WARNING:") else logging.ERROR
        return f"{source}: {match[1]}", level
    return None
