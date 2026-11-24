from unittest.mock import Mock

import numpy as np

from server.transcriber import Transcriber


def make_transcriber(text="Вишня 700", blacklist=()):
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
    process_text.assert_called_once_with("Вишня 700")
    processor.process.assert_called_once_with(
        "Исправлено: Вишня 700", pcm, raw_text="Вишня 700"
    )


def test_daily_code_formatter_runs_after_the_recognition_pipeline():
    worker, _, _, processor = make_transcriber("Береза 68, 48, 62, прийом")
    formatted = []

    def remove_confirmed_comma(text, recorded_at):
        formatted.append((text, recorded_at))
        return text.replace("48, 62", "48 62")

    worker.daily_code_formatter = remove_confirmed_comma
    pcm = make_pcm()
    worker.process_segment(pcm)

    assert formatted[0][0] == "Исправлено: Береза 68, 48, 62, прийом"
    assert formatted[0][1] > 0
    processor.process.assert_called_once_with(
        "Исправлено: Береза 68, 48 62, прийом", pcm, raw_text="Береза 68, 48, 62, прийом"
    )


def test_deferred_recognition_uses_the_original_recording_time_for_codes():
    worker, _, _, processor = make_transcriber("Береза 68, 48, 62, прийом")
    worker.deferred = Mock()
    worker.recorder = Mock()
    worker.deferred.created_at.return_value = 123.0
    formatted = Mock(side_effect=lambda text, recorded_at: text.replace("48, 62", "48 62"))
    worker.daily_code_formatter = formatted
    worker._dry = True

    worker.process_segment(make_pcm(), deferred_id="record-id")

    formatted.assert_called_once_with("Исправлено: Береза 68, 48, 62, прийом", 123.0)
    finish_args = worker.deferred.finish_deferred.call_args.args
    assert finish_args[0] == "record-id"
    assert finish_args[1]["final_text"] == "Исправлено: Береза 68, 48 62, прийом"
    processor.process.assert_not_called()


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
        {"text": "Вишня 700"},
    ]

    # Первый сегмент вызывает ошибку Whisper.
    # Исключение не должно выходить из process_segment().
    worker.process_segment(pcm)

    # Следующий сегмент должен обработаться нормально.
    worker.process_segment(pcm)

    assert recognizer.transcribe.call_count == 2

    process_text.assert_called_once_with("Вишня 700")

    processor.process.assert_called_once_with(
        "Исправлено: Вишня 700", pcm, raw_text="Вишня 700"
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
        "Вишня 700", pcm, raw_text="Вишня 700"
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
        "Исправлено: Вишня 700", pcm, raw_text="Вишня 700"
    )

    worker.log.exception.assert_called_once()
