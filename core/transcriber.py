import os
import re
from core.audio_debug import save_debug_audio
from core.audio import prepare_audio
from core.analyzer import looks_like_garbage
from callsigns import normalize
from core.diagnostics import runtime


class Transcriber:
    """Обрабатывает аудиосегменты и передаёт события в EventProcessor."""

    def __init__(
        self,
        segments,
        recognizer,
        process_text,
        event_processor,
        sample_rate,
        min_rms,
        normalize_audio,
        debug_rms,
        blacklist,
        log,
        pause_gate=None,
    ):
        self.segments = segments
        self.recognizer = recognizer
        self.process_text = process_text
        self.event_processor = event_processor
        self.sample_rate = sample_rate
        self.min_rms = min_rms
        self.normalize_audio = normalize_audio
        self.debug_rms = debug_rms
        self.debug_audio = os.getenv("RADIO_DEBUG_AUDIO") == "1"
        self.debug_audio_count = 0
        self.blacklist = {
            self._blacklist_key(item)
            for item in blacklist
            if item
        }
        self.log = log
        self.pause_gate = pause_gate

    @staticmethod
    def _blacklist_key(value: str) -> str:
        """Нормализует текст для сравнения целых фраз."""
        words = re.findall(
            r"[^\W_]+",
            normalize(value),
            flags=re.UNICODE,
        )
        return " ".join(words)

    def _is_blacklisted(self, text: str) -> bool:
        key = self._blacklist_key(text)
        return bool(key) and key in self.blacklist

    def process_segment(self, pcm, generation=None):
        runtime.count("asr.segments")
        def is_current():
            return (
                self.pause_gate is None
                or self.pause_gate.is_current(generation)
            )

        if not is_current():
            runtime.count("asr.stale_generation")
            return

        audio, rms = prepare_audio(
            pcm, self.min_rms, self.normalize_audio
        )

        if self.debug_rms:
            self.log.info("rms=%.4f", rms)

        if audio.size == 0 or rms < self.min_rms:
            runtime.count("asr.low_rms")
            return

        if self.debug_audio and self.debug_audio_count < 12:
            try:
                paths = save_debug_audio(
                    pcm, audio, self.sample_rate,
                    self.debug_audio_count + 1,
                )
            except Exception:
                self.debug_audio = False
                self.log.exception('Помилка збереження діагностичного аудіо')
            else:
                self.debug_audio_count += 1
                self.log.info(
                    'Аудио #%d: %s | RMS=%.4f',
                    self.debug_audio_count, paths[0], rms,
                )

        try:
            with runtime.timed("asr.whisper"):
                result = self.recognizer.transcribe(audio)
            raw_text = self.recognizer.extract_text(result)
        except Exception:
            runtime.count("asr.whisper_error")
            self.log.exception(
                "Помилка розпізнавання Whisper; сегмент пропущено"
            )
            return

        if not is_current():
            return

        duration = len(pcm) / (2 * self.sample_rate)

        if not raw_text:
            runtime.count("asr.empty_text")
            return
        if looks_like_garbage(raw_text, duration):
            runtime.count("asr.garbage_filtered")
            return
        if self._is_blacklisted(raw_text):
            runtime.count("asr.blacklist_filtered")
            return

        if not is_current():
            return

        self.log.info("Розпізнано (початкове): %s", raw_text)

        try:
            with runtime.timed("asr.correction_pipeline"):
                text = self.process_text(raw_text)
        except Exception:
            runtime.count("asr.correction_error")
            self.log.exception(
                "Помилка корекції тексту; використовується початковий текст Whisper"
            )
            text = raw_text
        if not is_current():
            return

        try:
            if self.pause_gate is None:
                self.event_processor.process(
                    text, pcm, raw_text=raw_text
                )
            else:
                self.pause_gate.run_if_current(
                    generation,
                    lambda: self.event_processor.process(
                        text, pcm, raw_text=raw_text
                    ),
                )
        except Exception:
            runtime.count("asr.event_error")
            self.log.exception(
                "Помилка обробки події; продовжуємо прослуховування ефіру"
            )

    def run(self):
        while True:
            item = self.segments.get()

            if self.pause_gate is None:
                self.process_segment(item)
                continue

            generation, pcm = item

            if not self.pause_gate.is_current(generation):
                continue

            self.process_segment(pcm, generation=generation)
