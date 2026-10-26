from __future__ import annotations

import json
from dataclasses import replace

from clawvoice import smoke
from clawvoice.backends.base import BackendState, RenderResult
from clawvoice.util import sha256_file


def test_smoke_keeps_reference_digest_after_temporary_audio_is_removed(
    settings, reference, tmp_path, monkeypatch
):
    seen = []

    class StubBackend:
        sample_rate = 24000
        last_metrics = {}

        def __init__(self, settings):
            pass

        def load(self):
            pass

        def render(self, *, reference_path, **kwargs):
            seen.append((reference_path, sha256_file(reference_path)))
            return RenderResult(b"\x01\x00" * 2400, self.sample_rate)

        def state(self):
            return BackendState("ready", {"device": "cpu"}, loaded=True)

        def shutdown(self):
            pass

    monkeypatch.setattr(smoke, "verify_model", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(smoke, "TurboBackend", StubBackend)
    config = replace(settings, backend="turbo", allow_fake_backend=False)
    config.ensure_directories()
    output = tmp_path / "smoke.wav"
    record = smoke.run_smoke(config, reference=reference, output=output, text="Hello.")
    assert record["reference"]["sha256"] == seen[0][1]
    assert not seen[0][0].exists()
    assert output.is_file()
    assert [x["pass"] for x in record["latency"]["renders"]] == ["cold", "warm"]
    saved = json.loads((config.state_dir / "compatibility.json").read_text())
    assert saved["reference"]["sha256"] == record["reference"]["sha256"]


def test_smoke_cli_routes_upstream_prints_to_stderr(reference, tmp_path, monkeypatch, capsys):
    from clawvoice.cli import main

    def noisy_smoke(*args, **kwargs):
        print("upstream model loaded")
        return {
            "output": {"path": "test.wav", "duration_ms": 1000},
            "latency": {"renders": [{"latency_seconds": 1}, {"latency_seconds": 1}]},
        }

    monkeypatch.setattr(smoke, "run_smoke", noisy_smoke)
    monkeypatch.setenv("CLAWVOICE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLAWVOICE_BACKEND", "turbo")
    assert (
        main(
            [
                "models",
                "smoke",
                "--reference",
                str(reference),
                "--text",
                "Hello.",
                "--output",
                str(tmp_path / "test.wav"),
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["output"]["duration_ms"] == 1000
    assert "upstream model loaded" in output.err
