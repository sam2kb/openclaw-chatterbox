from tests.helpers import create_direct_voice


def test_browser_reenrollment_preserves_profile_and_limits_scope(client, headers, reference):
    original = create_direct_voice(client, headers, reference, name="Original name")
    voice_id = original["voice"]["id"]
    session = client.post(
        "/api/v1/enrollment-sessions", headers=headers, json={"voice_id": voice_id}
    )
    assert session.status_code == 201
    data = session.json()
    assert data["mode"] == "revise" and data["target_voice_id"] == voice_id
    scoped = {"Authorization": "Bearer " + data["recorder_url"].split("#token=", 1)[1]}
    assert client.get("/api/v1/voices", headers=scoped).status_code == 401
    upload = client.post(
        f"/api/v1/enrollment-sessions/{data['id']}/uploads",
        headers={**scoped, "Content-Type": "application/octet-stream"},
        content=reference.read_bytes(),
    )
    assert upload.status_code == 201
    body = {"upload_id": upload.json()["id"], "name": "Ignored browser rename", "authorized": True}
    path = f"/api/v1/enrollment-sessions/{data['id']}/voices"
    saved = client.post(path, headers=scoped, json=body)
    assert saved.status_code == 201, saved.text
    revised = saved.json()["voice"]
    assert revised["id"] == voice_id and revised["name"] == "Original name"
    assert revised["reference"]["id"] != original["voice"]["reference"]["id"]
    assert len(client.get("/api/v1/voices", headers=headers).json()["voices"]) == 1
    assert client.post(path, headers=scoped, json=body).status_code == 409
    status = client.get(f"/api/v1/enrollment-sessions/{data['id']}", headers=scoped).json()
    assert (
        status["voice_id"] == voice_id
        and status["audition"]["id"] == saved.json()["audition_job"]["id"]
    )


def test_enrollment_target_must_be_explicit_and_exist(client, headers):
    for body in ({}, {"name": "new", "voice_id": "voc_" + "a" * 24}):
        assert (
            client.post("/api/v1/enrollment-sessions", headers=headers, json=body).status_code
            == 422
        )
    assert (
        client.post(
            "/api/v1/enrollment-sessions", headers=headers, json={"voice_id": "voc_" + "a" * 24}
        ).status_code
        == 404
    )


def test_schema_two_migrates_without_losing_data(tmp_path):
    import sqlite3

    from clawvoice.db import SCHEMA, Database

    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA.replace("    target_voice_id TEXT,\n", ""))
        c.execute("INSERT INTO settings VALUES ('sentinel','retained',1)")
        c.execute("PRAGMA user_version=2")
    Database(path).initialize()
    with sqlite3.connect(path) as c:
        assert "target_voice_id" in [
            r[1] for r in c.execute("PRAGMA table_info(enrollment_sessions)")
        ]
        assert (
            c.execute("SELECT value FROM settings WHERE key='sentinel'").fetchone()[0] == "retained"
        )
