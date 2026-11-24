from unittest.mock import Mock

import pytest

import core.transcriber as transcriber_module
from core.transcriber import Transcriber
from shared.default_text_lists import enabled

BLACKLIST = enabled("blacklist")


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
    "Віра 68, слабо чую, прийом",
    "Вишня 700, дякую, прийняв",
    "Дуб, слабо чую",
    "Віра 68, команди виконано",
    "Ясень, папа тут",
    "Віра 68, спасибо за просмотр, прийом",
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


def test_live_blacklist_is_archived_and_trace_does_not_leak_to_next_segment():
    from types import SimpleNamespace
    worker, processor = make_worker('Папа')
    worker.recorder = Mock()
    worker.text_lists = SimpleNamespace(refresh=Mock(), blacklist={'папа'})
    pcm = (10000).to_bytes(2, 'little', signed=True) * 16000
    worker.process_segment(pcm)
    trace = worker.recorder.submit.call_args.args[2]
    assert trace['status'] == 'blacklist'
    processor.process.assert_not_called()
    # A subsequent unrecognised quiet fragment must not inherit the earlier flag.
    worker.process_segment(b'\0' * 32000)
    trace = worker.recorder.submit.call_args.args[2]
    assert trace['status'] == 'low_rms'
    assert 'raw_text' not in trace


def test_repeated_blacklisted_phrase_is_filtered_but_mixed_speech_is_not():
    from types import SimpleNamespace
    worker, processor = make_worker("")
    worker.text_lists = SimpleNamespace(refresh=Mock(), blacklist={"дякую за перегляд"})
    assert worker._is_blacklisted("Дякую за перегляд! Дякую за перегляд! Дякую за перегляд!")
    assert not worker._is_blacklisted("Дякую за перегляд! Клен 15, прийом.")
    assert not worker._is_blacklisted("Клен 15, дякую за перегляд, прийом")
