"""Versioned request schemas. Unsupported fields are rejected, not ignored."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnrollmentSessionRequest(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    voice_id: str | None = Field(default=None, min_length=6, max_length=64)

    @model_validator(mode="after")
    def one_target(self):
        if (self.name is None) == (self.voice_id is None):
            raise ValueError("provide a name for a new voice or a voice_id to replace its reference")
        return self


class EnrollmentVoiceRequest(StrictModel):
    upload_id: str = Field(min_length=6, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    authorized: bool
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)


class VoiceRenameRequest(StrictModel):
    name: str = Field(min_length=1, max_length=80)


class SynthesisControls(StrictModel):
    temperature: float | None = Field(default=None, ge=0.05, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=1, le=10_000)
    repetition_penalty: float | None = Field(default=None, ge=0.5, le=2.0)


class TextSegment(StrictModel):
    type: Literal["text"] | None = None
    text: str


class PauseSegment(StrictModel):
    type: Literal["pause"] | None = None
    pause_ms: int


Segment = Annotated[TextSegment | PauseSegment, Field(description="A text or pause segment")]


class SynthesisRequest(StrictModel):
    voice_id: str | None = Field(default=None, min_length=6, max_length=64)
    text: str | None = None
    segments: list[TextSegment | PauseSegment] | None = None
    preset: Literal["master", "mp3", "telephony", "telephony-mulaw"] = "master"
    controls: SynthesisControls = Field(default_factory=SynthesisControls)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)

    @model_validator(mode="after")
    def exactly_one_input(self) -> SynthesisRequest:
        if (self.text is None) == (self.segments is None):
            raise ValueError("provide exactly one of text or segments")
        return self

    def input_payload(self) -> dict:
        if self.text is not None:
            return {"text": self.text}
        return {
            "segments": [
                segment.model_dump(exclude_none=True) for segment in (self.segments or [])
            ]
        }

    def controls_payload(self) -> dict:
        return self.controls.model_dump(exclude_none=True)


class EnrollmentSessionResponse(StrictModel):
    id: str
    name: str
    status: str
    expires_at: str | None
    recorder_url: str | None = None
    create_voice_once: bool = True


class ErrorDetail(StrictModel):
    code: str
    message: str
    retryable: bool = False
    details: dict = Field(default_factory=dict)


class ErrorResponse(StrictModel):
    error: ErrorDetail
