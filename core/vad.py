import collections

import webrtcvad


class VADSegmenter:
    """Выделяет речевые сегменты из последовательности PCM int16 кадров."""

    def __init__(
        self,
        sample_rate: int,
        frame_ms: int,
        vad_level: int,
        preroll_ms: int,
        silence_end_ms: int,
        min_speech_ms: int,
        max_segment_s: int,
    ):
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.silence_end_ms = silence_end_ms
        self.min_speech_ms = min_speech_ms
        self.max_segment_ms = max_segment_s * 1000

        self.vad = webrtcvad.Vad(vad_level)
        self.preroll = collections.deque(
            maxlen=preroll_ms // frame_ms
        )
        self.voiced = []
        self.silence = 0
        self.speech_frames = 0
        self.in_speech = False

    def process(self, frame: bytes):
        """Принимает один аудиокадр; возвращает готовый PCM-сегмент или None."""

        is_speech = self.vad.is_speech(frame, self.sample_rate)

        if not self.in_speech:
            self.preroll.append(frame)

            if is_speech:
                self.in_speech = True
                self.voiced = list(self.preroll)
                self.silence = 0
                self.speech_frames = 1

            return None

        self.voiced.append(frame)
        self.silence = 0 if is_speech else self.silence + 1
        if is_speech:
            self.speech_frames += 1

        total_ms = len(self.voiced) * self.frame_ms
        speech_ms = self.speech_frames * self.frame_ms

        if (
            self.silence * self.frame_ms >= self.silence_end_ms
            or total_ms >= self.max_segment_ms
        ):
            segment = None

            if speech_ms >= self.min_speech_ms:
                segment = b"".join(self.voiced)

            self.in_speech = False
            self.voiced = []
            self.silence = 0
            self.speech_frames = 0
            self.preroll.clear()

            return segment

        return None
