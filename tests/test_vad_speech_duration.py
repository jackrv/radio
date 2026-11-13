from core.vad import VADSegmenter


class FakeVad:
    def is_speech(self, frame, sample_rate):
        return frame == b"S"


def run_frames(frames):
    segmenter = VADSegmenter(
        sample_rate=16000,
        frame_ms=30,
        vad_level=2,
        preroll_ms=500,
        silence_end_ms=400,
        min_speech_ms=500,
        max_segment_s=20,
    )
    segmenter.vad = FakeVad()

    return [
        result
        for frame in frames
        if (result := segmenter.process(frame)) is not None
    ]


def test_short_speech_not_accepted_with_preroll():
    frames = [b"N"] * 20 + [b"S"] * 2 + [b"N"] * 14

    assert run_frames(frames) == []


def test_real_speech_is_accepted():
    frames = [b"N"] * 20 + [b"S"] * 20 + [b"N"] * 14

    assert len(run_frames(frames)) == 1


def test_internal_silence_not_counted_as_speech():
    frames = (
        [b"N"] * 20
        + [b"S"] * 9
        + [b"N"] * 5
        + [b"S"] * 7
        + [b"N"] * 14
    )

    # 16 речевых кадров = 480 мс, меньше 500 мс.
    assert run_frames(frames) == []
