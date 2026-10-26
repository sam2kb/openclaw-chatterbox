import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from clawvoice.client import ClientConfigurationError
from clawvoice.review import compare_words
from clawvoice.workflow import cleanup_library, deliver, verify_artifact
from tests.helpers import create_direct_voice, wait_job


def rendered(client, headers, reference, text="Please leave a text message. Thank you."):
    voice = create_direct_voice(client, headers, reference)
    request = client.post(
        "/api/v1/synthesis-jobs",
        headers=headers,
        json={"voice_id": voice["voice"]["id"], "text": text},
    )
    job = wait_job(client, headers, request.json()["job"]["id"])
    return voice, job, next(a for a in job["artifacts"] if a["kind"] == "master")


def test_review_is_audio_bound_and_retention_keeps_exact_master(client, headers, reference):
    voice, job, a = rendered(client, headers, reference)
    path = f"/api/v1/artifacts/{a['id']}"
    bad = client.post(
        path + "/verify",
        headers=headers,
        json={"audio_sha256": "0" * 64, "transcript": "wrong audio"},
    )
    assert bad.status_code == 409
    result = client.post(
        path + "/verify",
        headers=headers,
        json={"audio_sha256": a["sha256"], "transcript": "Please leave a text message. Thank you."},
    )
    assert result.status_code == 200, result.text
    quality = result.json()["manifest"]["quality"]["wording"]
    assert quality["status"] == "asr_agreement" and quality["proves_spoken_words"] is False
    assert quality["audio_sha256"] == a["sha256"]
    kept = client.post(
        path + "/retain",
        headers=headers,
        json={"audio_sha256": a["sha256"], "name": "Approved greeting", "owner_approved": True},
    )
    assert kept.status_code == 200 and kept.json()["expires_at"] is None
    assert kept.json()["sha256"] == a["sha256"]
    store = client.app.state.store
    store.expire_artifacts(now=10**15)
    store.redact_terminal_job_texts(cutoff_ms=10**15)
    assert client.get(path, headers=headers).status_code == 200
    assert "Thank you" in store.get_job_row(job["id"])["input_json"]
    assert (
        client.delete(f"/api/v1/voices/{voice['voice']['id']}", headers=headers).status_code == 409
    )


def test_private_player_scope_expiry_and_revocation(client, headers, reference):
    _, _, a = rendered(client, headers, reference)
    _, _, other = rendered(client, headers, reference, text="An unrelated take.")
    response = client.post("/api/v1/reviews", headers=headers, json={"artifact_ids": [a["id"]]})
    assert response.status_code == 200
    session = response.json()
    scoped = {"Authorization": "Bearer " + session["player_url"].split("#token=", 1)[1]}
    path = "/api/v1/reviews/" + session["id"]
    assert client.get(path).status_code == 401
    show = client.get(path, headers=scoped)
    assert show.status_code == 200 and len(show.json()["artifacts"]) == 1
    assert show.json()["artifacts"][0]["expected_text"].endswith("Thank you.")
    assert client.get(path + f"/artifacts/{a['id']}/download", headers=scoped).status_code == 200
    assert (
        client.get(path + f"/artifacts/{other['id']}/download", headers=scoped).status_code == 404
    )
    assert client.get("/api/v1/voices", headers=scoped).status_code == 401
    listen = client.post(
        path + f"/artifacts/{a['id']}/listen",
        headers=scoped,
        json={"audio_sha256": a["sha256"], "decision": "accepted"},
    )
    assert listen.status_code == 200
    assert (
        client.post(
            f"/api/v1/artifacts/{a['id']}/retain",
            headers=scoped,
            json={"audio_sha256": a["sha256"], "name": "No"},
        ).status_code
        == 401
    )
    assert client.delete(path, headers=headers).status_code == 200
    assert client.get(path, headers=scoped).status_code == 401


def test_asr_does_not_hide_article_changes_or_missing_ending():
    assert (
        compare_words("a text message. Thank you.", "the text message")["status"] == "needs_review"
    )
    assert len(compare_words("a text message. Thank you.", "the text message")["differences"]) == 2
    assert compare_words("Hello, world!", "hello world")["status"] == "asr_agreement"


class AudioService:
    def __init__(self, audio):
        self.audio = audio
        self.sha = hashlib.sha256(audio).hexdigest()
        self.calls = 0
        self.records = []

    def get_artifact(self, aid):
        return {
            "id": aid,
            "kind": "master",
            "sha256": self.sha,
            "expires_at": "2099-01-01T00:00:00Z",
            "duration_ms": 6500,
            "sample_rate": 24000,
            "channels": 1,
        }

    def fetch_artifact(self, aid, output):
        self.calls += 1
        Path(output).write_bytes(self.audio)

    def _json(self, method, path, body=None):
        self.records.append(body)
        return {"recorded": body}


def test_delivery_always_emits_readable_media_and_dedupes_only_by_checksum(
    tmp_path, monkeypatch, reference
):
    root = tmp_path / "library"
    monkeypatch.setenv("CLAWVOICE_LIBRARY_DIR", str(root))
    service = AudioService(reference.read_bytes())
    receipt = deliver(service, ["art_one", "art_two"])
    lines = receipt["reply"].splitlines()
    assert lines[-1].startswith("MEDIA:") and sum(line.startswith("MEDIA:") for line in lines) == 1
    assert Path(lines[-1][6:]).read_bytes() == service.audio
    assert service.calls == 1
    deliver(service, ["art_one"])
    assert service.calls == 1
    Path(lines[-1][6:]).write_bytes(b"tampered")
    with pytest.raises(ClientConfigurationError, match="checksum"):
        deliver(service, ["art_one"])


def test_local_cleanup_does_not_touch_retained_or_unmanaged_files(tmp_path, monkeypatch, reference):
    root = tmp_path / "library"
    monkeypatch.setenv("CLAWVOICE_LIBRARY_DIR", str(root))
    service = AudioService(reference.read_bytes())
    receipt = deliver(service, ["art_one"])
    path = Path(receipt["files"][0]["path"])
    metadata = path.with_suffix(".json")
    r = json.loads(metadata.read_text())
    r["expires_at"] = None
    metadata.write_text(json.dumps(r))
    unmanaged = root / "do-not-delete.wav"
    unmanaged.write_bytes(b"keep")
    assert cleanup_library()["expired_files"] == 0
    r["expires_at"] = "2000-01-01T00:00:00Z"
    metadata.write_text(json.dumps(r))
    assert cleanup_library(dry_run=True)["expired_files"] == 1 and path.exists()
    assert cleanup_library()["expired_files"] == 1 and not path.exists()
    assert unmanaged.read_bytes() == b"keep"


def test_asr_staging_always_cleans_only_its_own_directory(tmp_path, monkeypatch, reference):
    monkeypatch.setenv("CLAWVOICE_ASR_STAGING_ROOT", str(tmp_path))
    sentinel = tmp_path / "unrelated"
    sentinel.write_text("keep")
    service = AudioService(reference.read_bytes())

    def transcribe(args, **kwargs):
        path = Path(args[1])
        assert path.parent.parent == tmp_path and path.read_bytes() == service.audio
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"text": "Please leave a message.", "model": "medium"})
        )

    monkeypatch.setattr(subprocess, "run", transcribe)
    verify_artifact(service, "art_one")
    assert set(tmp_path.iterdir()) == {sentinel, reference}
    assert service.records[0]["audio_sha256"] == service.sha
    assert service.records[0]["audio_metrics"]["sample_rate"] == 24000
    assert service.records[0]["audio_metrics"]["clipped_fraction"] == 0

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("transcribe", 600)

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ClientConfigurationError):
        verify_artifact(service, "art_one")
    assert set(tmp_path.iterdir()) == {sentinel, reference}


def test_archiving_a_voice_is_reversible_and_preserves_production_asset(client, headers, reference):
    voice, _, artifact = rendered(client, headers, reference)
    voice_id = voice["voice"]["id"]
    assert (
        client.post(
            f"/api/v1/artifacts/{artifact['id']}/retain",
            headers=headers,
            json={"audio_sha256": artifact["sha256"], "name": "Keep this"},
        ).status_code
        == 200
    )
    assert (
        client.post(f"/api/v1/voices/{voice_id}/archive", headers=headers, json={}).status_code
        == 200
    )
    assert client.get("/api/v1/voices", headers=headers).json()["voices"] == []
    all_voices = client.get("/api/v1/voices?include_archived=true", headers=headers).json()[
        "voices"
    ]
    assert len(all_voices) == 1 and all_voices[0]["status"] == "archived"
    assert (
        client.get(f"/api/v1/artifacts/{artifact['id']}/download", headers=headers).status_code
        == 200
    )
    assert (
        client.post(f"/api/v1/voices/{voice_id}/restore", headers=headers, json={}).status_code
        == 200
    )
    assert (
        client.get(f"/api/v1/voices/{voice_id}", headers=headers).json()["reference"]["id"]
        == voice["voice"]["reference"]["id"]
    )


def test_private_review_cannot_mislabel_a_legacy_derivative_as_original(client, headers, reference):
    voice = create_direct_voice(client, headers, reference)
    job = client.post(
        "/api/v1/synthesis-jobs",
        headers=headers,
        json={
            "voice_id": voice["voice"]["id"],
            "text": "Legacy compatibility check.",
            "preset": "telephony",
        },
    ).json()["job"]
    finished = wait_job(client, headers, job["id"])
    derivative = next(a for a in finished["artifacts"] if a["kind"] == "output")
    result = client.post(
        "/api/v1/reviews", headers=headers, json={"artifact_ids": [derivative["id"]]}
    )
    assert result.status_code == 422
