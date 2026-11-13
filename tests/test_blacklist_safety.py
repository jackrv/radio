from unittest.mock import Mock

import pytest

import core.transcriber as transcriber_module
from core.transcriber import Transcriber
from config import BLACKLIST


def make_worker(text):
    recognizer = Mock()
    recognizer.transcribe.return_value = {"text": text}
    recognizer.extract_text.return_value = text

    processor = Mock()

    worker = Transcriber(
        segments=Mock(),
        recognizer=recognizer,
        process_text=lambda value: value,
        event_processor=processor,
        sample_rate=16000,
        min_rms=0.02,
        normalize_audio=True,
        debug_rms=False,
        blacklist=BLACKLIST,
        log=Mock(),
    )

    return worker, processor


@pytest.mark.parametrize("speech", [
    "Ліра 68, слабо чую, прийом",
    "Липка 700, дякую, прийняв",
    "Щит, слабо чую",
    "Ліра 68, команди виконано",
    "Сапсан, папа тут",
    "Ліра 68, спасибо за просмотр, прийом",
])
def test_useful_radio_messages_are_not_blacklisted(
    speech, monkeypatch
):
    monkeypatch.setattr(
        transcriber_module,
        "looks_like_garbage",
        lambda *_: False,
    )

    worker, processor = make_worker(speech)
    pcm = (10000).to_bytes(2, "little", signed=True) * 16000

    worker.process_segment(pcm)

    processor.process.assert_called_once_with(speech, pcm, raw_text=speech)


@pytest.mark.parametrize("phrase", [
    "Спасибо за просмотр!",
    "Дякую за перегляд.",
    "Редактор субтитров",
    "Продолжение следует",
])
def test_whisper_template_phrases_are_filtered(
    phrase, monkeypatch
):
    monkeypatch.setattr(
        transcriber_module,
        "looks_like_garbage",
        lambda *_: False,
    )

    worker, processor = make_worker(phrase)
    pcm = (10000).to_bytes(2, "little", signed=True) * 16000

    worker.process_segment(pcm)

    processor.process.assert_not_called()


def test_normal_radio_words_not_in_blacklist():
    for word in (
        "слабо",
        "дякую",
        "папа",
        "команди",
        "позивні",
    ):
        assert word not in BLACKLIST
