from __future__ import annotations

from fastapi.testclient import TestClient

from clawvoice.api import create_app
from tests.helpers import create_enrollment, wait_job


def test_liveness_is_public_but_other_routes_require_auth(client, headers):
    live = client.get("/api/v1/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "live"
    for path in (
        "/api/v1/health/ready",
        "/api/v1/capabilities",
        "/api/v1/voices",
        "/api/v1/artifacts/art_000000000000000000000000",
        "/api/v1/enrollment-sessions",
    ):
        response = client.get(path) if path != "/api/v1/enrollment-sessions" else client.post(path, json={"name": "x"})
        assert response.status_code == 401, (path, response.text)
    wrong = client.get("/api/v1/voices", headers={"Authorization": "Bearer " + "x" * 40})
    assert wrong.status_code == 401


def test_enrollment_token_is_scoped_and_single_use(client, headers, reference):
    session = create_enrollment(client, headers, "session voice")
    assert "#token=" in session["recorder_url"]
    assert session["recorder_url"].startswith("http://127.0.0.1:8765/enroll/")
    token = session["recorder_url"].split("#token=", 1)[1]
    session_id = session["id"]
    scoped = {"Authorization": f"Bearer {token}"}

    # The enrollment credential cannot act as a service credential.
    assert client.get("/api/v1/voices", headers=scoped).status_code == 401
    assert client.get("/api/v1/capabilities", headers=scoped).status_code == 401

    upload = client.post(
        f"/api/v1/enrollment-sessions/{session_id}/uploads",
        headers=scoped,
        content=reference.read_bytes(),
    )
    assert upload.status_code == 201, upload.text
    upload_id = upload.json()["id"]
    assert upload.json()["checks"]["speaker_identity_verified"] is False
    assert upload.json()["checks"]["suggested_segment"]["end_ms"] > 5_001

    create = client.post(
        f"/api/v1/enrollment-sessions/{session_id}/voices",
        headers=scoped,
        json={
            "upload_id": upload_id,
            "name": "session voice",
            "authorized": True,
            "start_ms": 0,
            "end_ms": 6500,
        },
    )
    assert create.status_code == 201, create.text
    voice = create.json()["voice"]
    assert voice["reference"]["duration_ms"] >= 5_001
    assert voice["metadata_version"] == 1

    # The create action is single-use even while the token may still read its audition.
    again = client.post(
        f"/api/v1/enrollment-sessions/{session_id}/voices",
        headers=scoped,
        json={
            "upload_id": upload_id,
            "name": "second",
            "authorized": True,
        },
    )
    assert again.status_code == 409
    job = wait_job(client, headers, create.json()["audition_job"]["id"])
    assert job["status"] == "succeeded"
    audition = client.get(f"/api/v1/enrollment-sessions/{session_id}/audition", headers=scoped)
    assert audition.status_code == 200
    artifact = audition.json()["artifacts"][0]
    download = client.get(
        f"/api/v1/enrollment-sessions/{session_id}/artifacts/{artifact['id']}/download",
        headers=scoped,
    )
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("audio/")


def test_enrollment_requires_consent_and_valid_upload_link(client, headers, reference):
    session = create_enrollment(client, headers)
    token = session["recorder_url"].split("#token=", 1)[1]
    scoped = {"Authorization": f"Bearer {token}"}
    upload = client.post(
        f"/api/v1/enrollment-sessions/{session['id']}/uploads",
        headers=scoped,
        content=reference.read_bytes(),
    )
    assert upload.status_code == 201
    denied = client.post(
        f"/api/v1/enrollment-sessions/{session['id']}/voices",
        headers=scoped,
        json={
            "upload_id": upload.json()["id"],
            "name": "denied",
            "authorized": False,
        },
    )
    assert denied.status_code == 422
    assert "permission" in denied.json()["error"]["message"]

    # Wrong session credential and malformed IDs must not leak details.
    other = create_enrollment(client, headers)
    other_token = other["recorder_url"].split("#token=", 1)[1]
    wrong = client.get(
        f"/api/v1/enrollment-sessions/{session['id']}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert wrong.status_code == 401
    malformed = client.get("/api/v1/enrollment-sessions/enr_000000000000000000000000", headers=scoped)
    assert malformed.status_code == 404


def test_enrollment_expiry_is_reported(settings, reference):
    settings.enrollment_ttl_seconds = -1
    # Construct after mutating so the session is already expired.
    application = create_app(settings, start_worker=False)
    with TestClient(application) as client:
        headers = {"Authorization": f"Bearer {settings.service_token}"}
        response = client.post("/api/v1/enrollment-sessions", headers=headers, json={"name": "expired"})
        # The ttl is applied at session creation from the setting.
        assert response.status_code == 201
        token = response.json()["recorder_url"].split("#token=", 1)[1]
        scoped = {"Authorization": f"Bearer {token}"}
        state = client.get(f"/api/v1/enrollment-sessions/{response.json()['id']}", headers=scoped)
        assert state.status_code == 401
        upload = client.post(
            f"/api/v1/enrollment-sessions/{response.json()['id']}/uploads",
            headers=scoped,
            content=reference.read_bytes(),
        )
        assert upload.status_code == 401


def test_artifact_requires_service_auth(client, headers, reference):
    from tests.helpers import create_direct_voice

    result = create_direct_voice(client, headers, reference)
    preview = client.post(f"/api/v1/voices/{result['voice']['id']}/preview", headers=headers)
    assert preview.status_code == 202
    job = wait_job(client, headers, preview.json()["job"]["id"])
    artifact = job["artifacts"][0]
    assert client.get(f"/api/v1/artifacts/{artifact['id']}").status_code == 401
    assert client.get(f"/api/v1/artifacts/{artifact['id']}/download").status_code == 401


def test_wildcard_cors_is_not_configured(client, headers):
    response = client.options(
        "/api/v1/voices",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert response.headers.get("access-control-allow-origin") is None
