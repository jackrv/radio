import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from callsigns import Callsigns
from core.transcriber_factory import build_transcriber
from services.event_store import init_db as init_event_db
from services.word_store import init_word_db, save_word_feedback


DICTIONARY = ["вишня: 700 720", "кобза: 110 112", "віра: 68"]


@pytest.fixture
def db_path(tmp_path):
    import sqlite3
    from database import init_db as init_dictionary_db
    path = tmp_path / "runtime.db"
    init_event_db(path)
    init_word_db(path)
    # Словники в тій самій SQLite: за ними класифікуються нові голоси.
    init_dictionary_db(path)
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO dictionary_entries (dictionary_type, line_number, raw_line) VALUES ('callsigns', ?, ?)",
                       list(enumerate(DICTIONARY, 1)))
    return path


def make_worker(db_path):
    config = SimpleNamespace(
        USE_LLM=False,
        LLM_REPO="unused",
        DB_PATH=str(db_path),
        MODEL_REPO="unused",
        LANGUAGE="uk",
        SR=16000,
        SHORT_GARBAGE=set(),
        LOG_CALLSIGNS_ONLY=True,
        MIN_RMS=0.02,
        NORMALIZE=True,
        DEBUG_RMS=False,
        BLACKLIST=(),
    )

    callsigns = Callsigns.from_lines(DICTIONARY)

    empty = Callsigns.from_lines([])

    cooldown = SimpleNamespace(
        fresh=lambda key: True,
        rollback=lambda key: None,
    )

    return build_transcriber(
        config=config,
        segments=Mock(),
        callsigns=callsigns,
        commands=empty,
        watch=empty,
        prompt="",
        telegram_service=Mock(),
        cooldown=cooldown,
        log=logging.getLogger("test_runtime_words"),
    )


def teach_lipka(db_path):
    save_word_feedback(
        db_path, "db:1",
        "Калиниця 700", "Вишня 700",
    )
    save_word_feedback(
        db_path, "db:2",
        "Калиниця 720", "Вишня 720",
    )


def test_runtime_uses_word_rules(db_path):
    teach_lipka(db_path)
    worker = make_worker(db_path)

    assert worker.process_text(
        "Калиниця 700, на связи"
    ) == "Вишня 700, на связи"


def test_invalid_number_is_unchanged(db_path):
    teach_lipka(db_path)
    worker = make_worker(db_path)

    assert worker.process_text(
        "Калиниця 999"
    ) == "Калиниця 999"


def test_word_correction_has_priority_over_qwen(
    db_path, monkeypatch
):
    teach_lipka(db_path)

    llm = Mock(return_value="Неправильный результат")

    monkeypatch.setattr(
        "core.transcriber_factory.build_llm_corrector",
        lambda *args: llm,
    )

    worker = make_worker(db_path)

    assert worker.process_text(
        "Калиниця 700"
    ) == "Вишня 700"

    llm.assert_called_once_with("Вишня 700")


def test_qwen_works_without_word_rule(db_path, monkeypatch):
    llm = Mock(return_value="Віра 68")

    monkeypatch.setattr(
        "core.transcriber_factory.build_llm_corrector",
        lambda *args: llm,
    )

    worker = make_worker(db_path)

    assert worker.process_text("Вира 68") == "Віра 68"
    llm.assert_called_once_with("Вира 68")


def test_qwen_cannot_change_numbers(
    db_path, monkeypatch
):
    monkeypatch.setattr(
        "core.transcriber_factory.build_llm_corrector",
        lambda *args: lambda text: "Сигнал 69",
    )

    worker = make_worker(db_path)

    assert worker.process_text(
        "Сигнал 68"
    ) == "Сигнал 68"


def test_qwen_complements_learned_callsign(
    db_path, monkeypatch
):
    teach_lipka(db_path)

    llm = Mock(return_value="Вишня 700, я Віра 68")

    monkeypatch.setattr(
        "core.transcriber_factory.build_llm_corrector",
        lambda *args: llm,
    )

    worker = make_worker(db_path)

    assert worker.process_text(
        "Калиниця 700, я Вира 68"
    ) == "Вишня 700, я Віра 68"

    llm.assert_called_once_with(
        "Вишня 700, я Вира 68"
    )




@pytest.mark.parametrize(
    "original, proposed",
    [
        ("Рухаємося на північ", "Рухаємося на південь"),
        ("Вишня 700, все чисто", "Вишня 700, все тихо"),
        ("Вишня 700, я тут", "Вишня 700, я там"),
        ("Вишня 700, я на свези",
         "Вишня 700, я на зв'язку"),
    ],
)
def test_unconfirmed_llm_rewrite_is_rejected(
    db_path, monkeypatch, original, proposed
):
    llm = Mock(return_value=proposed)

    monkeypatch.setattr(
        "core.transcriber_factory.build_llm_corrector",
        lambda *args: llm,
    )

    worker = make_worker(db_path)

    assert worker.process_text(original) == original
    llm.assert_called_once_with(original)


def test_rejected_llm_preserves_learned_correction(
    db_path, monkeypatch
):
    teach_lipka(db_path)

    llm = Mock(return_value="Вишня 700, я там")

    monkeypatch.setattr(
        "core.transcriber_factory.build_llm_corrector",
        lambda *args: llm,
    )

    worker = make_worker(db_path)

    assert worker.process_text(
        "Калиниця 700, я тут"
    ) == "Вишня 700, я тут"

    llm.assert_called_once_with("Вишня 700, я тут")
