# AGENTS.md — openclaw-chatterbox

A standalone local voice service: reference-conditioned Chatterbox synthesis, a
small recorder/preview UI, a lightweight `clawvoice` client, and an OpenClaw
skill. Read `README.md`, `docs/architecture.md`, `docs/compatibility.md`, and
`docs/training.md` before changing the model boundary.

## On resume

```sh
git status --short --branch
git diff
git log --oneline -10
sed -n '1,160p' .codex/HANDOFF.md   # local only, intentionally untracked
```

Do not rewrite unrelated work. Inspect the configured origin before publishing.
Local changes are not backed up to GitHub until they are pushed.

## Setup

Python 3.11 is the supported development/runtime version. Keep caches inside
the checkout because this host's `$HOME` is not a writable tool cache:

```sh
export UV_CACHE_DIR="$PWD/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$PWD/.cache/python"
export UV_PYTHON=3.11
export TMPDIR="$PWD/.tmp"; mkdir -p "$TMPDIR"
uv venv .venv --python 3.11
uv sync --python .venv/bin/python --extra dev
```

The base install deliberately has no Torch or Chatterbox dependency. The
`inference` extra is only for a real worker. Docker images use `uv.lock`
(`--frozen`); do not hand-edit a resolved lock.

## Verification commands

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests deploy/clawbox.py
.venv/bin/python -m compileall -q src deploy/clawbox.py
.venv/bin/python -m clawvoice --help
.venv/bin/python -m clawvoice doctor --json
.venv/bin/python -m clawvoice openapi --output docs/api/openapi.json
deploy/clawbox plan --json
.venv/bin/python -m build --wheel
```

Use `CLAWVOICE_BACKEND=fake` only in tests/CI and only with
`CLAWVOICE_ALLOW_FAKE=1`; a fake render is never evidence of voice quality or
CUDA support. Real-model smoke:

```sh
clawvoice models fetch --backend turbo --revision 749d1c1a46eb10492095d68fbcf55691ccf137cd
clawvoice models verify --backend turbo --deep --json
clawvoice models smoke --reference sample.wav --text "..." --output smoke.wav --json
```

Run the smoke test and hardware checks on the authorized Pascal host. Record
measured results in `docs/compatibility.md`; mocked tests do not establish GPU
support or voice quality. Build and verify both API and worker images.

## Safety boundaries

- Never log service tokens, enrollment tokens, raw recordings, or synthesis
  text. Use opaque IDs and redacted error messages.
- No arbitrary filesystem paths, remote media URLs, pickle uploads, or
  shell-interpolated filenames in the public API.
- The API must not silently fall back to CPU. Model absence is a provisioning
  error. Enrollment never fine-tunes model weights.
- Generated audio is not published anywhere. Do not add Twilio, messaging, or
  auto-publish behavior without a separate authorized task.
- Keep `.codex/HANDOFF.md`, `.reference/`, `.tmp/`, `.cache/`, `deploy/backups/`,
  and `deploy/.state/` untracked. Never commit secrets, weights, recordings,
  generated audio, or local configuration.
- `deploy/clawbox` may stage only the dedicated skill directory. Do not edit
  OpenClaw global config or grant the `coder` agent network/device access.
