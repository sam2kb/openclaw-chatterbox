from __future__ import annotations

from tests.helpers import create_direct_voice, wait_job, wait_ready


def test_file_to_voice_to_new_speech(client, headers, reference, tmp_path):
    wait_ready(client, headers)
    result = create_direct_voice(client, headers, reference, name="Phone greeting")
    voice = result["voice"]
    assert voice["name"] == "Phone greeting"
    assert voice["reference"]["sha256"]
    assert voice["metadata_version"] == 1
    audition = wait_job(client, headers, result["audition_job"]["id"])
    assert audition["status"] == "succeeded", audition
    assert audition["artifacts"][0]["kind"] == "master"

    listed = client.get("/api/v1/voices", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["voices"][0]["id"] == voice["id"]

    selected = client.post(f"/api/v1/voices/{voice['id']}/select", headers=headers)
    assert selected.status_code == 200
    assert selected.json()["is_default"] is True

    rendered = client.post(
        "/api/v1/synthesis-jobs",
        headers=headers,
        json={
            "text": "Thank you for calling. Our office is open Monday through Friday.",
            "preset": "master",
            "seed": 11,
        },
    )
    assert rendered.status_code == 202, rendered.text
    assert rendered.headers["location"].startswith("/api/v1/jobs/")
    job = wait_job(client, headers, rendered.json()["job"]["id"])
    assert job["status"] == "succeeded", job
    assert job["voice_id"] == voice["id"]
    master = next(a for a in job["artifacts"] if a["kind"] == "master")
    manifest = master["manifest"]
    assert manifest["request"]["seed"] == 11
    assert manifest["request"]["text_sha256"]
    assert "text" not in manifest["request"]
    assert manifest["watermark"]["expected"] == "not_applicable_fake"
    assert master["sample_rate"] == 24000
    assert master["sha256"]


def test_voice_delete_removes_private_data(client, headers, reference, settings):
    result = create_direct_voice(client, headers, reference)
    voice_id = result["voice"]["id"]
    wait_job(client, headers, result["audition_job"]["id"])
    voice_dir = settings.voices_dir / voice_id
    assert voice_dir.is_dir()
    response = client.delete(f"/api/v1/voices/{voice_id}", headers=headers)
    assert response.status_code == 200, response.text
    assert not voice_dir.exists()
    assert client.get(f"/api/v1/voices/{voice_id}", headers=headers).status_code == 404
    assert client.get("/api/v1/voices", headers=headers).json()["voices"] == []


def test_chunked_long_text_preserved(client, headers, reference):
    result = create_direct_voice(client, headers, reference)
    text = ("Numbers 123 and names like Ada. Is this a question? " * 10).strip()
    response = client.post(
        "/api/v1/synthesis-jobs",
        headers=headers,
        json={"voice_id": result["voice"]["id"], "text": text, "preset": "master", "seed": 4},
    )
    assert response.status_code == 202, response.text
    assert response.json()["request"]["text_sha256"]
    job = wait_job(client, headers, response.json()["job"]["id"], timeout=20)
    assert job["status"] == "succeeded", job
    manifest = job["artifacts"][0]["manifest"]
    assert manifest["request"]["total_chars"] == len(text)
    assert manifest["request"]["text_sha256"] == response.json()["request"]["text_sha256"]
    assert manifest["request"]["segments"][0]["type"] == "text"


def test_preset_master_is_native_rate_and_download_headers(client, headers, reference):
    result = create_direct_voice(client, headers, reference)
    response = client.post(
        "/api/v1/synthesis-jobs",
        headers=headers,
        json={"voice_id": result["voice"]["id"], "text": "A short line.", "preset": "master"},
    )
    job = wait_job(client, headers, response.json()["job"]["id"])
    artifact = job["artifacts"][0]
    download = client.get(f"/api/v1/artifacts/{artifact['id']}/download", headers=headers)
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("audio/wav")
    assert download.headers["content-disposition"].startswith("attachment; filename=")
    assert download.headers["x-artifact-sha256"] == artifact["sha256"]
    assert len(download.content) == artifact["bytes"]


def test_restart_preserves_saved_voice(settings, headers, reference):
    from fastapi.testclient import TestClient

    from clawvoice.api import create_app

    first = create_app(settings, start_worker=False)
    with TestClient(first) as client:
        result = create_direct_voice(client, headers, reference)
        voice_id = result["voice"]["id"]
        # Cancel the queued audition so the client can close cleanly and no job is lost.
        store = first.state.store
        store.request_cancel(result["audition_job"]["id"])

    second = create_app(settings, start_worker=False)
    with TestClient(second) as client:
        response = client.get(f"/api/v1/voices/{voice_id}", headers=headers)
        assert response.status_code == 200
        assert response.json()["id"] == voice_id
        assert response.json()["reference"]["sha256"]


def test_expired_artifact_cleanup_removes_private_file(client, headers, reference, settings):
    from clawvoice.storage import StorageManager
    from clawvoice.util import now_ms

    result = create_direct_voice(client, headers, reference)
    response = client.post(
        "/api/v1/synthesis-jobs",
        headers=headers,
        json={"voice_id": result["voice"]["id"], "text": "Expire me.", "preset": "master"},
    )
    job = wait_job(client, headers, response.json()["job"]["id"])
    artifact = job["artifacts"][0]
    row = client.app.state.store.get_artifact_row(artifact["id"])
    path = client.app.state.storage.resolve(row["path"])
    assert path.is_file()
    with client.app.state.store.db.transaction(immediate=True) as conn:
        conn.execute("UPDATE artifacts SET expires_at=? WHERE id=?", (now_ms() - 1, artifact["id"]))
    removed = StorageManager(settings, client.app.state.store).cleanup_expired_artifacts()
    assert removed == 1
    assert not path.exists()
    assert client.get(f"/api/v1/artifacts/{artifact['id']}", headers=headers).status_code == 404


def test_revise_voice_keeps_previous_reference_version(client, headers, reference, tmp_path, settings):
    from tests.helpers import write_wav

    first = create_direct_voice(client, headers, reference)
    voice_id = first["voice"]["id"]
    old_ref = first["voice"]["reference"]["id"]
    old_sha = first["voice"]["reference"]["sha256"]
    new_sample = write_wav(tmp_path / "revision.wav", seconds=6.6, frequency=440)
    revised = client.post(
        f"/api/v1/voices/{voice_id}/reference",
        params={"authorized": "true"},
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=new_sample.read_bytes(),
    )
    assert revised.status_code == 202, revised.text
    voice = revised.json()["voice"]
    assert voice["reference"]["id"] != old_ref
    assert voice["reference"]["sha256"] != old_sha
    assert revised.json()["audition_job"]["id"]
    # The historical reference is retained as a version row/file for a future
    # permissioned training extension; deleting the profile removes all versions.
    with client.app.state.store.db.connect() as conn:
        rows = conn.execute(
            "SELECT id, normalized_path FROM reference_versions WHERE voice_id=?", (voice_id,)
        ).fetchall()
    assert len(rows) == 2
    for row in rows:
        assert client.app.state.storage.resolve(row["normalized_path"]).is_file()
