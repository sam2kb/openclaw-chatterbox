"""Private audio review, checksum-bound QA, and durable master retention."""

from __future__ import annotations

import difflib
import hmac
import json
import re
import unicodedata
from html import escape
from typing import Literal

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import Field

from .errors import AuthenticationError, ConflictError, NotFoundError, ValidationFailure
from .schemas import StrictModel
from .util import iso_from_ms, new_id, new_secret, now_ms, token_hash, validate_id


class AudioMetrics(StrictModel):
    frames: int = Field(ge=0)
    sample_rate: int = Field(ge=1)
    channels: int = Field(ge=1)
    duration_ms: int = Field(ge=0)
    peak_dbfs: float | None = Field(default=None, allow_inf_nan=False)
    rms_dbfs: float | None = Field(default=None, allow_inf_nan=False)
    clipped_fraction: float = Field(ge=0, le=1)


class QualityRequest(StrictModel):
    audio_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    transcript: str = Field(max_length=100000)
    verifier: str = Field(default="owner-supplied-transcript", max_length=100)
    audio_metrics: AudioMetrics | None = None


class RetainRequest(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    audio_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    owner_approved: bool = False


class ListeningRequest(StrictModel):
    audio_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["accepted", "rejected"]


class ReviewRequest(StrictModel):
    artifact_ids: list[str] = Field(min_length=1, max_length=12)


def compare_words(expected: str, observed: str) -> dict:
    def words(s):
        return re.findall(r"\w+(?:['’]\w+)?", unicodedata.normalize("NFKC", s).casefold())

    left, right = words(expected), words(observed)
    differences = [
        {"operation": tag, "expected": left[a:b], "observed": right[c:d]}
        for tag, a, b, c, d in difflib.SequenceMatcher(
            None, left, right, autojunk=False
        ).get_opcodes()
        if tag != "equal"
    ]
    return {
        "status": "asr_agreement" if left and left == right else "needs_review",
        "differences": differences,
        "expected_words": len(left),
        "observed_words": len(right),
        "proves_spoken_words": False,
        "notice": "ASR can mishear articles, names, numbers, or final words. Listen before approving.",
    }


def register_review_routes(
    app, *, settings, store, storage, require_service, artifact_response, web_dir
):
    def artifact(aid):
        try:
            validate_id(aid, prefixes=("art",))
        except ValueError as exc:
            raise NotFoundError("artifact not found") from exc
        return store.get_artifact_row(aid)

    def expected_text(row):
        job = store.get_job_row(row["job_id"])
        payload = json.loads(job["input_json"])
        if payload.get("text") is not None:
            return payload["text"]
        return " ".join(s.get("text", "") for s in payload.get("segments", []) if "text" in s)

    def check_hash(row, digest):
        if not hmac.compare_digest(row["sha256"], digest):
            raise ConflictError("review checksum does not match this audio artifact")

    def update(aid, body, kind):
        # Read/modify/write under one lock so parallel review decisions cannot
        # erase one another or race artifact expiry.
        with store.db.transaction(immediate=True) as conn:
            record = conn.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
            if record is None:
                raise NotFoundError("artifact not found")
            row = dict(record)
            check_hash(row, body.audio_sha256)
            manifest = json.loads(row["manifest_json"])
            quality = manifest.setdefault("quality", {})
            if kind == "asr":
                text = expected_text(row)
                if not text.strip():
                    raise ConflictError("input text has expired; wording comparison is unavailable")
                quality["wording"] = {
                    **compare_words(text, body.transcript),
                    "audio_sha256": row["sha256"],
                    "transcript": body.transcript,
                    "verifier": body.verifier,
                    "checked_at": iso_from_ms(now_ms()),
                }
                if body.audio_metrics is not None:
                    metrics = body.audio_metrics.model_dump()
                    quality["signal"] = {
                        **metrics,
                        "audio_sha256": row["sha256"],
                        "possible_clipping": metrics["clipped_fraction"] > 0.001,
                        "very_quiet": metrics["rms_dbfs"] is None or metrics["rms_dbfs"] < -50,
                        "notice": "Signal measurements do not judge naturalness or speaker similarity.",
                    }
            elif kind == "listen":
                quality["listening"] = {
                    "decision": body.decision,
                    "audio_sha256": row["sha256"],
                    "recorded_at": iso_from_ms(now_ms()),
                    "source": "owner_attestation",
                }
                if manifest.get("asset"):
                    manifest["asset"]["owner_approved"] = body.decision == "accepted"
            elif kind == "retain":
                if row["kind"] != "master":
                    raise ValidationFailure("retain the original master, not a derivative")
                name = " ".join(body.name.split())
                if not name or any(unicodedata.category(c).startswith("C") for c in name):
                    raise ValidationFailure("asset name must be plain text")
                manifest["asset"] = {
                    "name": name,
                    "retained_at": iso_from_ms(now_ms()),
                    "owner_approved": body.owner_approved,
                    "audio_sha256": row["sha256"],
                }
                conn.execute("UPDATE artifacts SET expires_at=NULL WHERE id=?", (aid,))
                if body.owner_approved:
                    quality["listening"] = {
                        "decision": "accepted",
                        "audio_sha256": row["sha256"],
                        "recorded_at": iso_from_ms(now_ms()),
                        "source": "owner_attestation",
                    }
            conn.execute(
                "UPDATE artifacts SET manifest_json=? WHERE id=?", (json.dumps(manifest), aid)
            )
        return store.get_artifact(aid)

    @app.post(
        "/api/v1/voices/{voice_id}/archive",
        dependencies=[Depends(require_service)],
        tags=["voices"],
    )
    def archive_voice(voice_id: str, body: dict):
        if body:
            raise ValidationFailure("archive does not accept extra options")
        return store.archive_voice(voice_id)

    @app.post(
        "/api/v1/voices/{voice_id}/restore",
        dependencies=[Depends(require_service)],
        tags=["voices"],
    )
    def restore_voice(voice_id: str, body: dict):
        if body:
            raise ValidationFailure("restore does not accept extra options")
        return store.archive_voice(voice_id, restore=True)

    @app.post(
        "/api/v1/artifacts/{artifact_id}/verify",
        dependencies=[Depends(require_service)],
        tags=["review"],
    )
    def verify(artifact_id: str, body: QualityRequest):
        artifact(artifact_id)
        return update(artifact_id, body, "asr")

    @app.post(
        "/api/v1/artifacts/{artifact_id}/retain",
        dependencies=[Depends(require_service)],
        tags=["review"],
    )
    def retain(artifact_id: str, body: RetainRequest):
        artifact(artifact_id)
        return update(artifact_id, body, "retain")

    @app.get("/api/v1/assets", dependencies=[Depends(require_service)], tags=["review"])
    def assets():
        with store.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE expires_at IS NULL ORDER BY created_at DESC"
            ).fetchall()
        return {
            "assets": [
                store.public_artifact(dict(r))
                for r in rows
                if json.loads(r["manifest_json"]).get("asset")
            ]
        }

    def review_record(review_id, request):
        try:
            validate_id(review_id, prefixes=("rev",))
            record = json.loads(store.get_setting("review:" + review_id) or "null")
        except (ValueError, json.JSONDecodeError):
            record = None
        auth = request.headers.get("authorization", "")
        if (
            not record
            or not auth.startswith("Bearer ")
            or not hmac.compare_digest(record["token_hash"], token_hash(auth[7:]))
        ):
            raise AuthenticationError("invalid review credential")
        if record["expires_at"] and record["expires_at"] <= now_ms():
            raise AuthenticationError("review link expired; request a fresh link")
        return record

    @app.post("/api/v1/reviews", dependencies=[Depends(require_service)], tags=["review"])
    def create_review(body: ReviewRequest):
        ids = list(dict.fromkeys(body.artifact_ids))
        rows = [artifact(aid) for aid in ids]
        if any(
            row["kind"] != "master" or row["sample_rate"] != 24000 or row["channels"] != 1
            for row in rows
        ):
            raise ValidationFailure(
                "private reviews accept only original 24 kHz mono masters; use the job's master artifact"
            )
        expiry = min((r["expires_at"] for r in rows if r["expires_at"] is not None), default=None)
        if expiry and expiry <= now_ms():
            raise ConflictError("one or more audio artifacts have expired")
        rid, token = new_id("rev"), new_secret(32)
        store.set_setting(
            "review:" + rid,
            json.dumps(
                {"token_hash": token_hash(token), "artifact_ids": ids, "expires_at": expiry}
            ),
        )
        return {
            "id": rid,
            "expires_at": iso_from_ms(expiry),
            "player_url": f"{settings.public_base_url.rstrip('/')}/review/{rid}#token={token}",
            "notice": "Private view/listen credential; keep it in the owner conversation.",
        }

    @app.delete(
        "/api/v1/reviews/{review_id}", dependencies=[Depends(require_service)], tags=["review"]
    )
    def revoke_review(review_id: str):
        with store.db.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM settings WHERE key=?", ("review:" + review_id,))
        return {"revoked": True}

    @app.get("/api/v1/reviews/{review_id}", tags=["review"])
    def show_review(review_id: str, request: Request):
        record = review_record(review_id, request)
        return {
            "expires_at": iso_from_ms(record["expires_at"]),
            "artifacts": [
                {**store.get_artifact(aid), "expected_text": expected_text(artifact(aid))}
                for aid in record["artifact_ids"]
            ],
        }

    @app.get("/api/v1/reviews/{review_id}/artifacts/{artifact_id}/download", tags=["review"])
    def review_audio(review_id: str, artifact_id: str, request: Request):
        record = review_record(review_id, request)
        if artifact_id not in record["artifact_ids"]:
            raise NotFoundError("artifact not in this review")
        return artifact_response(artifact(artifact_id))

    @app.post("/api/v1/reviews/{review_id}/artifacts/{artifact_id}/listen", tags=["review"])
    def listening(review_id: str, artifact_id: str, request: Request, body: ListeningRequest):
        record = review_record(review_id, request)
        if artifact_id not in record["artifact_ids"]:
            raise NotFoundError("artifact not in this review")
        return update(artifact_id, body, "listen")

    @app.get("/review/{review_id}", include_in_schema=False)
    def player_page(review_id: str):
        try:
            validate_id(review_id, prefixes=("rev",))
        except ValueError as exc:
            raise NotFoundError("review not found") from exc
        document = (web_dir / "review.html").read_text()
        prefix = escape(settings.public_base_path, quote=True)
        document = document.replace("__CLAWVOICE_BASE_PATH__", prefix).replace(
            '"/static/', f'"{prefix}/static/'
        )
        return HTMLResponse(document, headers={"Cache-Control": "no-store"})
