import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import os
import copy
import re
from server.console import ConsoleDashboard
from shared.private_files import private_open, protect_existing

from config import LOG_PATH

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if "a" in self.mode else os.O_TRUNC)
        return os.fdopen(private_open(self.baseFilename, flags), self.mode,
                         encoding=self.encoding, errors=self.errors)


class TechnicalFileFormatter(logging.Formatter):
    """Зміст залишається на консолі; файл отримує лише назву етапу."""
    CONTENT = re.compile(r"Розпізнано \([^)]*\):|Виправлено за навчанням \([^)]*\):|"
                         r"Відкинуто: лише позивний[^:]*:|Відкинуто короткий хибний фрагмент:|"
                         r"Відкинуто: перелік назв[^:]*:|Прибрано перелік назв[^:]*:|"
                         r"Відкинуто: короткий текст[^:]*:|-> БД #\d+|-> черга Telegram")

    def __init__(self, fmt, datefmt, token):
        super().__init__(fmt, datefmt)
        self.token = token

    def format(self, record):
        message = record.getMessage()
        match = self.CONTENT.search(message)
        if match:
            record = copy.copy(record)
            prefix=message[:match.start()]
            if 'RADIO345_EVENT' in prefix: prefix=prefix.split('RADIO345_EVENT',1)[0]
            record.msg = prefix + match.group() + " [зміст лише в консолі]"
            record.args = ()
        return RedactingFormatter(self._style._fmt, self.datefmt, self.token).format(record)


def archive_log_name(default_name):
    """radio.log.1 -> radio-1.log"""
    path = Path(default_name)
    number = path.suffix.removeprefix(".")

    if not number.isdigit():
        return default_name

    base = path.with_suffix("")

    return str(
        base.with_name(
            f"{base.stem}-{number}{base.suffix}"
        )
    )



class RedactingFormatter(logging.Formatter):
    """Скрывает Telegram-токен в сообщениях и traceback."""

    def __init__(self, fmt, datefmt, token):
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.token = token

    def format(self, record):
        result = super().format(record)

        if self.token:
            result = result.replace(self.token, "[REDACTED]")

        return result


def configure_logging(token, starting_message=None):
    formatter = RedactingFormatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        "%Y-%m-%d %H:%M:%S",
        token,
    )

    handlers = [
        PrivateRotatingFileHandler(
            str(LOG_PATH),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        ),
        ConsoleDashboard(provider=lambda: __import__("shared.diagnostics", fromlist=["runtime"]).runtime.snapshot(),
                         starting_message=starting_message),
    ]

    handlers[0].namer = archive_log_name

    for handler in handlers:
        handler.setFormatter(formatter)
    handlers[1].setFormatter(RedactingFormatter("%(message)s", None, token))
    handlers[0].setFormatter(TechnicalFileFormatter(
        "%(asctime)s [%(levelname)s] %(name)s [%(threadName)s] %(message)s",
        "%Y-%m-%d %H:%M:%S", token,
    ))
    for number in range(1, LOG_BACKUP_COUNT + 1):
        protect_existing(archive_log_name(str(LOG_PATH) + f".{number}"))
    if not os.environ.get("RADIO345_MANAGED_CONSOLE"):
        handlers[1].start()

    logging.basicConfig(
        level=logging.INFO,
        handlers=handlers,
        force=True,
    )
