import os
import re
from core.audio_debug import save_debug_audio
from core.audio import prepare_audio
from core.analyzer import is_low_confidence_short, looks_like_garbage, strip_dictionary_echo
from core.callsigns import normalize
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
        health=None,
        spectral_matcher=None,
        spectral_mode="shadow",
        text_lists=None,
        recorder=None,
    ):
        self.recorder = recorder
        self.segments = segments
        # Стан прогресу для супервізора; None — без контролю (тести, старий код).
        self.health = health
        self.spectral_matcher = spectral_matcher
        self.spectral_mode = spectral_mode
        # Живі списки з SQLite (фрази-галюцинації, алиаси); None — статичний blacklist.
        self.text_lists = text_lists
        # Інші живі джерела (словники, налаштування обробки): викликаються перед сегментом.
        self.refreshers = ()
        self.garbage_limits = {}
        # {"max_words": .., "logprob": ..} — відкидати короткий текст із низькою
        # впевненістю Whisper; None — правило вимкнене (задає фабрика приймача).
        self.low_confidence = None
        self._whisper_called = False
        self._whisper_failed = False
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
        keys = self.text_lists.blacklist if self.text_lists is not None else self.blacklist
        return bool(key) and key in keys

    def process_segment(self, pcm, generation=None):
        from core.recording_trace import _trace
        trace = {"predicted_recipient": "", "predicted_sender": "", "roles_parsed": False} if self.recorder is not None else None
        token = _trace.set(trace)
        span = {}
        try:
            with runtime.asr_segment(pcm, self.sample_rate, self.segments) as span:
                self._process_segment(pcm, generation)
        finally:
            _trace.reset(token)
            if trace is not None:
                from core.diagnostics import _ASR_RECORD_FIELDS
                trace.update({key: value for key, value in span.items() if key in _ASR_RECORD_FIELDS})
                with runtime.lock:
                    trace["runtime"] = {key: value for key, value in runtime.gauges.items()
                                        if key in ("dictionaries.revision", "text_lists.revision", "runtime_settings.revision",
                                                   "audio.frames_dropped", "audio.segments_dropped")}
                trace["vad"] = getattr(pcm, "vad", None)
                trace.update(session=runtime.session, min_rms=self.min_rms, normalize=self.normalize_audio,
                             garbage_limits=dict(self.garbage_limits))
                self.recorder.submit(pcm, self.sample_rate, trace)

    def _spectral_score(self, pcm):
        """Оцінка спектрального збігу. У режимі спостереження лише записується."""
        if self.spectral_matcher is None or not self.spectral_matcher.templates:
            return None
        try:
            with runtime.timed("asr.notification_spectral"):
                result = self.spectral_matcher.score_pcm(pcm)
        except Exception:
            runtime.count("asr.spectral.error")
            self.log.exception("Помилка спектрального фільтра; сегмент передано далі")
            return None
        if result is not None:
            runtime.asr_notification_score(result)
        return result

    def _process_segment(self, pcm, generation=None):
        runtime.count("asr.segments")
        if self.text_lists is not None:
            try:
                self.text_lists.refresh()
            except Exception:
                runtime.count("text_lists.refresh_error")
                self.log.exception("Не вдалося оновити текстові списки; використовую попередні")
        for refresh in self.refreshers:
            try:
                refresh()
            except Exception:
                runtime.count("live_config.refresh_error")
                self.log.exception("Не вдалося оновити словники чи налаштування; використовую попередні")
        def is_current():
            return (
                self.pause_gate is None
                or self.pause_gate.is_current(generation)
            )

        if not is_current():
            runtime.count("asr.stale_generation")
            runtime.asr_status("stale")
            return

        audio, rms = prepare_audio(
            pcm, self.min_rms, self.normalize_audio
        )

        from core.recording_trace import note
        note(prepared_peak=float(abs(audio).max()) if audio.size else 0.0)
        if self.debug_rms:
            self.log.info("rms=%.4f", rms)

        if audio.size == 0 or rms < self.min_rms:
            runtime.asr_status("low_rms")
            runtime.count("asr.low_rms")
            # Рівні відкинутих і прийнятих сегментів: чи не ріже MIN_RMS реальне мовлення.
            runtime.observe("asr.low_rms_level", rms)
            return
        runtime.observe("asr.accepted_rms_level", rms)

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

        spectral = self._spectral_score(pcm)

        if spectral is not None and spectral["candidate"] and self.spectral_mode == "active":
            runtime.asr_status("notification")
            runtime.count("asr.notification_filtered")
            runtime.count("asr.spectral.filtered")
            self.log.info("Відкинуто окремий звук сповіщення: спектральний збіг %.3f", spectral["score"])
            return

        self._whisper_called = True
        try:
            with runtime.timed("asr.whisper"):
                result = self.recognizer.transcribe(audio)
            raw_text = self.recognizer.extract_text(result)
            from core.recording_trace import note
            whisper_segments = [{k: segment[k] for k in (
                "start", "end", "text", "avg_logprob", "no_speech_prob", "compression_ratio", "temperature") if k in segment}
                for segment in (result.get("segments", []) if isinstance(result, dict) else [])]
            note(raw_text=raw_text, whisper_segments=whisper_segments)
        except Exception:
            self._whisper_failed = True
            runtime.asr_status("whisper_error")
            runtime.count("asr.whisper_error")
            self.log.exception(
                "Помилка розпізнавання Whisper; сегмент пропущено"
            )
            return

        if not is_current():
            runtime.asr_status("stale")
            return

        duration = len(pcm) / (2 * self.sample_rate)

        if not raw_text:
            runtime.asr_status("empty_text")
            runtime.count("asr.empty_text")
            return
        if looks_like_garbage(raw_text, duration, **self.garbage_limits):
            runtime.asr_status("garbage")
            runtime.count("asr.garbage_filtered")
            return
        if self._is_blacklisted(raw_text):
            runtime.asr_status("blacklist")
            runtime.count("asr.blacklist_filtered")
            return
        # Перелік назв словника, переписаний із підказки Whisper («…береза вишня клен»).
        try:
            dictionaries = tuple(
                d for d in (getattr(self.event_processor, n, None) for n in ("callsigns", "commands", "watch"))
                if isinstance(getattr(d, "names", None), (list, tuple, set, frozenset)))
            cleaned, echo = strip_dictionary_echo(raw_text, dictionaries) if dictionaries else (raw_text, False)
        except Exception:
            self.log.exception("Помилка перевірки переліку назв; текст передано далі без змін")
            cleaned, echo = raw_text, False
        if echo:
            runtime.count("asr.dictionary_echo")
            if not cleaned:
                runtime.asr_status("dictionary_echo")
                self.log.info("Відкинуто: перелік назв словника з підказки Whisper: %s", raw_text)
                return
            self.log.info("Прибрано перелік назв словника з підказки: %s -> %s", raw_text, cleaned)
            raw_text = cleaned
            if self._is_blacklisted(raw_text):
                runtime.asr_status("blacklist")
                runtime.count("asr.blacklist_filtered")
                return
        if self.low_confidence is not None and is_low_confidence_short(
                raw_text, whisper_segments, self.event_processor.callsigns, self.event_processor.commands,
                **self.low_confidence):
            runtime.asr_status("low_confidence")
            runtime.count("asr.low_confidence_filtered")
            self.log.info("Відкинуто: короткий текст із низькою впевненістю Whisper: %s", raw_text)
            return

        if not is_current():
            runtime.asr_status("stale")
            return

        self.log.info("Розпізнано (початкове): %s", raw_text)

        note(filtered_text=raw_text)
        fallback = False
        try:
            with runtime.timed("asr.correction_pipeline"):
                text = self.process_text(raw_text)
        except Exception:
            fallback = True
            runtime.count("asr.correction_error")
            self.log.exception(
                "Помилка корекції тексту; використовується початковий текст Whisper"
            )
            text = raw_text
        if not is_current():
            runtime.asr_status("stale")
            return
        from core.recording_trace import note
        note(final_text=text)
        runtime.asr_text_ready(fallback=fallback)

        try:
            with runtime.timed("asr.event_processing"):
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

    def _tracked(self, pcm, generation=None):
        """Обробка сегмента з відміткою початку й результату для контролю прогресу."""
        if self.health is None:
            self.process_segment(pcm, generation=generation)
            return
        self._whisper_called = False
        self._whisper_failed = False
        self.health.started()
        try:
            self.process_segment(pcm, generation=generation)
        finally:
            self.health.finished(
                None if not self._whisper_called else not self._whisper_failed
            )

    def run(self):
        if self.health is not None:
            self.health.reset()
        while True:
            item = self.segments.get()
            runtime.count("asr.dequeued")

            if self.pause_gate is None:
                self._tracked(item)
                continue

            generation, pcm = item

            if not self.pause_gate.is_current(generation):
                with runtime.asr_segment(pcm, self.sample_rate, self.segments):
                    runtime.asr_status("stale")
                runtime.count("asr.stale_generation")
                continue

            self._tracked(pcm, generation=generation)
