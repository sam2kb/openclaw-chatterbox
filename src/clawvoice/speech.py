"""Text/segment planning. Preserve supplied text; never silently truncate."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .errors import ValidationFailure

_TOKEN = re.compile(r"\S+\s*")
_ALLOWED_CONTROL_KEYS = {
    "temperature",
    "top_p",
    "top_k",
    "repetition_penalty",
}

@dataclass
class TextChunk:
    text: str


@dataclass
class TextPiece:
    original: str
    sha256: str
    chars: int
    chunks: list[TextChunk] = field(default_factory=list)


@dataclass
class PausePiece:
    pause_ms: int


Piece = TextPiece | PausePiece


@dataclass
class SpeechPlan:
    pieces: list[Piece]
    controls: dict[str, Any]
    seed: int
    total_chars: int
    total_pause_ms: int
    chunk_count: int
    text_sha256: str

    @property
    def text_pieces(self) -> list[TextPiece]:
        return [p for p in self.pieces if isinstance(p, TextPiece)]


def default_controls() -> dict[str, Any]:
    return {
        "temperature": 0.8,
        "top_p": 0.95,
        "top_k": 1000,
        "repetition_penalty": 1.2,
    }


def validate_controls(value: dict[str, Any] | None) -> dict[str, Any]:
    controls = default_controls()
    if value is None:
        return controls
    if not isinstance(value, dict):
        raise ValidationFailure("controls must be a JSON object")
    extra = set(value) - _ALLOWED_CONTROL_KEYS
    if extra:
        raise ValidationFailure(
            "unsupported synthesis controls for the pinned Turbo backend",
            details={"unsupported": sorted(extra), "supported": sorted(_ALLOWED_CONTROL_KEYS)},
        )
    if "temperature" in value:
        try:
            cast = float(value["temperature"])
        except (TypeError, ValueError) as exc:
            raise ValidationFailure("temperature must be a number") from exc
        if not 0.05 <= cast <= 2.0:
            raise ValidationFailure("temperature must be between 0.05 and 2.0")
        controls["temperature"] = cast
    if "top_p" in value:
        try:
            cast = float(value["top_p"])
        except (TypeError, ValueError) as exc:
            raise ValidationFailure("top_p must be a number") from exc
        if not 0.0 <= cast <= 1.0:
            raise ValidationFailure("top_p must be between 0 and 1")
        controls["top_p"] = cast
    if "top_k" in value:
        try:
            cast = int(value["top_k"])
        except (TypeError, ValueError) as exc:
            raise ValidationFailure("top_k must be an integer") from exc
        if not 1 <= cast <= 10000:
            raise ValidationFailure("top_k must be between 1 and 10000")
        controls["top_k"] = cast
    if "repetition_penalty" in value:
        try:
            cast = float(value["repetition_penalty"])
        except (TypeError, ValueError) as exc:
            raise ValidationFailure("repetition_penalty must be a number") from exc
        if not 0.5 <= cast <= 2.0:
            raise ValidationFailure("repetition_penalty must be between 0.5 and 2.0")
        controls["repetition_penalty"] = cast
    return controls


def validate_seed(seed: int | None) -> int:
    if seed is None:
        import secrets

        return secrets.randbelow(2**31 - 1)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31 - 1:
        raise ValidationFailure("seed must be an integer between 0 and 2147483647")
    return seed


def split_text(text: str, target_chars: int, *, max_chunks: int) -> list[str]:
    """Split only at whitespace boundaries, preserving every character.

    The concatenation of the returned chunks is exactly the input string. A
    single overlong token is rejected rather than truncated.
    """
    if not isinstance(text, str):
        raise ValidationFailure("text must be a string")
    if not text.strip():
        raise ValidationFailure("text cannot be empty or whitespace only")
    if len(text) > 1_000_000:
        raise ValidationFailure("text is unreasonably large")
    tokens = [m.group(0) for m in _TOKEN.finditer(text)]
    if not tokens:
        raise ValidationFailure("text cannot be empty")
    if any(len(token) > target_chars for token in tokens):
        raise ValidationFailure(
            "a single word/token is too long for the synthesis chunk limit",
            details={"target_chars": target_chars},
        )
    chunks: list[str] = []
    current = ""
    for token in tokens:
        if current and len(current) + len(token) > target_chars:
            chunks.append(current)
            if len(chunks) > max_chunks:
                raise ValidationFailure(
                    "the text requires too many synthesis chunks",
                    details={"max_chunks": max_chunks},
                )
            current = token
        else:
            current += token
    if current:
        chunks.append(current)
    if len(chunks) > max_chunks:
        raise ValidationFailure(
            "the text requires too many synthesis chunks",
            details={"max_chunks": max_chunks},
        )
    if "".join(chunks) != text:
        raise AssertionError("text chunking must preserve the original text")
    return chunks


def build_plan(
    payload: dict[str, Any],
    *,
    settings: Settings,
    controls: dict[str, Any] | None = None,
    seed: int | None = None,
) -> SpeechPlan:
    if not isinstance(payload, dict):
        raise ValidationFailure("synthesis input must be an object")
    allowed_keys = {"text", "segments"}
    extra = set(payload) - allowed_keys
    if extra:
        raise ValidationFailure("unexpected synthesis input fields", details={"fields": sorted(extra)})
    has_text = "text" in payload
    has_segments = "segments" in payload
    if has_text == has_segments:
        raise ValidationFailure("provide exactly one of text or segments")
    if has_text:
        text = payload["text"]
        if not isinstance(text, str):
            raise ValidationFailure("text must be a string")
        if len(text) > settings.max_text_chars:
            raise ValidationFailure(
                "text exceeds the configured character limit",
                details={"length": len(text), "max_chars": settings.max_text_chars},
            )
        chunks = split_text(
            text, settings.chunk_target_chars, max_chunks=settings.max_chunks_per_text
        )
        piece = TextPiece(
            original=text,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            chars=len(text),
            chunks=[TextChunk(c) for c in chunks],
        )
        pieces: list[Piece] = [piece]
        total_chars = len(text)
        total_pause = 0
        chunk_count = len(chunks)
        text_sha = piece.sha256
    else:
        raw_segments = payload["segments"]
        if not isinstance(raw_segments, list) or not raw_segments:
            raise ValidationFailure("segments must be a non-empty array")
        if len(raw_segments) > settings.max_segments:
            raise ValidationFailure(
                "too many segments",
                details={"count": len(raw_segments), "max": settings.max_segments},
            )
        pieces = []
        total_chars = 0
        total_pause = 0
        chunk_count = 0
        aggregate: list[str] = []
        has_any_text = False
        for index, segment in enumerate(raw_segments):
            if not isinstance(segment, dict):
                raise ValidationFailure(f"segment {index} must be an object")
            keys = set(segment)
            if "text" in keys and "pause_ms" in keys:
                raise ValidationFailure(f"segment {index} cannot contain both text and pause_ms")
            if "text" in keys:
                if set(keys) - {"text", "type"}:
                    raise ValidationFailure(f"segment {index} has unexpected fields")
                text = segment["text"]
                if not isinstance(text, str) or not text.strip():
                    raise ValidationFailure(f"segment {index} text cannot be empty")
                has_any_text = True
                total_chars += len(text)
                if total_chars > settings.max_text_chars:
                    raise ValidationFailure(
                        "segments exceed the configured character limit",
                        details={"max_chars": settings.max_text_chars},
                    )
                max_chunks = settings.max_chunks_per_text - chunk_count
                if max_chunks <= 0:
                    raise ValidationFailure(
                        "segments exceed the configured chunk limit",
                        details={"max_chunks": settings.max_chunks_per_text},
                    )
                chunks = split_text(
                    text, settings.chunk_target_chars, max_chunks=max_chunks
                )
                chunk_count += len(chunks)
                aggregate.append(text)
                pieces.append(
                    TextPiece(
                        original=text,
                        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        chars=len(text),
                        chunks=[TextChunk(c) for c in chunks],
                    )
                )
            elif "pause_ms" in keys:
                if set(keys) - {"pause_ms", "type"}:
                    raise ValidationFailure(f"segment {index} has unexpected fields")
                pause = segment["pause_ms"]
                if isinstance(pause, bool) or not isinstance(pause, int):
                    raise ValidationFailure(f"segment {index} pause_ms must be an integer")
                if pause < 0 or pause > settings.max_pause_ms:
                    raise ValidationFailure(
                        f"segment {index} pause_ms is outside the allowed range",
                        details={"pause_ms": pause, "max_pause_ms": settings.max_pause_ms},
                    )
                total_pause += pause
                if total_pause > settings.max_total_pause_ms:
                    raise ValidationFailure(
                        "total requested silence is too long",
                        details={"total_pause_ms": total_pause, "max_ms": settings.max_total_pause_ms},
                    )
                pieces.append(PausePiece(pause_ms=pause))
            else:
                raise ValidationFailure(f"segment {index} must contain text or pause_ms")
        if not has_any_text:
            raise ValidationFailure("at least one text segment is required")
        text_sha = hashlib.sha256("\n".join(aggregate).encode("utf-8")).hexdigest()
    return SpeechPlan(
        pieces=pieces,
        controls=validate_controls(controls),
        seed=validate_seed(seed),
        total_chars=total_chars,
        total_pause_ms=total_pause,
        chunk_count=chunk_count,
        text_sha256=text_sha,
    )


def describe_plan(plan: SpeechPlan) -> dict[str, Any]:
    return {
        "total_chars": plan.total_chars,
        "total_pause_ms": plan.total_pause_ms,
        "chunks": plan.chunk_count,
        "segments": len(plan.pieces),
        "text_sha256": plan.text_sha256,
        "controls": plan.controls,
        "seed": plan.seed,
    }
