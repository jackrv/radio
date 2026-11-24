import numpy as np
import mlx_whisper
from shared.diagnostics import runtime
from shared.whisper_result import COMPRESSION_LIMIT, extract_text, is_looped  # noqa: F401 — старі імпорти


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
        if set(self.options) & {'beam_size','path_or_hf_repo','language','initial_prompt','temperature'}:
            raise ValueError('Параметр Whisper заборонений або повторює основний аргумент')
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

    def unload(self):
        """Вивантажити модель Whisper і повернути пам'ять (режим «лише запис»); наступний transcribe завантажить знову."""
        import gc
        import importlib
        module = importlib.import_module("mlx_whisper.transcribe")
        holder = getattr(module, "ModelHolder", None)
        if holder is None:
            raise RuntimeError("У встановленій mlx-whisper не знайдено кеш моделі")
        holder.model = None
        holder.model_path = None
        gc.collect()
        import mlx.core as mx
        mx.clear_cache()
        runtime.gauge("models.whisper", "unloaded")

    extract_text = staticmethod(extract_text)
