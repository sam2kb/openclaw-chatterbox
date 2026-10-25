# openclaw-chatterbox

A local, reference-conditioned voice service built on Chatterbox Turbo, with a
small browser recorder, a lightweight `clawvoice` CLI, and an OpenClaw skill.

> Status: **buildable release candidate; target hardware support is not yet
> verified.** This repository was implemented in a development environment
> without a usable GPU. See [docs/compatibility.md](docs/compatibility.md) for
> the explicit target-only gates.

## What works

- Create a named voice from a microphone recording or an audio file. This is
  reference-conditioned cloning: it stores your recording and uses it as the
  model prompt. It does **not** fine-tune model weights.
- Generate new speech from literal UTF-8 text, with a selected voice.
- Render structured text/pause segments; `pause_ms: 2000` inserts 2,000 ms of
  digital silence during assembly, not a punctuation hint to the model.
- Master WAV, MP3, 8 kHz telephony WAV, and optional raw mu-law outputs.
- Durable SQLite queue with one inference worker, cancellation, restart
  recovery, and serialized profile conditioning.
- Short-lived, single-enrollment browser sessions with scoped credentials.
- OpenAPI schema and a stdlib-only client that installs without Torch.

## Quick start (development, fake backend only)

```sh
uv venv .venv --python 3.11
uv sync --extra dev
export CLAWVOICE_DATA_DIR="$PWD/var"
export CLAWVOICE_BACKEND=fake
export CLAWVOICE_ALLOW_FAKE=1
export CLAWVOICE_DEVICE=cpu
export CLAWVOICE_TOKEN_FILE="$PWD/var/secrets/service-token"
.venv/bin/clawvoice serve
```

Open <http://127.0.0.1:8765/>. The fake backend is for exercising the API/UI and
tests only; it is not Chatterbox and produces a tone.

For the real path, see [docs/install.md](docs/install.md) and
[docs/compatibility.md](docs/compatibility.md). The pinned Turbo model is
provisioned with:

```sh
clawvoice models fetch --backend turbo --revision 749d1c1a46eb10492095d68fbcf55691ccf137cd
```

The worker refuses to start without provisioned files; it never downloads a
model during a caller's request and never falls back from CUDA to CPU.

## CLI

```sh
clawvoice doctor --json
clawvoice enroll start --name my-voice --json
clawvoice voices create --name my-voice --audio ./sample.wav --authorized
clawvoice voices revise voc_... --audio ./new-sample.wav --authorized
clawvoice voices list --json
clawvoice speak --voice voc_... --text-file greeting.txt --preset master --json
clawvoice render --voice voc_... --segments-file greeting.json --preset telephony --json
clawvoice jobs status job_... --json
clawvoice artifacts fetch art_... --output greeting.wav --json
```

Secrets are read from a mode-0600 token file or a runtime secret environment
variable, never from command arguments. Use `--json` for machine-readable
stdout; diagnostics go to stderr.

## Design boundaries

- This service only generates local audio artifacts. It does not publish them,
  change Twilio, or send messages.
- Voice enrollment is consent-attested by the owner. Speaker identity is not
  automatically verified.
- `docs/training.md` explains why no training command ships.
- The current OpenClaw integration is a skill + CLI client, not a replacement
  for OpenClaw's built-in TTS provider, Talk mode, or voice-call providers.

See [docs/architecture.md](docs/architecture.md), [docs/security.md](docs/security.md),
[docs/api.md](docs/api.md), [docs/runbook.md](docs/runbook.md), and
[docs/training.md](docs/training.md). The deploy plan is:

```sh
deploy/clawbox plan --json
```

The API image is built and smoke-tested; the Pascal worker image and real-model
acceptance remain target gates. Do not describe this release candidate as
target-supported until those gates are recorded.

## License

Project code is Apache-2.0. Chatterbox and the pinned Turbo checkpoint are MIT;
their notices are included in `THIRD_PARTY_NOTICES.md`.
