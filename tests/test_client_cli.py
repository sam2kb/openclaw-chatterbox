from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from clawvoice.client import ClientConfigurationError, load_service_token
from tests.helpers import write_wav

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_cli(args, env, timeout=30):
    return subprocess.run(
        [sys.executable, "-m", "clawvoice", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_client_package_has_no_ml_import():
    assert importlib.util.find_spec("torch") is None
    code = (
        "import sys; import clawvoice.client, clawvoice.cli; "
        "assert 'torch' not in sys.modules and 'chatterbox' not in sys.modules"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_token_file_permissions_are_enforced(tmp_path):
    token = tmp_path / "token"
    token.write_text("x" * 40)
    token.chmod(0o644)
    with pytest.raises(ClientConfigurationError):
        load_service_token(token)


def test_cli_end_to_end_without_torch(tmp_path):
    port = free_port()
    data_dir = tmp_path / "service"
    token_file = data_dir / "secrets" / "service-token"
    env = os.environ.copy()
    env.update(
        {
            "CLAWVOICE_URL": f"http://127.0.0.1:{port}",
            "CLAWVOICE_PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
            "CLAWVOICE_ALLOWED_ORIGINS": f"http://127.0.0.1:{port}",
            "CLAWVOICE_DATA_DIR": str(data_dir),
            "CLAWVOICE_TOKEN_FILE": str(token_file),
            "CLAWVOICE_BACKEND": "fake",
            "CLAWVOICE_ALLOW_FAKE": "1",
            "CLAWVOICE_DEVICE": "cpu",
            "CLAWVOICE_LOG_LEVEL": "warning",
            "TMPDIR": str(tmp_path / "tmp"),
        }
    )
    (tmp_path / "tmp").mkdir()
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "clawvoice",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            check = run_cli(["healthcheck"], env, timeout=3)
            if check.returncode == 0:
                break
            time.sleep(0.15)
        else:
            raise AssertionError("server did not become live")

        doctor = run_cli(["doctor", "--json"], env)
        assert doctor.returncode == 0, doctor.stderr
        report = json.loads(doctor.stdout)
        assert report["service"]["live"]["status"] == "live"
        assert report["torch"]["installed"] is False

        enrollment = run_cli(["enroll", "start", "--name", "cli voice", "--json"], env)
        assert enrollment.returncode == 0, enrollment.stderr
        payload = json.loads(enrollment.stdout)
        assert "#token=" in payload["recorder_url"]

        sample = write_wav(tmp_path / "sample.wav", seconds=6.7)
        created = run_cli(
            [
                "voices",
                "create",
                "--name",
                "CLI Voice",
                "--audio",
                str(sample),
                "--authorized",
                "--json",
            ],
            env,
        )
        assert created.returncode == 0, created.stderr
        voice = json.loads(created.stdout)["voice"]
        assert voice["name"] == "CLI Voice"

        text_file = tmp_path / "greeting.txt"
        text_file.write_text("Thank you for calling. Press 2 for billing.\n", encoding="utf-8")
        speak = run_cli(
            [
                "speak",
                "--voice",
                voice["id"],
                "--text-file",
                str(text_file),
                "--preset",
                "master",
                "--wait",
                "--timeout",
                "20",
                "--json",
            ],
            env,
        )
        assert speak.returncode == 0, speak.stderr
        job = json.loads(speak.stdout)["job"]
        assert job["status"] == "succeeded", job
        artifact_id = job["output_artifact_id"]
        output = tmp_path / "out.wav"
        fetched = run_cli(
            ["artifacts", "fetch", artifact_id, "--output", str(output), "--json"], env
        )
        assert fetched.returncode == 0, fetched.stderr
        result = json.loads(fetched.stdout)
        assert result["bytes"] == output.stat().st_size > 1000
        assert output.read_bytes()[:4] == b"RIFF"
        assert result["sha256"]
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
