from clawvoice.doctor import ready_enough
from clawvoice.speech import split_text
from tests.helpers import create_direct_voice, wait_job


def test_client_does_not_require_local_torch_or_model_for_ready_worker():
    report = {
        "model": {"ok": False},
        "torch": {"installed": False},
        "service": {
            "reachable": True,
            "ready": {"status": "ready", "checks": {"worker": {"real_model": True}}},
        },
    }
    assert ready_enough(report)
    report["service"]["ready"]["checks"]["worker"]["real_model"] = False
    assert not ready_enough(report)


def test_sentence_chunks_preserve_words_spacing_and_closing_sentence():
    text = "  Call Dr. Smith. Please leave a text message. Thank you.\n"
    chunks = split_text(text, 280, max_chunks=10)
    assert "".join(chunks) == text
    assert len(chunks) == 3
    assert chunks[-1] == "Thank you.\n"
    assert "Dr. Smith." in chunks[0]


def test_cleanup_reports_and_removes_expired_unpinned_audio(client, headers, reference, settings):
    result = create_direct_voice(client, headers, reference)
    job = wait_job(client, headers, result["audition_job"]["id"])
    artifact = job["artifacts"][0]
    store = client.app.state.store
    row = store.get_artifact_row(artifact["id"])
    path = settings.data_dir / row["path"]
    with store.db.transaction(immediate=True) as c:
        c.execute("UPDATE artifacts SET expires_at=1 WHERE id=?", (artifact["id"],))
    # Invoke the same maintenance implementation the lifespan loop schedules.
    # Startup/periodic execution itself is verified by the live acceptance check.
    from clawvoice.storage import StorageManager

    storage = StorageManager(settings, store)
    assert storage.cleanup_expired_artifacts() == 1
    assert not path.exists()
    assert client.get(f"/api/v1/artifacts/{artifact['id']}", headers=headers).status_code == 404
    health = client.get("/api/v1/health/ready", headers=headers).json()
    assert health["checks"]["cleanup"]["interval_seconds"] == settings.cleanup_interval_seconds
    assert health["checks"]["cleanup"]["last_run_at"]
