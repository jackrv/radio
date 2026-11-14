"""Числові часові мітки поруч із PCM; формат bytes у чергах збережено."""

from dataclasses import dataclass
import math
import time


class CapturedFrame(bytes):
    def __new__(cls, pcm, *, captured_at, captured_unix_s, source):
        frame = super().__new__(cls, pcm)
        frame.captured_at = captured_at
        frame.captured_unix_s = captured_unix_s
        frame.capture_source = source
        return frame


def capture_frame(pcm, frame_count, sample_rate, time_info):
    now, wall = time.perf_counter(), time.time()
    delay, source = 0.0, "callback"
    try:
        # Часи PortAudio мають власний спільний відлік; переносимо лише різницю.
        current = float(time_info.currentTime)
        adc = float(time_info.inputBufferAdcTime)
        lag = current - adc - frame_count / sample_rate
        if current > 0 and adc > 0 and math.isfinite(lag) and 0 <= lag <= 10:
            delay, source = lag, "portaudio"
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        pass
    return CapturedFrame(pcm, captured_at=now - delay, captured_unix_s=wall - delay, source=source)


@dataclass(frozen=True)
class SegmentTiming:
    enqueued_at: float
    enqueued_unix_s: float
    audio_end_at: float | None = None
    audio_end_unix_s: float | None = None
    speech_end_at: float | None = None
    speech_end_unix_s: float | None = None
    capture_source: str = "legacy"


class TimedSegment(bytes):
    def __new__(cls, pcm, timing):
        segment = super().__new__(cls, pcm)
        segment.timing = timing
        return segment


def stamp_segment(pcm, frame, segmenter):
    return TimedSegment(pcm, SegmentTiming(
        enqueued_at=time.perf_counter(), enqueued_unix_s=time.time(),
        audio_end_at=getattr(frame, "captured_at", None),
        audio_end_unix_s=getattr(frame, "captured_unix_s", None),
        speech_end_at=getattr(segmenter, "completed_speech_end_at", None),
        speech_end_unix_s=getattr(segmenter, "completed_speech_end_unix_s", None),
        capture_source=getattr(segmenter, "completed_speech_source", getattr(frame, "capture_source", "legacy")),
    ))
