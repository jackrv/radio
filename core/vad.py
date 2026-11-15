import collections

import webrtcvad

from core.diagnostics import runtime


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
        on_discard=None,
        speech_gate=None,
    ):
        self.on_discard = on_discard
        # Необов’язкова додаткова умова «кадр — мовлення» (наприклад, гучність над
        # фоном ефіру). Типово вимкнено: поведінка приймача не змінюється.
        self.speech_gate = speech_gate
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
        self._speech_end_at = None
        self._speech_end_unix_s = None
        self._speech_source = "legacy"
        self.completed_speech_end_at = None
        self.completed_speech_end_unix_s = None
        self.completed_speech_source = "legacy"
        self.completed_vad = None

    def process(self, frame: bytes):
        """Принимает один аудиокадр; возвращает готовый PCM-сегмент или None."""

        is_speech = self.vad.is_speech(frame, self.sample_rate)
        if is_speech and self.speech_gate is not None and not self.speech_gate(frame):
            is_speech = False
        if is_speech:
            self._speech_end_at = getattr(frame, "captured_at", None)
            self._speech_end_unix_s = getattr(frame, "captured_unix_s", None)
            self._speech_source = getattr(frame, "capture_source", "legacy")

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
                self.completed_vad = {"speech_ms": speech_ms, "total_ms": total_ms,
                                      "end_reason": "max_duration" if total_ms >= self.max_segment_ms else "silence",
                                      "speech_ratio": speech_ms / total_ms}
                self.completed_speech_end_at = self._speech_end_at
                self.completed_speech_end_unix_s = self._speech_end_unix_s
                self.completed_speech_source = self._speech_source
            else:
                # Видно, скільки коротких фрагментів відсіяно порогом MIN_SPEECH_MS.
                if self.on_discard is not None:
                    self.on_discard(b"".join(self.voiced), self.sample_rate, speech_ms)
                runtime.count("vad.short_discarded")
                runtime.observe("vad.short_discarded_speech", speech_ms / 1000)

            self.in_speech = False
            self.voiced = []
            self.silence = 0
            self.speech_frames = 0
            self.preroll.clear()
            self._speech_end_at = None
            self._speech_end_unix_s = None
            self._speech_source = "legacy"

            return segment

        return None
