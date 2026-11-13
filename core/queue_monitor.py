import time


class QueueDropMonitor:
    """Контролирует потери аудиокадров и речевых сегментов."""

    def __init__(
        self,
        frames,
        segments,
        log,
        interval_s=30.0,
        clock=None,
    ):
        if interval_s <= 0:
            raise ValueError("interval_s должен быть положительным")

        self.frames = frames
        self.segments = segments
        self.log = log
        self.interval_s = interval_s
        self.clock = clock or time.monotonic

        self.last_check = self.clock()
        self.last_frames = getattr(frames, "dropped", 0)
        self.last_segments = getattr(segments, "dropped", 0)

    def check(self):
        now = self.clock()

        if now - self.last_check < self.interval_s:
            return

        frames_total = getattr(self.frames, "dropped", 0)
        segments_total = getattr(self.segments, "dropped", 0)

        frames_lost = max(0, frames_total - self.last_frames)
        segments_lost = max(0, segments_total - self.last_segments)

        self.last_check = now
        self.last_frames = frames_total
        self.last_segments = segments_total

        if frames_lost or segments_lost:
            self.log.warning(
                "Потери аудио: кадров=%d, сегментов=%d "
                "(всего кадров=%d, сегментов=%d)",
                frames_lost,
                segments_lost,
                frames_total,
                segments_total,
            )
