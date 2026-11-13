from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

import config
import core.audio_debug as audio_debug
import core.log_security as log_security
import core.transcriber_factory as factory


def test_project_paths_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    root = Path(config.__file__).resolve().parent

    assert config.BASE_DIR == root
    assert config.DB_PATH == str(root / "events.db")
    assert config.LOG_PATH == root / "radio.log"
    assert config.DEBUG_AUDIO_DIR == root / "debug_audio"
    assert Path(config.DB_PATH).is_absolute()


def test_logging_uses_project_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    handler = Mock()
    factory = Mock(return_value=handler)

    monkeypatch.setattr(
        log_security,
        "RotatingFileHandler",
        factory,
    )

    monkeypatch.setattr(
        log_security.logging,
        "basicConfig",
        Mock(),
    )

    log_security.configure_logging("FAKE_TOKEN")

    factory.assert_called_once_with(
        str(config.LOG_PATH),
        maxBytes=log_security.LOG_MAX_BYTES,
        backupCount=log_security.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )

    assert handler.namer is log_security.archive_log_name



def test_debug_audio_uses_configured_directory(
    tmp_path, monkeypatch
):
    folder = tmp_path / "project" / "debug_audio"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        audio_debug,
        "DEBUG_AUDIO_DIR",
        folder,
    )

    pcm = np.full(
        16000, 1000, dtype=np.int16
    ).tobytes()

    audio = np.full(
        16000, 0.1, dtype=np.float32
    )

    paths = audio_debug.save_debug_audio(
        pcm, audio, 16000, 1
    )

    assert len(paths) == 3
    assert all(p.parent == folder for p in paths)
    assert all(p.is_file() for p in paths)


def test_factory_uses_configured_db_for_corrections(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    db_path = str(tmp_path / "project" / "events.db")

    # Берём все настройки из настоящего config.py.
    settings = SimpleNamespace(**{
        name: getattr(config, name)
        for name in dir(config)
        if name.isupper()
    })

    settings.DB_PATH = db_path
    settings.USE_LLM = False
    settings.FUZZY_CORRECTION_SCORE = 93

    selected = {}

    def capture_pipeline(**kwargs):
        selected["corrections"] = kwargs[
            "apply_learned_corrections"
        ]
        return lambda text: text

    monkeypatch.setattr(
        factory,
        "build_llm_corrector",
        lambda *args: None,
    )

    monkeypatch.setattr(
        factory,
        "build_text_pipeline",
        capture_pipeline,
    )

    monkeypatch.setattr(
        factory,
        "WhisperRecognizer",
        Mock(),
    )

    monkeypatch.setattr(
        factory,
        "Transcriber",
        Mock(),
    )

    # Все аргументы указаны по именам.
    # Ошибка с лишним Mock() теперь исключена.
    factory.build_transcriber(
        config=settings,
        segments=Mock(),
        callsigns=Mock(),
        commands=Mock(),
        watch=Mock(),
        prompt="prompt",
        telegram_service=Mock(),
        cooldown=Mock(),
        bot_state=Mock(),
        log=Mock(),
    )

    corrector = selected["corrections"]

    assert isinstance(corrector, partial)
    assert corrector.keywords["db_path"] == db_path
    assert corrector.keywords["fuzzy_score"] == 93
