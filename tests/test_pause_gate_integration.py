from unittest.mock import Mock

import numpy as np
import pytest

from core.pause_gate import PauseGate
from core.transcriber import Transcriber


def make_worker(gate):
    recognizer = Mock()
    recognizer.transcribe.return_value = {"text": "Тест"}
    recognizer.extract_text.return_value = "Тест"

    processor = Mock()

    worker = Transcriber(
        segments=Mock(),
        recognizer=recognizer,
        process_text=lambda text: text,
        event_processor=processor,
        sample_rate=16000,
        min_rms=0.02,
        normalize_audio=True,
        debug_rms=False,
        blacklist=(),
        log=Mock(),
        pause_gate=gate,
    )

    return worker, recognizer, processor


def make_pcm():
    return np.full(
        16000, 10000, dtype=np.int16
    ).tobytes()


def test_old_generation_is_rejected_after_resume():
    gate = PauseGate()
    worker, recognizer, processor = make_worker(gate)

    old_generation = gate.tag()

    gate.set_paused(True)
    gate.set_paused(False)

    worker.process_segment(
        make_pcm(),
        generation=old_generation,
    )

    recognizer.transcribe.assert_not_called()
    processor.process.assert_not_called()


def test_result_is_discarded_if_paused_during_whisper():
    gate = PauseGate()
    worker, recognizer, processor = make_worker(gate)

    generation = gate.tag()

    def recognize(audio):
        gate.set_paused(True)
        return {"text": "Тест"}

    recognizer.transcribe.side_effect = recognize

    worker.process_segment(
        make_pcm(),
        generation=generation,
    )

    processor.process.assert_not_called()


def test_new_generation_is_processed():
    gate = PauseGate()
    worker, recognizer, processor = make_worker(gate)

    gate.set_paused(True)
    gate.set_paused(False)

    generation = gate.tag()

    worker.process_segment(
        make_pcm(),
        generation=generation,
    )

    recognizer.transcribe.assert_called_once()
    processor.process.assert_called_once()


def test_queue_discards_old_generation():
    gate = PauseGate()
    worker, recognizer, processor = make_worker(gate)

    old_generation = gate.tag()

    gate.set_paused(True)
    gate.set_paused(False)

    new_generation = gate.tag()

    worker.segments.get.side_effect = [
        (old_generation, make_pcm()),
        (new_generation, make_pcm()),
        KeyboardInterrupt(),
    ]

    with pytest.raises(KeyboardInterrupt):
        worker.run()

    recognizer.transcribe.assert_called_once()
    processor.process.assert_called_once()
