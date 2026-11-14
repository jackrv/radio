from unittest.mock import Mock

import numpy as np

from core.transcriber import Transcriber


def make_transcriber(text="Липка 700", blacklist=()):
    recognizer = Mock()
    recognizer.transcribe.return_value = {"text": text}
    recognizer.extract_text.return_value = text

    process_text = Mock(
        side_effect=lambda value: "Исправлено: " + value
    )

    event_processor = Mock()

    transcriber = Transcriber(
        segments=Mock(),
        recognizer=recognizer,
        process_text=process_text,
        event_processor=event_processor,
        sample_rate=16000,
        min_rms=0.02,
        normalize_audio=True,
        debug_rms=False,
        blacklist=blacklist,
        log=Mock(),
    )

    return transcriber, recognizer, process_text, event_processor


def make_pcm(amplitude=10000, duration=1):
    return np.full(
        16000 * duration,
        amplitude,
        dtype=np.int16,
    ).tobytes()


def test_valid_segment():
    worker, recognizer, process_text, processor = make_transcriber()
    pcm = make_pcm()

    worker.process_segment(pcm)

    recognizer.transcribe.assert_called_once()
    process_text.assert_called_once_with("Липка 700")
    processor.process.assert_called_once_with(
        "Исправлено: Липка 700", pcm, raw_text="Липка 700"
    )


def test_silence_is_ignored():
    worker, recognizer, process_text, processor = make_transcriber()

    worker.process_segment(make_pcm(amplitude=0))

    recognizer.transcribe.assert_not_called()
    process_text.assert_not_called()
    processor.process.assert_not_called()


def test_empty_audio_is_ignored():
    worker, recognizer, process_text, processor = make_transcriber()

    worker.process_segment(b"")

    recognizer.transcribe.assert_not_called()
    process_text.assert_not_called()
    processor.process.assert_not_called()


def test_blacklist_is_filtered():
    worker, recognizer, process_text, processor = make_transcriber(
        text="Спасибо за просмотр",
        blacklist=("спасибо за просмотр",),
    )

    worker.process_segment(make_pcm())

    recognizer.transcribe.assert_called_once()
    process_text.assert_not_called()
    processor.process.assert_not_called()


def test_empty_recognition_is_ignored():
    worker, recognizer, process_text, processor = make_transcriber(
        text=""
    )

    worker.process_segment(make_pcm())

    recognizer.transcribe.assert_called_once()
    process_text.assert_not_called()
    processor.process.assert_not_called()


def test_whisper_error_does_not_stop_processing():
    worker, recognizer, process_text, processor = make_transcriber()
    pcm = make_pcm()

    recognizer.transcribe.side_effect = [
        RuntimeError("Whisper temporary error"),
        {"text": "Липка 700"},
    ]

    # Первый сегмент вызывает ошибку Whisper.
    # Исключение не должно выходить из process_segment().
    worker.process_segment(pcm)

    # Следующий сегмент должен обработаться нормально.
    worker.process_segment(pcm)

    assert recognizer.transcribe.call_count == 2

    process_text.assert_called_once_with("Липка 700")

    processor.process.assert_called_once_with(
        "Исправлено: Липка 700", pcm, raw_text="Липка 700"
    )

    worker.log.exception.assert_called_once()


def test_qwen_error_falls_back_to_whisper_text():
    worker, recognizer, process_text, processor = make_transcriber()
    pcm = make_pcm()

    process_text.side_effect = RuntimeError(
        "Qwen temporary error"
    )

    # Помилка коррекции не должна останавливать обработку.
    worker.process_segment(pcm)

    # Сохраняем исходный результат Whisper.
    processor.process.assert_called_once_with(
        "Липка 700", pcm, raw_text="Липка 700"
    )

    # Помилка должна попасть в журнал.
    worker.log.exception.assert_called_once()


def test_event_processor_error_does_not_stop_transcriber():
    worker, recognizer, process_text, processor = make_transcriber()
    pcm = make_pcm()

    processor.process.side_effect = [
        RuntimeError("SQLite or Telegram temporary error"),
        None,
    ]

    # Первый сегмент вызывает ошибку обработки события.
    worker.process_segment(pcm)

    # Второй сегмент должен обработаться нормально.
    worker.process_segment(pcm)

    assert recognizer.transcribe.call_count == 2
    assert processor.process.call_count == 2

    processor.process.assert_called_with(
        "Исправлено: Липка 700", pcm, raw_text="Липка 700"
    )

    worker.log.exception.assert_called_once()
