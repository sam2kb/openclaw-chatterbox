from __future__ import annotations

import math
import struct
import time
import wave
from pathlib import Path

from fastapi.testclient import TestClient


def write_wav(
    path: Path, seconds: float = 6.5, frequency: float = 220.0, sample_rate: int = 24000
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(sample_rate * seconds)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        data = bytearray()
        for i in range(frames):
            value = 0.18 * math.sin(2 * math.pi * frequency * i / sample_rate)
            data.extend(struct.pack("<h", int(value * 32767)))
        wav.writeframes(bytes(data))
    return path


def wait_ready(client: TestClient, headers: dict[str, str], timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = client.get("/api/v1/health/ready", headers=headers)
        if last.status_code == 200:
            return last
        time.sleep(0.05)
    raise AssertionError(f"service did not become ready: {last.status_code} {last.text}")


def wait_job(
    client: TestClient, headers: dict[str, str], job_id: str, timeout: float = 10.0
):
    deadline = time.monotonic() + timeout
    response = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
        assert response.status_code == 200, response.text
        if response.json()["status"] in ("succeeded", "failed", "canceled"):
            return response.json()
        time.sleep(0.03)
    raise AssertionError(f"job {job_id} did not finish: {response.text if response else 'no response'}")


def create_direct_voice(
    client: TestClient,
    headers: dict[str, str],
    reference: Path,
    *,
    name: str = "test voice",
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> dict:
    response = client.post(
        "/api/v1/voices",
        params={
            "name": name,
            "authorized": "true",
            **({"start_ms": start_ms} if start_ms is not None else {}),
            **({"end_ms": end_ms} if end_ms is not None else {}),
        },
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=reference.read_bytes(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_enrollment(
    client: TestClient, headers: dict[str, str], name: str = "enrolled voice"
) -> dict:
    response = client.post("/api/v1/enrollment-sessions", headers=headers, json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()
