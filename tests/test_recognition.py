from unittest.mock import Mock

import numpy as np

from core.recognition import WhisperRecognizer


def make_recognizer():
    log = Mock()
    recognizer = WhisperRecognizer(
        model_repo="test-model",
        language="ru",
        sample_rate=16000,
        prompt="позывные, команды",
        log=log,
    )
    recognizer.transcribe_fn = Mock(return_value={"segments": []})
    return recognizer, log


def test_warmup_uses_silence_and_model_settings():
    recognizer, log = make_recognizer()

    recognizer.warmup()

    args, kwargs = recognizer.transcribe_fn.call_args

    assert len(args) == 1
    assert isinstance(args[0], np.ndarray)
    assert args[0].dtype == np.float32
    assert len(args[0]) == 16000
    assert np.all(args[0] == 0)
    assert kwargs == {
        "path_or_hf_repo": "test-model",
        "language": "ru",
    }
    log.info.assert_called_once_with("Загружаю Whisper (%s)...", "test-model")


def test_transcribe_preserves_original_parameters():
    recognizer, _ = make_recognizer()
    audio = np.array([0.1, -0.1], dtype=np.float32)

    recognizer.transcribe(audio)

    args, kwargs = recognizer.transcribe_fn.call_args

    assert args[0] is audio
    assert kwargs == {
        "path_or_hf_repo": "test-model",
        "language": "ru",
        "initial_prompt": "позывные, команды",
        "temperature": (0.0, 0.2, 0.4),
        "condition_on_previous_text": False,
        "no_speech_threshold": 0.6,
        "logprob_threshold": -1.0,
        "compression_ratio_threshold": 2.4,
    }


def test_extract_text_filters_unreliable_segments():
    result = {
        "segments": [
            {
                "text": " Первый фрагмент ",
                "no_speech_prob": 0.1,
                "avg_logprob": -0.2,
                "compression_ratio": 1.0,
            },
            {
                "text": "Ложный фрагмент",
                "no_speech_prob": 0.9,
                "avg_logprob": -1.0,
                "compression_ratio": 1.0,
            },
            {
                "text": "Сжатый мусор",
                "no_speech_prob": 0.1,
                "avg_logprob": -0.2,
                "compression_ratio": 2.5,
            },
            {
                "text": " Второй фрагмент ",
                "no_speech_prob": 0.7,
                "avg_logprob": -0.5,
                "compression_ratio": 2.4,
            },
        ]
    }

    assert WhisperRecognizer.extract_text(result) == (
        "Первый фрагмент Второй фрагмент"
    )


def test_extract_text_handles_empty_result():
    assert WhisperRecognizer.extract_text({}) == ""
    assert WhisperRecognizer.extract_text({"segments": []}) == ""


def test_extract_text_without_confidence_fields():
    """Отсутствующая метаинформация не вызывает KeyError."""

    result = {
        "segments": [
            {"text": "Ліра 68"},
            {
                "text": "Наган 110",
                "no_speech_prob": 0.9,
            },
            {
                "text": "Буря 011",
                "avg_logprob": -1.5,
            },
        ]
    }

    assert WhisperRecognizer.extract_text(result) == (
        "Ліра 68 Наган 110 Буря 011"
    )


def test_extract_text_still_filters_unreliable_speech():
    """При наличии оценок сохраняется прежняя фильтрация."""

    result = {
        "segments": [
            {
                "text": "Помилковий текст",
                "no_speech_prob": 0.95,
                "avg_logprob": -1.2,
            },
            {
                "text": "Липка 700",
                "no_speech_prob": 0.1,
                "avg_logprob": -0.2,
            },
        ]
    }

    assert WhisperRecognizer.extract_text(result) == (
        "Липка 700"
    )
