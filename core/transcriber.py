import os
from core.audio_debug import save_debug_audio
from core.audio import prepare_audio
from core.analyzer import looks_like_garbage
from callsigns import normalize


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
        self.blacklist = blacklist
        self.log = log

    def process_segment(self, pcm):
        audio, rms = prepare_audio(
            pcm, self.min_rms, self.normalize_audio
        )

        if self.debug_rms:
            self.log.info("rms=%.4f", rms)

        if audio.size == 0 or rms < self.min_rms:
            return

        if self.debug_audio and self.debug_audio_count < 12:
            try:
                paths = save_debug_audio(
                    pcm, audio, self.sample_rate,
                    self.debug_audio_count + 1,
                )
            except Exception:
                self.debug_audio = False
                self.log.exception('Ошибка сохранения диагностического аудио')
            else:
                self.debug_audio_count += 1
                self.log.info(
                    'Аудио #%d: %s | RMS=%.4f',
                    self.debug_audio_count, paths[0], rms,
                )

        try:
            result = self.recognizer.transcribe(audio)
            raw_text = self.recognizer.extract_text(result)
        except Exception:
            self.log.exception(
                "Ошибка распознавания Whisper; сегмент пропущен"
            )
            return

        duration = len(pcm) / (2 * self.sample_rate)

        if (
            not raw_text
            or looks_like_garbage(raw_text, duration)
            or any(b in normalize(raw_text) for b in self.blacklist)
        ):
            return

        self.log.info("Распознано (сырое): %s", raw_text)

        try:
            text = self.process_text(raw_text)
        except Exception:
            self.log.exception(
                "Ошибка коррекции текста; используется исходный текст Whisper"
            )
            text = raw_text
        try:
            self.event_processor.process(text, pcm)
        except Exception:
            self.log.exception(
                "Ошибка обработки события; продолжаем прослушивание эфира"
            )

    def run(self):
        while True:
            pcm = self.segments.get()
            self.process_segment(pcm)
