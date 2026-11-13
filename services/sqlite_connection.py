"""Единое SQLite-подключение Radio 345."""

import sqlite3
from contextlib import contextmanager

from config import DB_PATH


@contextmanager
def get_db(db_path=DB_PATH):
    """Открывает соединение с управляемой транзакцией."""

    conn = sqlite3.connect(
        str(db_path),
        timeout=15.0,
    )

    try:
        # Устанавливаем ожидание до операций с журналом.
        conn.execute("PRAGMA busy_timeout=10000")

        # WAL позволяет чтение во время записи.
        conn.execute("PRAGMA journal_mode=WAL")

        yield conn

        conn.commit()

    except BaseException:
        conn.rollback()
        raise

    finally:
        conn.close()
