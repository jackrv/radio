import numpy as np
import mlx_whisper


class WhisperRecognizer:
    """Обёртка над MLX Whisper."""

    def __init__(self, model_repo, language, sample_rate, prompt, log):
        self.model_repo = model_repo
        self.language = language
        self.sample_rate = sample_rate
        self.prompt = prompt
        self.log = log
        self.transcribe_fn = mlx_whisper.transcribe

    def warmup(self):
        """Прогревает модель на секунде тишины."""
        self.log.info("Загружаю Whisper (%s)...", self.model_repo)
        self.transcribe_fn(
            np.zeros(self.sample_rate, dtype=np.float32),
            path_or_hf_repo=self.model_repo,
            language=self.language,
        )

    def transcribe(self, audio):
        """Распознаёт аудио с исходными параметрами проекта."""
        return self.transcribe_fn(
            audio,
            path_or_hf_repo=self.model_repo,
            language=self.language,
            initial_prompt=self.prompt,
            temperature=(0.0, 0.2, 0.4),
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            logprob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )

    @staticmethod
    def extract_text(result):
        """Отбирает пригодные для дальнейшей обработки фрагменты."""
        parts = [
            segment["text"].strip()
            for segment in result.get("segments", [])
            if not (
                segment["no_speech_prob"] > 0.6
                and segment["avg_logprob"] < -0.8
            )
            and segment.get("compression_ratio", 0) <= 2.4
        ]
        return " ".join(parts).strip()
