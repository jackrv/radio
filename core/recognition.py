import numpy as np
import mlx_whisper
from core.diagnostics import runtime

COMPRESSION_LIMIT = 2.4   # вище — Whisper зациклився («Грозу, Грозу, Грозу…»): сегмент відкидається


def is_looped(result):
    """Whisper зациклився хоча б в одному сегменті (після всіх температур)."""
    return isinstance(result, dict) and any(
        isinstance(s, dict) and (s.get("compression_ratio") or 0) > COMPRESSION_LIMIT for s in result.get("segments", ()))


class WhisperRecognizer:
    """Обёртка над MLX Whisper."""

    DEFAULT_TEMPERATURE = (0.0, 0.2, 0.4)

    def __init__(self, model_repo, language, sample_rate, prompt, log, temperature=DEFAULT_TEMPERATURE,
                 options=None):
        self.model_repo = model_repo
        self.language = language
        self.sample_rate = sample_rate
        self.prompt = prompt
        self.log = log
        self.temperature = tuple(temperature)
        # Додаткові параметри декодування — лише для офлайн-досліджень (tools/asr_lab).
        self.options = dict(options or {})
        self.transcribe_fn = mlx_whisper.transcribe

    def warmup(self):
        """Прогревает модель на секунде тишины."""
        self.log.info("Завантажую Whisper (%s)...", self.model_repo)
        runtime.gauge("models.whisper", "loading")
        try:
            with runtime.timed("asr.whisper_warmup"):
                self.transcribe_fn(
                    np.zeros(self.sample_rate, dtype=np.float32),
                    path_or_hf_repo=self.model_repo,
                    language=self.language,
                )
        except Exception:
            runtime.gauge("models.whisper", "failed")
            raise
        runtime.gauge("models.whisper", "ready")

    def transcribe(self, audio, prompt=None):
        """Розпізнає аудіо з параметрами проєкту. ``prompt=""`` — без підказки (другий шанс)."""
        options = {
            "condition_on_previous_text": False,
            "no_speech_threshold": 0.6,
            "logprob_threshold": -1.0,
            "compression_ratio_threshold": 2.4,
            **self.options,
        }
        return self.transcribe_fn(
            audio,
            path_or_hf_repo=self.model_repo,
            language=self.language,
            initial_prompt=self.prompt if prompt is None else (prompt or None),
            temperature=self.temperature,
            **options,
        )

    @staticmethod
    def extract_text(result):
        """Отбирает пригодные для дальнейшей обработки фрагменты."""
        parts = [
            segment["text"].strip()
            for segment in result.get("segments", [])
            if not (
                segment.get("no_speech_prob", 0.0) > 0.6
                and segment.get("avg_logprob", 0.0) < -0.8
            )
            and segment.get("compression_ratio", 0) <= COMPRESSION_LIMIT
        ]
        return " ".join(parts).strip()
