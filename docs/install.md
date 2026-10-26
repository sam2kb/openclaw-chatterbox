# Install and operate

This document describes the deployment workflow. See `compatibility.md` for
the measured GPU checks and remaining owner listening checks.

## Docker Compose (preferred)

Prerequisites on the target: Docker Engine + Compose v2 or later (current Compose builds require Buildx 0.17+), NVIDIA driver and
Container Toolkit for GPU mode, FFmpeg only inside images, approx 10 GB free for
images plus ~3 GB for the pinned model, and a local filesystem for `data/`.
Do not mount model/data on NFS or SMB.

```sh
mkdir -p models
chmod 755 models
cp config/clawvoice.env.example config/clawvoice.env
chmod 600 config/clawvoice.env
# Edit the private env file. Never commit it.

# Build API and worker images (worker is large; target-only as needed).
docker compose --env-file config/clawvoice.env -f deploy/compose.yaml build

# Provision the pinned model explicitly. This does not run on every request.
# The provision service uses a throwaway /tmp data tree, not the runtime volume,
# so it cannot leave root-owned dirs where UID 10001 must write.
docker compose --env-file config/clawvoice.env -f deploy/compose.yaml \
  --profile tools run --rm provision
# The fetch normalizes model file modes; the parent models/ dir must be
# traversable by UID 10001.

# GPU target: add the GPU override after the model and image exist.
docker compose --env-file config/clawvoice.env \
  -f deploy/compose.yaml -f deploy/compose.gpu.yaml up -d
```

Loopback only by default: `127.0.0.1:8765`. Private data is a named Compose
volume (`openclaw-chatterbox_clawvoice-data`); model weights are a host bind at
`./models/chatterbox-turbo`. The API generates a service token at
`/data/secrets/service-token` on first start. Readiness first reports
`worker_loading`, then `ready` after the worker loads the model. Model absence is
reported as `model_missing`; the worker never downloads weights during a request.

For CPU evaluation, use `-f deploy/compose.cpu.yaml` instead of the GPU
override. It is an explicit configuration, not an automatic fallback. CPU
latency is unverified. A host bind for `data/` is not the default because a
directory bind created by Docker is usually owned by root; use the named volume
or prepare ownership for UID 10001 explicitly.

Copy the generated service token into the operator's protected flow before using
the host CLI or OpenClaw skill:

```sh
mkdir -p ./secrets
chmod 700 ./secrets
docker compose --env-file config/clawvoice.env -f deploy/compose.yaml \
  cp api:/data/secrets/service-token ./secrets/clawvoice-service-token
chmod 600 ./secrets/clawvoice-service-token
export CLAWVOICE_TOKEN_FILE="$PWD/secrets/clawvoice-service-token"
export CLAWVOICE_URL="http://127.0.0.1:8765"
clawvoice doctor --json
```

Do not commit the copied token. If a sandboxed agent needs it, use the sandbox's
documented read-only secret mount; skill environment injection does not reach a
sandbox.

## CLI only (development or operator host)

```sh
uv venv .venv --python 3.11
uv sync --extra dev
export CLAWVOICE_DATA_DIR="$PWD/var"
export CLAWVOICE_TOKEN_FILE="$PWD/var/secrets/service-token"
.venv/bin/clawvoice serve
```

For real inference, install the `inference` extra in an isolated environment and
provision the model. The base client deliberately has no Torch dependency.

## Environment

| Variable | Purpose | Default |
| --- | --- | --- |
| `CLAWVOICE_URL` | client API base URL | `http://127.0.0.1:8765` |
| `CLAWVOICE_TOKEN_FILE` | service token file | `./var/secrets/service-token` or `$XDG_CONFIG_HOME/clawvoice/service-token` for the CLI |
| `CLAWVOICE_SERVICE_TOKEN` | runtime secret alternative to file | unset |
| `CLAWVOICE_DATA_DIR` | private SQLite/files root | `./var` |
| `CLAWVOICE_MODEL_DIR` | provisioned model directory | `./models/chatterbox-turbo` |
| `CLAWVOICE_BACKEND` | `turbo` (default) or test-only `fake` | `turbo` |
| `CLAWVOICE_DEVICE` | `cuda` or `cpu`; never silently changed | `cuda` |
| `CLAWVOICE_PUBLIC_BASE_URL` | recorder link base | `http://127.0.0.1:8765` (a safe HTTPS path prefix is supported) |
| `CLAWVOICE_ALLOWED_ORIGINS` | comma-separated exact origins | base URL origin |
| `CLAWVOICE_MAX_QUEUE_DEPTH` | bounded queued/running jobs | 32 |
| `CLAWVOICE_ARTIFACT_RETENTION_DAYS` | generated audio retention | 7 |
| `CLAWVOICE_CLEANUP_INTERVAL_SECONDS` | background cleanup interval; 0 disables the loop | 3600 |

The fake backend is refused unless `CLAWVOICE_ALLOW_FAKE=1`; it is never a
production default.

## Native fallback

A venv/systemd deployment can be added later with a local worker unit. This
release ships only the Compose workflow plus a direct `clawvoice serve` command;
it does not maintain two full installers.

## OpenClaw

Installing the service does not install the skill or alter OpenClaw. See
`openclaw-integration.md` for the integration instructions. The supported caller is the authorized host `main` agent, not the
offline `coder` container. The installer stages only the dedicated skill; the client uses its default protected token path.

## Host verification and export maintenance

The ASR workflow uses the existing `/usr/local/bin/openclaw-transcribe` helper.
It stages and removes only its own private directory beneath `/srv/inbox`;
the helper's approved roots are unchanged. No ASR model is added to the voice
worker image. If that optional helper is unavailable, audio stays unverified.

As the authorized host agent user, run `clawvoice library schedule --apply`.
This installs only `clawvoice-cache-cleanup.service` and its daily user timer.
It removes expired managed exports, keeps retained masters, and does not scan
or delete arbitrary workspace audio. The API's separate hourly cleanup already
handles service artifacts. No Gateway restart or new development-harness access
is needed.
