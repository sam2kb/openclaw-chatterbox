from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from clawvoice.api import create_app
from clawvoice.backends.fake import FakeBackend
from clawvoice.worker import InferenceWorker
from tests.helpers import create_direct_voice, wait_job, wait_ready


class SlowBackend(FakeBackend):
    def __init__(self, settings):
        super().__init__(settings)
        self.entered = threading.Event()
        self.release = threading.Event()

    def render(self, *, reference_path, text, controls, seed):
        self.entered.set()
        self.release.wait(timeout=8)
        return super().render(
            reference_path=reference_path, text=text, controls=controls, seed=seed
        )


def test_queue_depth_and_queued_cancel(settings, headers, reference):
    settings.max_queue_depth = 1
    app = create_app(settings, start_worker=False)
    store = app.state.store
    with TestClient(app) as client:
        first = create_direct_voice(client, headers, reference)
        second = client.post(
            "/api/v1/voices",
            params={"name": "second", "authorized": "true"},
            headers={**headers, "Content-Type": "application/octet-stream"},
            content=reference.read_bytes(),
        )
        assert second.status_code == 429, second.text

        canceled = store.request_cancel(first["audition_job"]["id"])
        assert canceled["status"] == "canceled"
        preview = client.post(f"/api/v1/voices/{first['voice']['id']}/preview", headers=headers)
        assert preview.status_code == 202, preview.text


def test_running_cancel_is_pending_then_canceled(settings, headers, reference):
    app = create_app(settings, start_worker=False)
    store = app.state.store
    with TestClient(app) as client:
        voice = create_direct_voice(client, headers, reference)["voice"]
        # drain the audition from the queue first (no worker, so cancel it)
        # Find it by reading the job table via the store is internal to this test.
        with store.db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM jobs WHERE voice_id=? AND kind='audition'", (voice["id"],)
            ).fetchone()
        store.request_cancel(row["id"])
        backend = SlowBackend(settings)
        worker = InferenceWorker(settings, store, app.state.audio, backend=backend)
        worker.start_thread()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = store.worker_lock_view()
                if state.get("status") == "ready":
                    break
                time.sleep(0.03)
            response = client.post(
                "/api/v1/synthesis-jobs",
                headers=headers,
                json={"voice_id": voice["id"], "text": "Wait for it.", "preset": "master"},
            )
            assert response.status_code == 202, response.text
            job_id = response.json()["job"]["id"]
            assert backend.entered.wait(timeout=5)
            canceled = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=headers)
            assert canceled.status_code == 202
            assert canceled.json()["cancel_pending"] is True
            backend.release.set()
            final = wait_job(client, headers, job_id, timeout=10)
            assert final["status"] == "canceled"
            assert final["artifacts"] == []
            job_dir = settings.artifacts_dir / job_id
            assert not job_dir.exists() or not any(job_dir.rglob("*"))
        finally:
            backend.release.set()
            worker.stop(timeout=10)


def test_stale_job_recovery_preserves_id_and_bounds_attempts(settings, headers, reference):
    app = create_app(settings, start_worker=False)
    store = app.state.store
    with TestClient(app) as client:
        result = create_direct_voice(client, headers, reference)
        audition_id = result["audition_job"]["id"]
        with store.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE jobs SET status='running', worker_id='wrk_dead', attempts=1, "
                "lease_expires_at=? WHERE id=?",
                (1, audition_id),
            )
        recovered = store.recover_stale_jobs(lease_seconds=30)
        assert audition_id in recovered
        row = store.get_job_row(audition_id)
        assert row["id"] == audition_id
        assert row["status"] == "queued"
        with store.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE jobs SET status='running', worker_id='wrk_dead', attempts=2, "
                "lease_expires_at=? WHERE id=?",
                (1, audition_id),
            )
        store.recover_stale_jobs(lease_seconds=30)
        failed = store.get_job_row(audition_id)
        assert failed["status"] == "failed"
        assert failed["error_code"] == "worker_interrupted"


def test_failed_job_does_not_poison_next_profile(settings, headers, reference):
    from tests.helpers import write_wav

    other = write_wav(settings.data_dir.parent / "other.wav", frequency=440)
    app = create_app(settings, start_worker=True)
    with TestClient(app) as client:
        wait_ready(client, headers)
        a_result = create_direct_voice(client, headers, reference, name="A")
        b_result = create_direct_voice(client, headers, other, name="B")
        a = a_result["voice"]
        b = b_result["voice"]
        wait_job(client, headers, a_result["audition_job"]["id"])
        wait_job(client, headers, b_result["audition_job"]["id"])

        def render(voice, text):
            response = client.post(
                "/api/v1/synthesis-jobs",
                headers=headers,
                json={"voice_id": voice["id"], "text": text, "preset": "master", "seed": 5},
            )
            assert response.status_code == 202, response.text
            job = wait_job(client, headers, response.json()["job"]["id"])
            return job

        a1 = render(a, "Same text, same seed.")
        b1 = render(b, "Same text, same seed.")
        failed = render(a, "This will fail [[fail]] deliberately.")
        a2 = render(a, "Same text, same seed.")
        assert a1["status"] == b1["status"] == a2["status"] == "succeeded"
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "inference_failed"
        assert a1["artifacts"][0]["sha256"] == a2["artifacts"][0]["sha256"]
        assert a1["artifacts"][0]["sha256"] != b1["artifacts"][0]["sha256"]
        assert app.state.worker.backend.max_active == 1
        oom = render(a, "This requests OOM [[oom]].")
        assert oom["status"] == "failed"
        assert oom["error"]["code"] == "cuda_oom"
        assert "CPU fallback" in oom["error"]["message"]
        after = render(b, "Still B after OOM.")
        assert after["status"] == "succeeded"


def test_profile_conditioning_alternates_under_overlapping_requests(settings, headers, reference):
    from tests.helpers import write_wav

    other = write_wav(settings.data_dir.parent / "other2.wav", frequency=600)
    app = create_app(settings, start_worker=True)
    with TestClient(app) as client:
        wait_ready(client, headers)
        a_result = create_direct_voice(client, headers, reference, name="A")
        b_result = create_direct_voice(client, headers, other, name="B")
        a = a_result["voice"]
        b = b_result["voice"]
        wait_job(client, headers, a_result["audition_job"]["id"])
        wait_job(client, headers, b_result["audition_job"]["id"])
        job_ids = []
        for index in range(6):
            voice = a if index % 2 == 0 else b
            response = client.post(
                "/api/v1/synthesis-jobs",
                headers=headers,
                json={
                    "voice_id": voice["id"],
                    "text": f"Turn {index}.",
                    "preset": "master",
                    "seed": 100 + index,
                },
            )
            assert response.status_code == 202, response.text
            job_ids.append(response.json()["job"]["id"])
        jobs = [wait_job(client, headers, job_id, timeout=20) for job_id in job_ids]
        assert all(job["status"] == "succeeded" for job in jobs)
        assert app.state.worker.backend.max_active == 1
        # Each job records exactly one rendered profile and no cross-profile leak.
        assert len({job["artifacts"][0]["sha256"] for job in jobs}) == 6


def test_claim_requires_current_worker_lease(settings, headers, reference):
    app = create_app(settings, start_worker=False)
    store = app.state.store
    with TestClient(app) as client:
        create_direct_voice(client, headers, reference)
        assert store.try_acquire_worker_lock(owner="wrk_a", lease_seconds=30) is True
        with store.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE worker_lock SET owner='wrk_b', lease_expires_at=? WHERE id=1",
                (time.time() * 1000 + 30_000,),
            )
        assert store.claim_job(worker_id="wrk_a", lease_seconds=30) is None


def test_recovered_job_clears_stale_error_on_success(settings, headers, reference):
    from tests.helpers import wait_job, wait_ready

    app = create_app(settings, start_worker=False)
    store = app.state.store
    with TestClient(app) as client:
        result = create_direct_voice(client, headers, reference)
        job_id = result["audition_job"]["id"]
        with store.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE jobs SET status='running', worker_id='wrk_dead', attempts=1, "
                "lease_expires_at=? WHERE id=?",
                (1, job_id),
            )
        store.recover_stale_jobs(lease_seconds=30)
        worker = InferenceWorker(settings, store, app.state.audio)
        worker.start_thread()
        try:
            wait_ready(client, headers, timeout=15)
            job = wait_job(client, headers, job_id, timeout=20)
            assert job["status"] == "succeeded"
            assert job["error"] is None
        finally:
            worker.stop(timeout=10)


def test_ready_worker_merges_identity_with_device_diagnostics(settings, headers):
    from clawvoice.backends.base import BackendState

    class DetailedBackend(FakeBackend):
        def identity(self):
            return {**super().identity(), "real_model": True, "backend": "turbo"}

        def state(self):
            return BackendState("ready", {"device": "cuda", "capability": "6.1"}, True)

    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        worker = InferenceWorker(
            settings, app.state.store, app.state.audio, backend=DetailedBackend(settings)
        )
        worker.start_thread()
        try:
            ready = wait_ready(client, headers).json()
            assert ready["checks"]["worker"]["real_model"] is True
            assert ready["checks"]["worker"]["device"] == "cuda"
        finally:
            worker.stop()
