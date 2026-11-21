"""Тіла запитів локальної панелі."""
from typing import Literal

from pydantic import BaseModel, Field, model_validator

class RecordingReview(BaseModel):
    reference_recipient: str | None = Field(default=None, max_length=300)
    reference_sender: str | None = Field(default=None, max_length=300)
    reference: str = Field(max_length=2000)
    label: str = Field(pattern=r"^(speech|noise|notification|unclear|room)$")
    expected_revision: int = Field(ge=0)
    multiple_messages: bool | None = None


class RecordingRestore(BaseModel):
    """Стан запису до швидкої розмітки — для «↶ Скасувати» (reference=None — нерозмічений)."""
    expected_revision: int = Field(ge=0)
    reference: str | None = Field(default=None, max_length=2000)
    label: str | None = Field(default=None, pattern=r"^(speech|noise|notification|unclear|room)$")
    reference_recipient: str | None = Field(default=None, max_length=300)
    reference_sender: str | None = Field(default=None, max_length=300)
    multiple_messages: bool | None = None
    reviewed_at: float | None = Field(default=None, ge=0, le=4e9)
    reference_source: str | None = Field(default=None, pattern=r"^miniapp$")


class SuggestionDismiss(BaseModel):
    kind: str = Field(max_length=40)


class SuggestionRestore(BaseModel):
    keys: list[str] = Field(max_length=100)


class SessionInput(BaseModel):
    token: str = Field(max_length=128)


class RoleSuggestion(BaseModel):
    text: str = Field(max_length=2000)


class TextCheck(BaseModel):
    text: str = Field(max_length=5000)
    highlights: bool = False
    at: float | None = Field(default=None, ge=0, le=4e9)   # час запису: коди доби цього дня


class DailyCodesUpdate(BaseModel):
    codes: str = Field(max_length=500)


class ListeningHold(BaseModel):
    seconds: float = Field(ge=0, le=120)


class RuntimeSettingsUpdate(BaseModel):
    values: dict[str, bool | int | float]
    expected_revision: int = Field(ge=0)


class TextListEntry(BaseModel):
    raw_line: str = Field(max_length=300)
    enabled: bool = Field(strict=True)


class TextListCheck(BaseModel):
    entries: list[TextListEntry] = Field(max_length=1000)


class TextListUpdate(BaseModel):
    entries: list[TextListEntry] = Field(max_length=500)
    expected_revision: int = Field(ge=0)


class DictionaryUpdate(BaseModel):
    entries: list[TextListEntry] = Field(max_length=1000)
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuleApproval(BaseModel):
    approved: bool = Field(strict=True)
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuleUpdate(BaseModel):
    disabled: bool = Field(strict=True)
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class UserUpdate(BaseModel):
    role: Literal["admin", "editor", "viewer"]
    enabled: bool = Field(strict=True)
    expected_version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    display_name: str | None = Field(default=None, max_length=80)
    notify_mode: Literal["all", "watch", "commands", "off"] | None = None


class RecognitionValues(BaseModel):
    USE_LLM: bool = Field(strict=True)
    VAD_LEVEL: int = Field(ge=0, le=3, strict=True)
    SILENCE_END_MS: int = Field(ge=150, le=3000, strict=True)
    MIN_SPEECH_MS: int = Field(ge=90, le=3000, strict=True)
    MAX_SEGMENT_S: int = Field(ge=3, le=60, strict=True)

    @model_validator(mode="after")
    def valid_duration(self):
        if self.MIN_SPEECH_MS >= self.MAX_SEGMENT_S * 1000:
            raise ValueError("Мінімальна тривалість мовлення має бути меншою за максимальний сегмент")
        return self


class SettingsUpdate(BaseModel):
    values: RecognitionValues
    expected_revision: int = Field(ge=0, strict=True)


class PauseUpdate(BaseModel):
    minutes: int = Field(ge=0, le=60, strict=True)
    expected_revision: int = Field(ge=0, strict=True)


class SampleConfirmation(BaseModel):
    confirmed_isolated: bool = Field(strict=True)
