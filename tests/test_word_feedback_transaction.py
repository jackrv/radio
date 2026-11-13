import pytest

from services.sqlite_connection import get_db
from services.word_store import (
    init_word_db,
    save_word_feedback,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "transaction.db"
    init_word_db(path)
    return path


def test_feedback_inside_existing_transaction(db_path):
    with get_db(db_path) as db:
        count = save_word_feedback(
            db_path,
            "db:1",
            "На ган 110",
            "Наган 110",
            db=db,
        )

        assert count == 1

        result = db.execute("""
            SELECT confirmations
            FROM word_corrections
            WHERE original = 'на ган'
              AND corrected = 'наган'
        """).fetchone()

        assert result == (1,)

    with get_db(db_path) as db:
        assert db.execute("""
            SELECT COUNT(*)
            FROM word_correction_votes
        """).fetchone()[0] == 1


def test_outer_transaction_rollback(db_path):
    with pytest.raises(RuntimeError):
        with get_db(db_path) as db:
            save_word_feedback(
                db_path,
                "db:1",
                "На ган 110",
                "Наган 110",
                db=db,
            )
            raise RuntimeError("Тестовый откат")

    with get_db(db_path) as db:
        assert db.execute("""
            SELECT COUNT(*)
            FROM word_correction_votes
        """).fetchone()[0] == 0

        assert db.execute("""
            SELECT COUNT(*)
            FROM word_corrections
        """).fetchone()[0] == 0
