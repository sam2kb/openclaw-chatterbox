# openclaw-chatterbox

Save a voice from a microphone recording or audio file, then ask OpenClaw to
turn your text into an audio file. Chatterbox Turbo generates speech locally;
the service keeps recordings and generated files in private storage.

## Use it through OpenClaw

After installation, you can say:

- "Create a voice called My voice from this audio file." Attach a recording
  you own or have permission to use.
- "I'd like to record a new voice." OpenClaw gives you a private HTTPS link
  where you can use the microphone or upload a file.
- "Use My voice to make a WAV saying: Thank you for calling. Please leave a
  message after the tone."
- "Make an MP3 of this text using My voice."

OpenClaw saves the voice, waits for generation, and returns the audio file.
You can reuse a saved voice without uploading the sample again. A clear
10-15 second clip is recommended; the recorder lets you preview and select it.

Requests to "train a voice" use the sample as a reference for synthesis. This
is reference-conditioned cloning: enrollment stores the recording and does not
fine-tune model weights. Voice identity is consent-attested, not automatically
verified. See [recording guidance](docs/recording.md) and the
[training boundary](docs/training.md).

## Deployment status

Version 0.1.1 fixes real GPU startup on a read-only filesystem, worker readiness,
smoke-test reporting, HTTPS path-prefix routing, and upgrade recovery. The pinned
Torch 2.6.0/CUDA 12.4 stack has loaded and generated speech on a GTX 1070 Ti.
[Compatibility results](docs/compatibility.md) distinguish measured GPU behavior
from the listening and speaker-similarity checks that still need an owner review.

The OpenClaw integration is a skill and lightweight client. It provides the
recording/file-to-voice and text-to-audio workflow; it does not replace Talk mode,
OpenClaw's built-in TTS provider, or voice-call providers.

## Install

Use [the installation guide](docs/install.md) to build the API and worker,
provision the pinned model, and install the skill for the authorized host agent.
The deployment plan is read-only:

```sh
deploy/clawbox plan --json
```

A private HTTPS route may use a prefix such as `/clawvoice/`. Set
`CLAWVOICE_PUBLIC_BASE_URL` to that full service URL and
`CLAWVOICE_ALLOWED_ORIGINS` to the HTTPS origin. The API port stays on loopback.
The browser uses a short-lived, single-enrollment credential; the service
credential remains in a protected file on the host.

## Outputs and behavior

- Master 24 kHz mono WAV, MP3, 8 kHz telephony WAV, and optional raw mu-law.
- Structured text/pause segments. A `pause_ms: 2000` segment inserts exactly
  2,000 ms of digital silence during assembly.
- Durable SQLite queue with one inference worker, cancellation and interrupted
  job recovery. Profile conditioning is serialized and restored between renders.
- Explicit model provisioning and no automatic CPU fallback.
- Private local artifacts. Sending to other people, Twilio changes, calls, and
  publishing are outside this service.

## Developer quick start

The fake backend exercises the API and recorder without loading a model. It
produces a tone and cannot establish voice quality or GPU support.

```sh
uv venv .venv --python 3.11
uv sync --frozen --extra dev
export CLAWVOICE_DATA_DIR="$PWD/var"
export CLAWVOICE_BACKEND=fake
export CLAWVOICE_ALLOW_FAKE=1
export CLAWVOICE_DEVICE=cpu
export CLAWVOICE_TOKEN_FILE="$PWD/var/secrets/service-token"
.venv/bin/clawvoice serve
```

Open <http://127.0.0.1:8765/>. For real synthesis, provision the model explicitly:

```sh
clawvoice models fetch --backend turbo --revision 749d1c1a46eb10492095d68fbcf55691ccf137cd
```

The worker checks the pinned files before loading and does not fetch weights
during an audio request.

## Operator CLI

The client installs without Torch. Ordinary use through OpenClaw does not
require the owner to run these commands:

```sh
clawvoice doctor --json
clawvoice enroll start --name my-voice --json
clawvoice voices create --name my-voice --audio ./sample.wav --authorized
clawvoice voices revise voc_... --audio ./new-sample.wav --authorized
clawvoice voices list --json
clawvoice speak --voice voc_... --text-file greeting.txt --preset master --json --wait
clawvoice render --voice voc_... --segments-file greeting.json --preset telephony --json --wait
clawvoice artifacts fetch art_... --output greeting.wav --json
```

Credentials come from a mode-0600 file or runtime secret environment variable,
never from command arguments. JSON results go to stdout and diagnostics to stderr.

See [architecture](docs/architecture.md), [security](docs/security.md),
[API](docs/api.md), [operations](docs/runbook.md), and
[OpenClaw integration](docs/openclaw-integration.md).

## License

Project code is Apache-2.0. Chatterbox, the pinned Turbo checkpoint and Perth
are MIT; their notices are in `THIRD_PARTY_NOTICES.md`.
