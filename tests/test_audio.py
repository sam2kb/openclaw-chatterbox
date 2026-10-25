from __future__ import annotations

import wave
from pathlib import Path

import pytest

from clawvoice.audio import AudioDecodeError, AudioProcessor
from clawvoice.errors import ValidationFailure
from clawvoice.speech import build_plan, split_text
from tests.helpers import write_wav


def test_split_text_preserves_every_character():
    text = "First sentence. Second sentence has more words.\n\nThird line!\tAnd a tab."
    from clawvoice.config import Settings
    from clawvoice.util import new_secret

    settings = Settings(
        data_dir=Path("/tmp/unused-audio-test"),
        db_path=Path("/tmp/unused-audio-test/db"),
        model_dir=Path("/tmp/unused-audio-test/model"),
        token_file=Path("/tmp/unused-audio-test/token"),
        service_token=new_secret(32),
        backend="fake",
        device="cpu",
        allow_fake_backend=True,
        chunk_target_chars=40,
    )
    plan = build_plan({"text": text}, settings=settings, seed=1)
    chunks = [c.text for c in plan.text_pieces[0].chunks]
    assert "".join(chunks) == text
    assert len(chunks) > 1


def test_split_text_rejects_overlong_token():
    with pytest.raises(ValidationFailure):
        split_text("x" * 500, 100, max_chunks=4)


def test_native_rate_and_exact_pause(tmp_path, client, headers, reference):
    from tests.helpers import create_direct_voice, wait_job

    result = create_direct_voice(client, headers, reference)
    voice = result["voice"]
    response = client.post(
        "/api/v1/synthesis-jobs",
        headers=headers,
        json={
            "voice_id": voice["id"],
            "segments": [
                {"type": "text", "text": "Please hold."},
                {"type": "pause", "pause_ms": 2000},
                {"type": "text", "text": "Press 2 for billing."},
            ],
            "preset": "master",
            "seed": 10,
        },
    )
    assert response.status_code == 202, response.text
    job = wait_job(client, headers, response.json()["job"]["id"])
    assert job["status"] == "succeeded", job
    master = next(a for a in job["artifacts"] if a["kind"] == "master")
    assert master["sample_rate"] == 24000
    assert master["channels"] == 1
    download = client.get(f"/api/v1/artifacts/{master['id']}/download", headers=headers)
    assert download.status_code == 200
    path = tmp_path / "master.wav"
    path.write_bytes(download.content)
    with wave.open(str(path), "rb") as wav:
        assert wav.getframerate() == 24000
        assert wav.getnchannels() == 1
        frames = wav.readframes(wav.getnframes())
    pcm = frames
    longest = 0
    run = 0
    for i in range(0, len(pcm), 2):
        sample = pcm[i : i + 2]
        if sample == b"\x00\x00":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    # 2000 ms at 24 kHz is 48,000 samples. Assembly inserts exact zero PCM;
    # the natural fake tone's zero crossings are single samples.
    assert longest >= 48_000
    assert longest <= 48_100


def test_telephony_and_mulaw_are_derived(tmp_path, client, headers, reference):
    from tests.helpers import create_direct_voice, wait_job

    result = create_direct_voice(client, headers, reference)
    for preset, expected_rate, ext in (("telephony", 8000, "wav"), ("mp3", 24000, "mp3"), ("telephony-mulaw", 8000, "ulaw")):
        response = client.post(
            "/api/v1/synthesis-jobs",
            headers=headers,
            json={"voice_id": result["voice"]["id"], "text": "Hello there.", "preset": preset, "seed": 3},
        )
        assert response.status_code == 202, response.text
        job = wait_job(client, headers, response.json()["job"]["id"])
        assert job["status"] == "succeeded", job
        assert {a["kind"] for a in job["artifacts"]} == {"master", "output"}
        output = next(a for a in job["artifacts"] if a["kind"] == "output")
        assert output["sample_rate"] == expected_rate
        download = client.get(f"/api/v1/artifacts/{output['id']}/download", headers=headers)
        assert download.status_code == 200
        target = tmp_path / f"out.{ext}"
        target.write_bytes(download.content)
        assert download.content[:3] != b"ID3" or preset == "mp3"
        if preset == "telephony":
            with wave.open(str(target), "rb") as wav:
                assert wav.getframerate() == 8000
                assert wav.getnchannels() == 1


def test_malformed_and_oversized_uploads(client, headers, settings):
    settings.max_upload_bytes = 256
    bad = client.post(
        "/api/v1/voices",
        params={"name": "bad", "authorized": "true"},
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=b"not really audio",
    )
    assert bad.status_code == 422, bad.text
    assert bad.json()["error"]["code"] == "audio_decode_failed"

    oversized = client.post(
        "/api/v1/enrollment-sessions",
        headers=headers,
        json={"name": "x"},
    )
    assert oversized.status_code == 201
    session_id = oversized.json()["id"]
    token = oversized.json()["recorder_url"].split("#token=", 1)[1]
    response = client.post(
        f"/api/v1/enrollment-sessions/{session_id}/uploads",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"},
        content=b"x" * 512,
    )
    assert response.status_code == 413, response.text


def test_decoder_timeout_is_actionable(client, headers, reference, monkeypatch):


    def timeout(self, args, *, timeout):
        raise AudioDecodeError("audio decoding timed out; try a shorter or simpler file")

    monkeypatch.setattr(AudioProcessor, "_run", timeout)
    response = client.post(
        "/api/v1/voices",
        params={"name": "timeout", "authorized": "true"},
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=reference.read_bytes(),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "audio_decode_failed"


def test_segment_too_short_rejected(client, headers, reference):
    response = client.post(
        "/api/v1/voices",
        params={"name": "short", "authorized": "true", "start_ms": 0, "end_ms": 5000},
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=reference.read_bytes(),
    )
    assert response.status_code == 422
    assert "more than five seconds" in response.json()["error"]["message"]


def test_artifacts_symlink_and_tampered_path_are_not_served(client, headers, reference, settings):
    from clawvoice.store import Store
    from tests.helpers import create_direct_voice, wait_job

    result = create_direct_voice(client, headers, reference)
    response = client.post(
        "/api/v1/synthesis-jobs",
        headers=headers,
        json={"voice_id": result["voice"]["id"], "text": "Hello.", "preset": "master", "seed": 1},
    )
    job = wait_job(client, headers, response.json()["job"]["id"])
    artifact = job["artifacts"][0]
    store = Store(client.app.state.store.db)
    target = settings.data_dir / "outside-secret.txt"
    target.write_text("SECRET")
    link = settings.data_dir / "artifact-link.wav"
    link.symlink_to(target)
    with store.db.transaction(immediate=True) as conn:
        conn.execute("UPDATE artifacts SET path=? WHERE id=?", ("artifact-link.wav", artifact["id"]))
    response = client.get(f"/api/v1/artifacts/{artifact['id']}/download", headers=headers)
    assert response.status_code == 404
    assert b"SECRET" not in response.content
    # Restore and tamper with a traversal path.
    with store.db.transaction(immediate=True) as conn:
        conn.execute("UPDATE artifacts SET path=? WHERE id=?", ("../../etc/passwd", artifact["id"]))
    response = client.get(f"/api/v1/artifacts/{artifact['id']}/download", headers=headers)
    assert response.status_code == 404
    assert b"root:" not in response.content


def test_no_text_truncation_in_plan(settings):
    text = ("Sentence with names, numbers 12345, and a question? " * 20).strip()
    plan = build_plan({"text": text}, settings=settings, seed=9)
    chunks = [c.text for c in plan.text_pieces[0].chunks]
    assert "".join(chunks) == text
    assert plan.total_chars == len(text)
    assert plan.seed == 9


def test_headerless_recording_duration_falls_back_to_bounded_decode(tmp_path, settings, monkeypatch):
    import json
    import subprocess

    from clawvoice.audio import AudioProcessor

    source = write_wav(tmp_path / "headerless-ish.wav", seconds=6.4)
    audio = AudioProcessor(settings)
    original = AudioProcessor._run

    def fake_run(self, args, *, timeout):
        if args and str(args[0]).endswith("ffprobe"):
            payload = {
                "streams": [{"codec_name": "opus", "sample_rate": "48000", "channels": 1}],
                "format": {"format_name": "matroska,webm"},
            }
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        return original(self, args, timeout=timeout)

    monkeypatch.setattr(AudioProcessor, "_run", fake_run)
    probe = audio.probe(source)
    assert probe.duration_source == "decoded"
    assert 6_300 <= probe.duration_ms <= 6_500
