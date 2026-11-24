import os
import queue
import re
import sqlite3
import time
import threading
from core.audio_debug import save_debug_audio
from shared.audio import prepare_audio
from shared.analyzer import is_low_confidence_short, looks_like_garbage, strip_dictionary_echo
from shared.callsigns import normalize
from shared.diagnostics import runtime
from shared.whisper_result import RecognizerUnavailable, is_looped


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
        stop_event=None,
        abandon_event=None,
    ):
        self.stop_event = stop_event or threading.Event()
        self.abandon_event = abandon_event or threading.Event()
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
        # Другий шанс: якщо з підказкою Whisper зациклився й тексту не лишилося — ще раз без
        # підказки (назви словника в підказці провокують такі петлі). Вмикає фабрика з config.
        self.retry_without_prompt = False
        self._whisper_called = False
        self._whisper_failed = False
        self._unavailable = False
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
        # Режим «лише запис» (на час тестів лабораторії): capture_only() → True — фрази лише в архів,
        # Whisper вивантажено; on_unloaded(bool) — повідомити, що пам'ять звільнено / модель знову потрібна;
        # deferred — накопичене: next_deferred() → (id, pcm) чи None, finish_deferred(id, trace).
        # Задає приймач; None — вимкнено.
        self.capture_only = None
        self.on_unloaded = None
        self.deferred = None
        self._unloaded = False
        self._capture_cache = (0.0, False)
        self._capture_error_logged = False
        self._dry = False          # доробка накопиченого: без подій і Telegram
        self._deferred_idle_until = 0.0   # накопиченого немає — не питати архів щосекунди
        self._unload_retry_at = 0.0
        self._restore_retry_at = 0.0
        self._model_report_pending = None
        self._model_report_retry_at = 0.0
        self.daily_code_formatter = None

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
        """Фраза з чорного списку цілком — або кілька її повторів («Дякую за перегляд! Дякую…»)."""
        keys = self.text_lists.blacklist if self.text_lists is not None else self.blacklist
        key = self._blacklist_key(text)
        if key and key in keys:
            return True
        parts = [k for k in (self._blacklist_key(p) for p in re.split(r"[.!?;…]+", text)) if k]
        return len(parts) > 1 and all(p in keys for p in parts)

    def process_segment(self, pcm, generation=None, deferred_id=None):
        from shared.recording_trace import _trace
        trace = {"predicted_recipient": "", "predicted_sender": "", "roles_parsed": False} if self.recorder is not None else None
        token = _trace.set(trace)
        span = {}
        self._unavailable = False
        try:
            with runtime.asr_segment(pcm, self.sample_rate, self.segments) as span:
                self._process_segment(pcm, generation, deferred_id)
        finally:
            _trace.reset(token)
            if self._unavailable:
                if deferred_id is None:
                    self._store_deferred(pcm)   # накопичений запис і так лишається deferred
                else:
                    self._deferred_idle_until = time.monotonic() + 10
            elif trace is not None and not self.abandon_event.is_set():
                from shared.diagnostics import _ASR_RECORD_FIELDS
                trace.update({key: value for key, value in span.items() if key in _ASR_RECORD_FIELDS})
                with runtime.lock:
                    trace["runtime"] = {key: value for key, value in runtime.gauges.items()
                                        if key in ("dictionaries.revision", "text_lists.revision", "runtime_settings.revision",
                                                   "audio.frames_dropped", "audio.segments_dropped")}
                trace["vad"] = getattr(pcm, "vad", None)
                trace.update(session=runtime.session, min_rms=self.min_rms, normalize=self.normalize_audio,
                             garbage_limits=dict(self.garbage_limits))
                if deferred_id is not None:
                    self.deferred.finish_deferred(deferred_id, trace)   # той самий запис архіву, а не новий
                else:
                    self.recorder.submit(pcm, self.sample_rate, trace)

    def _spectral_score(self, pcm):
        """Оцінка спектрального збігу. У режимі спостереження лише записується."""
        if self.spectral_mode == "off" or self.spectral_matcher is None or not self.spectral_matcher.templates:
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

    def _process_segment(self, pcm, generation=None, deferred_id=None):
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
            return not self.abandon_event.is_set() and (
                self._dry   # накопичене з архіву — завжди «актуальне»: подій із нього не буде
                or self.pause_gate is None
                or self.pause_gate.is_current(generation)
            )

        if not is_current():
            runtime.count("asr.stale_generation")
            runtime.asr_status("stale")
            return

        audio, rms = prepare_audio(
            pcm, self.min_rms, self.normalize_audio
        )

        from shared.recording_trace import note
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
            from shared.recording_trace import note
            if not raw_text and self.retry_without_prompt and getattr(self.recognizer, "prompt", "") and is_looped(result):
                runtime.count("asr.whisper_retry")
                with runtime.timed("asr.whisper_retry"):
                    retry = self.recognizer.transcribe(audio, prompt="")
                retry_text = self.recognizer.extract_text(retry)
                note(whisper_retry=bool(retry_text))
                if retry_text:
                    runtime.count("asr.whisper_retry_recovered")
                    result, raw_text = retry, retry_text
            whisper_segments = [{k: segment[k] for k in (
                "start", "end", "text", "avg_logprob", "no_speech_prob", "compression_ratio", "temperature") if k in segment}
                for segment in (result.get("segments", []) if isinstance(result, dict) else [])]
            note(raw_text=raw_text, whisper_segments=whisper_segments)
        except RecognizerUnavailable:
            # Вузол розшифровки (Mac) спить чи зайнятий: це не помилка моделі, фразу відкладаємо.
            self._whisper_called = False
            self._unavailable = True
            runtime.count("asr.recognizer_unavailable")
            return
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
        if self.daily_code_formatter is not None:
            try:
                recorded_at = self.deferred.created_at(deferred_id) if deferred_id is not None else time.time()
                if recorded_at is None:
                    self.log.warning("Час відкладеного запису недоступний; коди доби не застосовано")
                else:
                    text = self.daily_code_formatter(text, recorded_at)
            except (sqlite3.Error, OSError, ValueError) as exc:
                runtime.count("asr.daily_code_format_error")
                self.log.warning("Коди доби не застосовано: %s", type(exc).__name__)
        if not is_current():
            runtime.asr_status("stale")
            return
        from shared.recording_trace import note
        note(final_text=text)
        runtime.asr_text_ready(fallback=fallback)

        if self._dry:
            # Доробка накопиченого: текст і ролі лише в архіві, без подій і Telegram.
            try:
                self.event_processor.annotate(text)
            except Exception:
                runtime.count("asr.event_error")
                self.log.exception("Помилка аналізу відкладеного запису; лишається текст без нього")
            return
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

    # ---------- Режим «лише запис» ----------
    def _capture_active(self):
        if self.capture_only is None:
            return False
        checked, value = self._capture_cache
        if time.monotonic() - checked >= 1.0:
            try:
                value = bool(self.capture_only())
            except Exception:
                runtime.count("asr.capture_check_error")
                if not self._capture_error_logged:
                    self.log.exception("Не вдалося перевірити режим «лише запис»; Whisper тимчасово не запускається")
                    self._capture_error_logged = True
                value = True
            else:
                self._capture_error_logged = False
            self._capture_cache = (time.monotonic(), value)
        return value

    def _set_unloaded(self, unloaded):
        if unloaded == self._unloaded:
            return self._report_model_state()
        now = time.monotonic()
        if unloaded and now < self._unload_retry_at:
            return False
        if not unloaded and now < self._restore_retry_at:
            return False
        try:
            if unloaded:
                self.recognizer.unload()
            else:
                self.recognizer.warmup()
        except Exception:
            runtime.count("asr.capture_model_transition_error")
            if unloaded:
                self._unload_retry_at = now + 10
                self.log.exception("Не вдалося вивантажити Whisper; режим запису чекатиме")
            else:
                self._restore_retry_at = now + 10
                self.log.exception("Не вдалося відновити Whisper; накопичені фрази поки не розпізнаються")
            return False
        self._unload_retry_at = self._restore_retry_at = 0.0
        if unloaded:
            self.log.info("Режим «лише запис»: Whisper вивантажено, фрази пишуться в архів для розпізнавання пізніше",
                          extra={"console": True})
        else:
            self._deferred_idle_until = 0.0   # щойно з режиму — одразу доробляти накопичене
            self.log.info("Режим «лише запис» завершено: розпізнавання відновлено", extra={"console": True})
        self._unloaded = unloaded
        self._model_report_pending = unloaded
        self._model_report_retry_at = 0.0
        return self._report_model_state()

    def _report_model_state(self):
        """Повторити підтвердження після збою SQLite, не перевантажуючи модель повторно."""
        pending = self._model_report_pending
        if pending is None:
            return True
        if time.monotonic() < self._model_report_retry_at:
            return False
        if self.on_unloaded is not None:
            try:
                self.on_unloaded(pending)
            except Exception:
                self._model_report_retry_at = time.monotonic() + 2
                runtime.count("asr.capture_state_report_error")
                self.log.exception("Не вдалося записати стан режиму «лише запис»")
                return False
        self._model_report_pending = None
        return True

    def _store_deferred(self, pcm):
        """Фраза — в архів зі звуком і статусом «очікує розпізнавання»."""
        runtime.count("asr.deferred")
        if self.recorder is not None:
            captured_at = getattr(getattr(pcm, "timing", None), "audio_end_unix_s", None) or time.time()
            self.recorder.submit(pcm, self.sample_rate, {"status": "deferred", "capture_only": True,
                                                         "captured_at": captured_at, "session": runtime.session,
                                                         "vad": getattr(pcm, "vad", None)})

    def _process_one_deferred(self):
        """У вільну хвилину — одну накопичену фразу: текст в архів, без подій і Telegram."""
        if time.monotonic() < self._deferred_idle_until:
            return False
        try:
            item = self.deferred.next_deferred()
        except Exception:
            self.log.exception("Не вдалося взяти накопичений запис з архіву")
            return False
        if item is None:
            self._deferred_idle_until = time.monotonic() + 30
            return False
        record_id, pcm = item
        self._dry = True
        try:
            self.process_segment(pcm, deferred_id=record_id)
            if not self._unavailable:
                runtime.count("asr.deferred_done")
        except sqlite3.Error:
            # Запис звуку лишається deferred: збій індексу не є помилкою Whisper.
            runtime.count("asr.deferred_store_error")
            self._deferred_idle_until = time.monotonic() + 5
            self.log.exception("Не вдалося зберегти результат накопиченого запису; спробу буде повторено")
        except Exception:
            runtime.count("asr.deferred_error")
            self.log.exception("Не вдалося розпізнати накопичений запис %s", record_id)
            try:
                self.deferred.finish_deferred(record_id, {"status": "whisper_error"})
            except Exception:
                self.log.exception("Не вдалося зафіксувати помилку розпізнавання запису %s", record_id)
        finally:
            self._dry = False
        return True

    def run(self):
        if self.health is not None:
            self.health.reset()
        if self.on_unloaded is not None:
            self._model_report_pending = False
            self._model_report_retry_at = 0.0
            self._report_model_state()
        while True:
            if self.abandon_event.is_set() or (self.stop_event.is_set() and self.segments.empty()):
                return
            capture = self._capture_active()
            if capture:
                self._set_unloaded(True)
            try:
                # Чекаємо з таймаутом: і без ефіру помічаємо зміну режиму й доробляємо накопичене.
                item = self.segments.get(timeout=1.0)
            except queue.Empty:
                if not self.stop_event.is_set() and not capture and self.deferred is not None:
                    if self._set_unloaded(False):
                        self._process_one_deferred()
                continue
            runtime.count("asr.dequeued")

            generation, pcm = (None, item) if self.pause_gate is None else item

            if self.pause_gate is not None and not self.pause_gate.is_current(generation):
                with runtime.asr_segment(pcm, self.sample_rate, self.segments):
                    runtime.asr_status("stale")
                runtime.count("asr.stale_generation")
                continue

            if capture:
                self._store_deferred(pcm)
                continue
            if not self._set_unloaded(False):
                self._store_deferred(pcm)
                continue
            self._tracked(pcm, generation=generation)
            if not self.stop_event.is_set() and self.deferred is not None and self.segments.empty():
                self._process_one_deferred()
