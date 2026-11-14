import logging
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from callsigns import Callsigns
from core.transcriber_factory import build_transcriber
from services.event_store import init_db as init_event_db
from services.word_store import init_word_db, save_word_feedback


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "runtime.db"
    init_event_db(path)
    init_word_db(path)
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

    callsigns = Callsigns.from_lines([
        "липка: 700 720",
        "наган: 110 112",
        "ліра: 68",
    ])

    empty = Callsigns.from_lines([])

    bot_state = SimpleNamespace(
        lock=threading.Lock(),
        get_notify_mode=lambda: "all",
    )

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
        bot_state=bot_state,
        log=logging.getLogger("test_runtime_words"),
    )


def teach_lipka(db_path):
    save_word_feedback(
        db_path, "db:1",
        "Улыбка 700", "Липка 700",
    )
    save_word_feedback(
        db_path, "db:2",
        "Улыбка 720", "Липка 720",
    )


def test_runtime_uses_word_rules(db_path):
    teach_lipka(db_path)
    worker = make_worker(db_path)

    assert worker.process_text(
        "Улыбка 700, на связи"
    ) == "Липка 700, на связи"


def test_invalid_number_is_unchanged(db_path):
    teach_lipka(db_path)
    worker = make_worker(db_path)

    assert worker.process_text(
        "Улыбка 999"
    ) == "Улыбка 999"


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
        "Улыбка 700"
    ) == "Липка 700"

    llm.assert_called_once_with("Липка 700")


def test_qwen_works_without_word_rule(db_path, monkeypatch):
    llm = Mock(return_value="Ліра 68")

    monkeypatch.setattr(
        "core.transcriber_factory.build_llm_corrector",
        lambda *args: llm,
    )

    worker = make_worker(db_path)

    assert worker.process_text("Лира 68") == "Ліра 68"
    llm.assert_called_once_with("Лира 68")


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

    llm = Mock(return_value="Липка 700, я Ліра 68")

    monkeypatch.setattr(
        "core.transcriber_factory.build_llm_corrector",
        lambda *args: llm,
    )

    worker = make_worker(db_path)

    assert worker.process_text(
        "Улыбка 700, я Лира 68"
    ) == "Липка 700, я Ліра 68"

    llm.assert_called_once_with(
        "Липка 700, я Лира 68"
    )




@pytest.mark.parametrize(
    "original, proposed",
    [
        ("Рухаємося на північ", "Рухаємося на південь"),
        ("Липка 700, все чисто", "Липка 700, все тихо"),
        ("Липка 700, я тут", "Липка 700, я там"),
        ("Липка 700, я на свези",
         "Липка 700, я на зв'язку"),
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

    llm = Mock(return_value="Липка 700, я там")

    monkeypatch.setattr(
        "core.transcriber_factory.build_llm_corrector",
        lambda *args: llm,
    )

    worker = make_worker(db_path)

    assert worker.process_text(
        "Улыбка 700, я тут"
    ) == "Липка 700, я тут"

    llm.assert_called_once_with("Липка 700, я тут")
