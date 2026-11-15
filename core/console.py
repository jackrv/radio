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
        self.native_mac_links = os.environ.get("TERM_PROGRAM") == "Apple_Terminal"
        self.admin_url = None
        self.problem = None
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

    def set_problem(self, message):
        with self.lock:
            self.problem = self.clean(message)
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

    # ---------- Вигляд ----------
    # Лише базові 16 кольорів ANSI: однаково виглядає в Terminal, iTerm і VS Code.
    DIM, TEXT, BRIGHT = "90", "37", "1;97"
    GOOD, WARN, BAD, ACCENT = "32", "33", "31", "36"
    BADGES = {"good": "1;30;42", "warn": "1;30;43", "bad": "1;97;41"}
    MODEL_STATES = {"ready": ("готовий", "32"), "loading": ("завантаження", "33"),
                    "failed": ("помилка", "31"), "disabled": ("вимкнено", "90")}

    @staticmethod
    def width(text):
        return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
                   for c in ConsoleDashboard.clean(text))

    @staticmethod
    def _clock(seconds):
        seconds = max(0, int(seconds))
        return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"

    @staticmethod
    def _ago(seconds):
        seconds = max(0, int(seconds))
        if seconds < 60:
            return f"{seconds} с тому"
        if seconds < 3600:
            return f"{seconds // 60} хв тому"
        return f"{seconds // 3600} год {seconds // 60 % 60} хв тому"

    def _fresh(self, data):
        updated = data.get("updated_at", 0)
        return (data.get("session_status") != "finished" and isinstance(updated, (int, float))
                and updated >= self.started - 2 and time.time() - updated < 15)

    def _status(self, data):
        """(текст плашки, тон, права частина шапки)."""
        if self.problem:
            return "✖ ЗУПИНЕНО", "bad", ""
        if not self._fresh(data):
            return "● ОЧІКУВАННЯ", "warn", ""
        state = (data.get("gauges") or {}).get("receiver.state")
        label, tone = {"listening": ("● ПРИЙМАННЯ", "good"), "paused": ("❚❚ ПАУЗА", "warn"),
                       "stopped": ("■ ЗУПИНЕНО", "bad")}.get(state, ("● ЗАПУСК", "warn"))
        uptime = time.time() - data.get("started_at", self.started)
        return label, tone, f"робота {self._clock(uptime)}"

    def _cells(self, data):
        """Показники: (підпис, значення, колір значення)."""
        gauges, counts = data.get("gauges") or {}, data.get("counters") or {}
        number = lambda value: value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
        ready = number(counts.get("asr.outcome.ready")) + number(counts.get("asr.outcome.ready_fallback"))
        audio_queue = number(gauges.get("audio.segments_queue"))
        telegram_queue = number(gauges.get("telegram.queue_depth"))
        frames, segments = number(gauges.get("audio.frames_dropped")), number(gauges.get("audio.segments_dropped"))
        # Ліва колонка — потік ефіру, права — швидкість і моделі.
        cells = [
            ("Розпізнано", str(ready), self.BRIGHT),
            ("Черга аудіо", str(audio_queue), self.WARN if audio_queue else self.GOOD),
            ("Черга Telegram", str(telegram_queue), self.WARN if telegram_queue else self.GOOD),
            ("Втрати", f"{frames} кадрів · {segments} сегм.", self.BAD if frames or segments else self.GOOD),
        ]
        latency = ((data.get("asr_latency") or {}).get("ready") or {}).get("total_latency_s") or {}
        if isinstance(latency.get("mean"), (int, float)):
            p95 = latency.get("recent_p95")
            text = f"{latency['mean']:.1f} с" + (f" · p95 {p95:.1f}" if isinstance(p95, (int, float)) else "")
            cells.append(("Затримка", text.replace(".", ","), self.WARN if (p95 or 0) > 10 else self.TEXT))
        recent = (data.get("asr_latency") or {}).get("recent_segments") or []
        stamps = [row.get("text_ready_unix_s") for row in recent if isinstance(row, dict)
                  and isinstance(row.get("text_ready_unix_s"), (int, float))]
        if stamps:
            cells.append(("Остання фраза", self._ago(time.time() - max(stamps)), self.TEXT))
        for key, label in (("models.whisper", "Whisper"), ("models.qwen", "Qwen")):
            if gauges.get(key) in self.MODEL_STATES:
                text, color = self.MODEL_STATES[gauges[key]]
                cells.append((label, text, color))
        if number(counts.get("llm.changed")):
            cells.append(("Qwen виправив", str(number(counts["llm.changed"])), self.TEXT))
        return cells

    def lines(self, data):
        """Текстовий зміст екрана без кольорів (для перевірок і не-TTY)."""
        label, _, right = self._status(data)
        rows = [f"Radio 345  {label.split(' ', 1)[-1].capitalize()}" + (f"  {right}" if right else "")]
        if self.problem:
            return rows + [self.problem, "Докладно: launcher.log та radio.log"]
        if not self._fresh(data):
            return [rows[0] + " — немає актуальних даних приймача", "Докладний журнал: radio.log"]
        return rows + [f"{name}: {value}" for name, value, _ in self._cells(data)]

    # ---------- Складання кадру ----------

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

    def _row(self, segments, columns, fill=None):
        """Сегменти (текст, колір) → рядок рівно в межах columns клітинок, із «…» при обрізанні."""
        out, used = [], 0
        for index, (text, sgr) in enumerate(segments):
            text = self.clean(text)
            room = columns - used
            if room <= 0:
                break
            if self.width(text) > room:
                text = self.fit(text, max(0, room - 1)) + "…"
            out.append(f"\x1b[{sgr}m{text}\x1b[0m" if sgr else text)
            used += self.width(text)
        if fill and used < columns:
            out.append(f"\x1b[{fill}m{' ' * (columns - used)}\x1b[0m")
        return "".join(out)

    def _wrap(self, text, columns):
        """Перенесення по словах для повідомлення про зупинку (не обрізаємо на півслові)."""
        lines, current = [], ""
        for word in self.clean(text).split():
            candidate = f"{current} {word}".strip()
            if self.width(candidate) <= columns or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        return lines + ([current] if current else [])

    def _layout(self, data, columns, height):
        """[(рядок ANSI, ключ для пропуску незмінних)] на весь екран; підвал — в останньому рядку."""
        inner = max(10, columns - 2)
        pad = lambda segments: [(" ", "")] + segments
        rule = (" " + "─" * min(inner, 76), self.DIM)
        label, tone, right = self._status(data)
        badge = f" {label} "
        left = [(" Radio 345 ", self.BRIGHT), (" ", ""), (badge, self.BADGES[tone])]
        gap = inner - self.width(" Radio 345 ") - 1 - self.width(badge) - self.width(right)
        header = left + ([(" " * max(1, gap), ""), (right, self.DIM)] if right and gap >= 2 else [])
        rows = [header, [rule]]
        link_row = None

        if self.problem:
            for line in self._wrap(self.problem, inner - 1)[:4]:
                rows.append(pad([(line, "1;" + self.BAD)]))
            rows.append(pad([("Докладно: launcher.log та radio.log", self.DIM)]))
        elif not self._fresh(data):
            rows.append(pad([("Немає актуальних даних приймача. ", self.TEXT),
                             ("Зачекайте запуску або перегляньте radio.log.", self.DIM)]))
        else:
            cells = self._cells(data)
            columns_count = 2 if inner >= 60 else 1
            # Заповнюємо по стовпцях: перші 4 показники — ліворуч, решта — праворуч.
            split = 4 if columns_count == 2 and len(cells) > 4 else len(cells)
            groups = [cells[:split], cells[split:]] if columns_count == 2 else [cells]
            label_width = [max((self.width(name) for name, _, _ in group), default=0) for group in groups]
            cell_width = inner // columns_count
            for line in range(max(len(group) for group in groups)):
                segments = []
                for column, group in enumerate(groups):
                    if line >= len(group):
                        continue
                    name, value, color = group[line]
                    if column:
                        used = sum(self.width(text) for text, _ in segments)
                        segments.append((" " * max(2, cell_width - used), ""))
                    segments += [(name + " " * (label_width[column] - self.width(name) + 2), self.DIM), (value, color)]
                rows.append(pad(segments))

        if self.admin_url:
            rows.append([rule])
            link_row = len(rows)
            hint = "  ⌘ + подвійний клік — відкрити" if self.native_mac_links else ""
            rows.append(pad([("Адмін-панель: ", self.DIM), (self.admin_url, "4;" + self.ACCENT), (hint, self.DIM)]))

        rows.append([rule])
        rows.append(pad([("ПОДІЇ", "1;" + self.DIM)]))
        footer_left, footer_right = "radio.log · launcher.log", "Ctrl+C — зупинити"
        space = inner - self.width(footer_left) - self.width(footer_right)
        footer = pad([(footer_left, self.DIM)] + ([(" " * space, ""), (footer_right, self.DIM)] if space >= 2 else []))

        available = max(0, height - len(rows) - 1)
        icons = {logging.ERROR: ("✖", self.BAD), logging.WARNING: ("!", self.WARN)}
        events = list(zip(self.events, self.event_levels))[-available:] if available else []
        for line, level in events:
            stamp, _, message = line.partition(" ")
            icon, color = icons.get(logging.ERROR if level >= logging.ERROR else level, ("·", self.DIM))
            text_color = self.BAD if level >= logging.ERROR else self.WARN if level >= logging.WARNING else self.TEXT
            rows.append(pad([(stamp, self.DIM), ("  ", ""), (icon, color), (" ", ""), (message, text_color)]))
        if not events and available:
            rows.append(pad([("Поки без подій", self.DIM)]))
        while len(rows) < height - 1:
            rows.append([])
        rows = rows[:max(0, height - 1)] + [footer]
        return [(self._row(segments, columns - 1), index == link_row) for index, segments in enumerate(rows[:height])]

    def render(self):
        with self.lock:
            try:
                data = self.provider()
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            size = self.terminal_size()
            height = max(1, size.lines)
            frame = []
            if size != self._painted_size:
                self._painted_rows.clear()
            painted = {}
            # Кожен рядок позиціонується явно: без LF/CR і автоперенесення
            # екран не прокручується навіть при зміні розміру чи широких символах.
            for index, (text, is_link) in enumerate(self._layout(data, max(2, size.columns), height)):
                if is_link and not self.native_mac_links:
                    # OSC 8 робить адресу посиланням; сам URL лишається видимим текстом.
                    text = f"\x1b]8;;{self.admin_url}\x1b\\" + text + "\x1b]8;;\x1b\\"
                painted[index] = text
                if self._painted_rows.get(index) != text:
                    frame.append(f"\x1b[{index + 1};1H" + text + "\x1b[0m\x1b[K")
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
    if source == "ngrok" and re.search(r"\blvl=(eror|error|crit)\b|ERR_NGROK_\d+", line):
        lower = line.lower()
        if any(word in lower for word in ("i/o timeout", "deadline exceeded", "timed out")):
            return "ngrok: перевищено час підключення до сервера. Перевірте інтернет або VPN і повторіть запуск.", logging.ERROR
        if any(word in lower for word in ("no such host", "name resolution")):
            return "ngrok: не вдалося визначити адресу сервера (DNS). Перевірте мережу.", logging.ERROR
        if any(word in lower for word in ("authtoken", "authentication", "err_ngrok_4018")):
            return "ngrok: помилка авторизації. Перевірте налаштування ngrok; докладно в launcher.log.", logging.ERROR
        return "ngrok: не вдалося встановити тунель. Причина та код помилки — у launcher.log.", logging.ERROR
    # Third-party progress bars, blank lines and traceback continuations are diagnostics.
    match = re.match(r"(?:INFO|WARNING|ERROR|CRITICAL):\s+(.+)", line.strip())
    if match and line.strip().startswith(("WARNING:", "ERROR:", "CRITICAL:")):
        level = logging.WARNING if line.strip().startswith("WARNING:") else logging.ERROR
        return f"{source}: {match[1]}", level
    return None
