from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import config as project_config
from core.transcriber_factory import build_transcriber

config = None


@pytest.fixture(autouse=True)
def temporary_config(tmp_path):
    """Копія налаштувань із тимчасовою базою: фабрика читає й створює таблиці."""
    global config
    values = {k: getattr(project_config, k) for k in dir(project_config) if k.isupper()}
    config = SimpleNamespace(**{**values, "DB_PATH": str(tmp_path / "events.db")})
    yield
    config = None


def make_dependencies():
    return {
        "config": config,
        "segments": Mock(),
        "callsigns": Mock(),
        "commands": Mock(),
        "watch": Mock(),
        "prompt": "Вишня, Кобза, Буря",
        "telegram_service": Mock(),
        "cooldown": Mock(),
        "log": Mock(),
    }


@patch("core.transcriber_factory.Transcriber")
@patch("core.transcriber_factory.WhisperRecognizer")
@patch("core.transcriber_factory.EventProcessor")
@patch("core.transcriber_factory.build_text_pipeline")
@patch("core.transcriber_factory.build_llm_corrector")
def test_build_transcriber(
    mock_llm,
    mock_pipeline,
    mock_event_processor,
    mock_whisper,
    mock_transcriber,
):
    deps = make_dependencies()

    worker = build_transcriber(**deps)

    assert worker is mock_transcriber.return_value

    mock_llm.assert_called_once()
    use_llm, repo, live_callsigns, live_commands, log = mock_llm.call_args.args
    assert (use_llm, repo, log) == (config.USE_LLM, config.LLM_REPO, deps["log"])
    # Живі словники: об’єкти-посередники над поточним знімком.
    assert live_callsigns._current is deps["callsigns"]
    assert live_commands._current is deps["commands"]

    mock_whisper.assert_called_once_with(
        model_repo=config.MODEL_REPO,
        language=config.LANGUAGE,
        sample_rate=config.SR,
        prompt=deps["prompt"],
        log=deps["log"],
    )

    mock_event_processor.assert_called_once()
    event_kwargs = mock_event_processor.call_args.kwargs

    assert event_kwargs["telegram_service"] is deps["telegram_service"]
    assert event_kwargs["db_path"] == config.DB_PATH
    assert event_kwargs["fresh"] == deps["cooldown"].fresh
    assert event_kwargs["select_recipients"] is deps["telegram_service"].recipients_for

    mock_pipeline.assert_called_once()
    assert tuple(d._current for d in mock_pipeline.call_args.kwargs["dictionaries"]) == (
        deps["callsigns"],
        deps["commands"],
        deps["watch"],
    )
    assert event_kwargs["callsigns"] is mock_pipeline.call_args.kwargs["dictionaries"][0]

    mock_transcriber.assert_called_once()
    worker_kwargs = mock_transcriber.call_args.kwargs

    assert worker_kwargs["recognizer"] is mock_whisper.return_value
    assert worker_kwargs["process_text"] is mock_pipeline.return_value
    assert worker_kwargs["event_processor"] is mock_event_processor.return_value

    mock_whisper.return_value.warmup.assert_not_called()
    mock_transcriber.return_value.run.assert_not_called()


def test_factory_does_not_start_models():
    deps = make_dependencies()

    with (
        patch("core.transcriber_factory.WhisperRecognizer") as whisper,
        patch("core.transcriber_factory.build_llm_corrector") as llm,
        patch("core.transcriber_factory.Transcriber") as worker,
    ):
        build_transcriber(**deps)

        whisper.return_value.warmup.assert_not_called()
        whisper.return_value.transcribe.assert_not_called()
        llm.return_value.assert_not_called()
        worker.return_value.run.assert_not_called()
