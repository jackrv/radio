"""Короткий текст із низькою впевненістю Whisper відкидається до корекції й подій."""

from unittest.mock import Mock

import numpy as np

import config
from callsigns import Callsigns, command_dictionary
from core.analyzer import is_low_confidence_short
from core.transcriber import Transcriber

CALLSIGNS = Callsigns.from_lines(["дуб: 42", "клен"])
COMMANDS = command_dictionary(["грім", "злива 4242"])


def low(score=-0.9):
    return [{"avg_logprob": score}]


def check(text, segments):
    return is_low_confidence_short(text, segments, CALLSIGNS, COMMANDS, max_words=2, logprob=-0.7)


def test_short_unsure_text_is_dropped():
    assert check("Дякую!", low())
    assert check("Клен", low()), "позивний без номера сам по собі не захищає"


def test_confident_or_longer_text_stays():
    assert not check("Дякую!", low(-0.5))
    assert not check("Дякую за перегляд", low())
    assert not check("Дякую", [{"avg_logprob": -0.5}, {"avg_logprob": -0.4}])


def test_lowest_segment_counts():
    assert check("Дякую", [{"avg_logprob": -0.5}, {"avg_logprob": -0.8}])


def test_command_or_numbered_callsign_protects():
    assert not check("Дуб 42", low())
    assert not check("Грім", low())
    assert not check("Злива 4242", low())


def test_without_scores_rule_does_not_apply():
    assert not check("Дякую", [])
    assert not check("Дякую", [{"avg_logprob": None}])
    assert not check("", low())


def test_config_defaults():
    assert (config.LOW_CONFIDENCE_MAX_WORDS, config.LOW_CONFIDENCE_LOGPROB) == (2, -0.7)


def make_worker(text, score):
    recognizer = Mock()
    recognizer.transcribe.return_value = {"segments": [{"text": text, "avg_logprob": score}]}
    recognizer.extract_text.return_value = text
    processor = Mock(callsigns=CALLSIGNS, commands=COMMANDS)
    process_text = Mock(side_effect=lambda value: value)
    worker = Transcriber(segments=Mock(), recognizer=recognizer, process_text=process_text,
                         event_processor=processor, sample_rate=16000, min_rms=0.02, normalize_audio=True,
                         debug_rms=False, blacklist=(), log=Mock())
    worker.low_confidence = {"max_words": 2, "logprob": -0.7}
    return worker, processor, process_text


def loud():
    return np.full(16000, 10000, dtype=np.int16).tobytes()


def test_receiver_drops_unsure_short_text_before_correction():
    worker, processor, process_text = make_worker("Дякую!", -0.9)
    worker.process_segment(loud())
    process_text.assert_not_called()
    processor.process.assert_not_called()


def test_receiver_keeps_unsure_numbered_callsign():
    worker, processor, _ = make_worker("Дуб 42", -0.9)
    worker.process_segment(loud())
    processor.process.assert_called_once()


def test_rule_is_off_unless_configured():
    worker, processor, _ = make_worker("Дякую!", -0.9)
    worker.low_confidence = None
    worker.process_segment(loud())
    processor.process.assert_called_once()
