import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import os
from core.console import ConsoleDashboard

from config import LOG_PATH

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5


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


def configure_logging(token):
    formatter = RedactingFormatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        "%Y-%m-%d %H:%M:%S",
        token,
    )

    handlers = [
        RotatingFileHandler(
            str(LOG_PATH),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        ),
        ConsoleDashboard(provider=lambda: __import__("core.diagnostics", fromlist=["runtime"]).runtime.snapshot()),
    ]

    handlers[0].namer = archive_log_name

    for handler in handlers:
        handler.setFormatter(formatter)
    handlers[1].setFormatter(RedactingFormatter("%(message)s", None, token))
    handlers[0].setFormatter(RedactingFormatter(
        "%(asctime)s [%(levelname)s] %(name)s [%(threadName)s] %(message)s",
        "%Y-%m-%d %H:%M:%S", token,
    ))
    if not os.environ.get("RADIO345_MANAGED_CONSOLE"):
        handlers[1].start()

    logging.basicConfig(
        level=logging.INFO,
        handlers=handlers,
        force=True,
    )
