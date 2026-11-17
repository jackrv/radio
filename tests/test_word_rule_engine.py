import pytest

from callsigns import Callsigns
from services.word_store import (
    init_word_db,
    save_word_feedback,
)
from core.word_rule_engine import apply_verified_word_rules


@pytest.fixture
def environment(tmp_path):
    db_path = tmp_path / "rules.db"
    init_word_db(db_path)

    callsigns = Callsigns.from_lines([
        "калина: 700 720",
        "набат: 110 112",
        "ліна: 68",
    ])

    commands = Callsigns.from_lines([])
    watch = Callsigns.from_lines([])

    return db_path, (callsigns, commands, watch)


def teach(db_path, source_id, old, new):
    save_word_feedback(db_path, source_id, old, new)


def test_active_rule_and_valid_number(environment):
    path, dictionaries = environment

    teach(path, "db:1", "Калиниця 700", "Калина 700")
    teach(path, "db:2", "Калиниця 720", "Калина 720")

    assert apply_verified_word_rules(
        "Калиниця 700, я на связи",
        path,
        dictionaries,
    ) == "Калина 700, я на связи"


def test_invalid_number_is_not_replaced(environment):
    path, dictionaries = environment

    teach(path, "db:1", "Калиниця 700", "Калина 700")
    teach(path, "db:2", "Калиниця 720", "Калина 720")

    assert apply_verified_word_rules(
        "Калиниця 999",
        path,
        dictionaries,
    ) == "Калиниця 999"


def test_single_confirmation_is_not_enough(environment):
    path, dictionaries = environment

    teach(path, "db:1", "На бат 110", "Набат 110")

    assert apply_verified_word_rules(
        "На бат 112",
        path,
        dictionaries,
    ) == "На бат 112"


def test_unknown_target_is_not_applied(environment):
    path, dictionaries = environment

    teach(path, "db:1", "Помеха 700", "Случайное 700")
    teach(path, "db:2", "Помеха 720", "Случайное 720")

    assert apply_verified_word_rules(
        "Помеха 700",
        path,
        dictionaries,
    ) == "Помеха 700"


def test_each_number_is_checked_separately(environment):
    path, dictionaries = environment

    teach(path, "db:1", "Калиниця 700", "Калина 700")
    teach(path, "db:2", "Калиниця 720", "Калина 720")

    assert apply_verified_word_rules(
        "Калиниця 700, калиниця 999",
        path,
        dictionaries,
    ) == "Калина 700, калиниця 999"
