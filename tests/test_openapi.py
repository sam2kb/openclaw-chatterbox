from __future__ import annotations

import json
import subprocess
import sys


def test_openapi_schema_contains_versioned_routes_and_bearer_security(tmp_path):
    output = tmp_path / "openapi.json"
    proc = subprocess.run(
        [sys.executable, "-m", "clawvoice", "openapi", "--output", str(output)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    schema = json.loads(output.read_text())
    assert "/api/v1/synthesis-jobs" in schema["paths"]
    assert "/api/v1/enrollment-sessions" in schema["paths"]
    assert "/api/v1/artifacts/{artifact_id}/download" in schema["paths"]
    assert "/api/v1/voices/{voice_id}/reference" in schema["paths"]
    assert schema["components"]["securitySchemes"]
    # No arbitrary filesystem path or remote URL upload parameter is exposed.
    text = output.read_text()
    assert '"format": "binary"' not in text
