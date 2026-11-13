from unittest.mock import patch

from core.vad import VADSegmenter


def make_segmenter(**overrides):
    settings = {
        "sample_rate": 16000,
        "frame_ms": 30,
        "vad_level": 2,
        "preroll_ms": 60,
        "silence_end_ms": 60,
        "min_speech_ms": 30,
        "max_segment_s": 20,
    }
    settings.update(overrides)

    with patch("core.vad.webrtcvad.Vad") as vad_class:
        vad_class.return_value.is_speech.side_effect = (
            lambda frame, sample_rate: frame == b"S"
        )
        segmenter = VADSegmenter(**settings)

    return segmenter


def test_preroll_is_included_in_segment():
    segmenter = make_segmenter()

    assert segmenter.process(b"P1") is None
    assert segmenter.process(b"P2") is None
    assert segmenter.process(b"S") is None
    assert segmenter.process(b"X") is None

    result = segmenter.process(b"X")

    assert result == b"P2SXX"


def test_short_speech_is_discarded():
    segmenter = make_segmenter(
        preroll_ms=0,
        min_speech_ms=60,
    )

    assert segmenter.process(b"S") is None
    assert segmenter.process(b"X") is None

    result = segmenter.process(b"X")

    assert result is None
    assert segmenter.in_speech is False


def test_maximum_segment_length():
    segmenter = make_segmenter(
        frame_ms=500,
        preroll_ms=500,
        silence_end_ms=2000,
        min_speech_ms=500,
        max_segment_s=1,
    )

    assert segmenter.process(b"S") is None

    result = segmenter.process(b"S")

    assert result == b"SS"
    assert segmenter.in_speech is False


def test_segmenter_can_process_next_segment():
    segmenter = make_segmenter()

    segmenter.process(b"S")
    segmenter.process(b"X")
    first = segmenter.process(b"X")

    assert first == b"SXX"

    segmenter.process(b"S")
    segmenter.process(b"X")
    second = segmenter.process(b"X")

    assert second == b"SXX"
