import hashlib
import sqlite3
import time

import callsigns as callsign_module
from callsigns import Callsigns, command_dictionary
from database import DICTIONARY_TYPES, init_db
from services.sqlite_connection import get_db

REFRESH_INTERVAL_S = 2.0


def build_prompt(callsigns, commands, watch):
    """Підказка Whisper: усі відомі назви словників."""
    return ", ".join(sorted(set(callsigns.names) | set(commands.names) | set(watch.names)))


def dictionaries_from_lines(lines):
    """Єдиний конструктор словників; не читає й не змінює базу."""
    return (Callsigns.from_lines(lines.get("callsigns", ())),
            command_dictionary(lines.get("commands", ())),
            Callsigns.from_lines(lines.get("telegram", ())))


def read_dictionary_lines(db):
    """Лише SELECT: придатний і для read-only знімка лабораторії."""
    lines = {kind: [] for kind in DICTIONARY_TYPES}
    for kind, line in db.execute("""SELECT dictionary_type, raw_line FROM dictionary_entries
            WHERE enabled = 1 ORDER BY dictionary_type, line_number"""):
        lines.setdefault(kind, []).append(line)
    return lines


def load_dictionaries(db_path, log):
    """Загружает словари из SQLite и формирует prompt для Whisper."""

    init_db(db_path)

    with get_db(db_path) as db:
        callsigns, commands, watch = dictionaries_from_lines(read_dictionary_lines(db))

    if not callsigns.names:
        message = (
            "Словник позивних порожній. Запуск Radio 345 зупинено. "
            "Перевірте dictionary_entries у SQLite "
            "(dictionary_type='callsigns', enabled=1)."
        )
        log.error(message)
        raise RuntimeError(message)

    if not commands.names:
        log.warning(
            "Словник команд порожній: виявлення команд недоступне"
        )

    if not watch.names:
        log.warning(
            "Словник Telegram порожній: список спостереження недоступний"
        )

    prompt = build_prompt(callsigns, commands, watch)

    log.info(
        "Словники завантажено зі SQLite: позивні=%d, команди=%d, Telegram=%d",
        len(callsigns.names),
        len(commands.names),
        len(watch.names),
    )

    return callsigns, commands, watch, prompt


class LiveDictionary:
    """Заміна об’єкта Callsigns, що завжди звертається до поточного знімка словника.

    Споживачі (EventProcessor, правила, перевірка Qwen) отримують цей об’єкт один
    раз і бачать оновлення без перезапуску.
    """

    def __init__(self, current):
        object.__setattr__(self, "_current", current)

    def _swap(self, current):
        object.__setattr__(self, "_current", current)

    def __getattr__(self, name):
        return getattr(self._current, name)


def dictionary_fingerprint(db):
    digest = hashlib.sha256()
    for row in db.execute("""SELECT dictionary_type, line_number, enabled, raw_line
            FROM dictionary_entries ORDER BY dictionary_type, line_number"""):
        digest.update(repr(row).encode())
    return digest.hexdigest()


class LiveDictionaries:
    """Перезавантаження словників із SQLite без перезапуску приймача.

    Перевірка — не частіше ніж раз на ``interval`` секунд (відбиток рядків).
    Порожній словник позивних не застосовується: лишається попередній.
    """

    def __init__(self, db_path, callsigns, commands, watch, prompt, log=None, *,
                 interval=REFRESH_INTERVAL_S, clock=time.monotonic):
        self.db_path = db_path
        self.callsigns = LiveDictionary(callsigns)
        self.commands = LiveDictionary(commands)
        self.watch = LiveDictionary(watch)
        self.prompt = prompt
        self.log = log
        self.interval = interval
        self.clock = clock
        self.revision = None
        self._key = None
        self._checked = None
        self._prompt_listeners = []
        self._warned = None

    @property
    def dictionaries(self):
        return (self.callsigns, self.commands, self.watch)

    def on_prompt(self, callback):
        self._prompt_listeners.append(callback)

    def refresh(self, force=False):
        now = self.clock()
        if not force and self._checked is not None and now - self._checked < self.interval:
            return False
        self._checked = now
        try:
            with get_db(self.db_path) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='dictionary_entries'").fetchone():
                    return False
                fingerprint = dictionary_fingerprint(db)
                # Імена нормалізуються з алиасами: зміна алиасів теж перебудовує словники.
                key = (fingerprint, id(callsign_module.ALIASES))
                if key == self._key and not force:
                    return False
                lines = read_dictionary_lines(db)
        except sqlite3.Error:
            if self.log is not None and self._warned != "db":
                self.log.warning("Словники недоступні; використовую попередні")
            self._warned = "db"
            return False

        callsigns, commands, watch = dictionaries_from_lines(lines)
        self._key = key
        if not callsigns.names:
            if self.log is not None and self._warned != fingerprint:
                self.log.error("Новий словник позивних порожній; приймач лишає попередній словник")
            self._warned = fingerprint
            return False
        self._warned = None
        changed = self.revision is not None and fingerprint != self.revision
        self.callsigns._swap(callsigns)
        self.commands._swap(commands)
        self.watch._swap(watch)
        self.prompt = build_prompt(callsigns, commands, watch)
        self.revision = fingerprint
        for callback in self._prompt_listeners:
            callback(self.prompt)
        try:
            from core.diagnostics import runtime
            runtime.gauge("dictionaries.revision", fingerprint[:12])
            if changed:
                runtime.count("dictionaries.reloaded")
        except Exception:
            pass
        if self.log is not None and changed:
            self.log.info("Словники оновлено без перезапуску: позивні=%d, команди=%d, Telegram=%d",
                          len(callsigns.names), len(commands.names), len(watch.names))
        return True
