# openclaw-chatterbox

Save a voice from a browser microphone recording or audio file, then ask
OpenClaw to read your text. The service generates original 24 kHz mono WAV
locally with Chatterbox Turbo and keeps recordings and audio private.

## Use it through OpenClaw

- “Create a voice called My voice from this recording.” Attach a file you own
  or have permission to use.
- “I'd like to record a new voice.” OpenClaw gives you a private recorder link.
- “Update My voice with a new recording.” The recorder updates that profile's
  reference and keeps its ID and earlier versions.
- “Use My voice to say: Thank you for calling. Please leave a message.”
- “Keep take 2 as Production greeting.” OpenClaw retains that exact original.
- “Send Production greeting again.” It retrieves the saved file without another
  synthesis.

The webchat composer mic is Talk/dictation. Use the recorder link to capture
an audio file. A new voice's name is editable before saving; the link's name is
only a default. Links are single-use and normally expire after 30 minutes.
Ask for a fresh link if one has expired or been used.

A clear 10-15 second clip is recommended. Stereo is mixed to mono, background
noise is not removed automatically, and clipping can affect the result. The
service records permission attestation and does not verify speaker identity.
Enrollment uses the sample as a reference; it does not fine-tune model weights.
See [recording guidance](docs/recording.md) and [training](docs/training.md).

## Generate, check, and listen

The service splits text at sentence boundaries while preserving the supplied
characters. Explicit pause segments are available. Each new render is a
stochastic take: a seed does not guarantee bytes, timing, duration, or wording.
Sampling controls do not set an accent; accent comes from the reference.

Local transcription compares the generated audio with the requested words.
The result is bound to the audio checksum. It flags article changes, missing
endings, and other differences without silently accepting them. Transcription
can also be wrong, so agreement is not proof. Signal checks measure levels and
possible clipping; they cannot judge naturalness or voice similarity.

A private player shows each original, its requested wording, transcript check,
and listening decision. The owner can compare takes and choose one to retain.
OpenClaw delivery uses standalone `MEDIA:` directives so described files become
actual chat attachments. The helper produces the complete presentation block.
A raw share URL is not used as a substitute for a player.

## Original files and retention

The normal workflow delivers only the original master. It does not synthesize
another preset or produce telephony/downsampled variants. Historical artifact
formats remain readable. An explicitly requested encoding should be derived
from the approved original, never from another synthesis.

Draft service artifacts expire after seven days; cleanup runs at startup and
hourly. Doctor reports the schedule and last result. Managed host exports have
a daily cleanup timer and deduplicate only byte-identical audio by SHA-256.
Different takes of the same length remain different files.

Retained originals and their managed exports have no automatic expiry.
Generate a fresh player link after retaining a take: earlier draft links keep
their original expiry. Retained links can be revoked. Saved assets are listed
by name and ID, and reversible voice archival hides stale profiles without
deleting their recordings or production audio.

## Install and operate

Use [installation](docs/install.md), [OpenClaw integration](docs/openclaw-integration.md),
and [the runbook](docs/runbook.md). The API stays on loopback; the recorder and
private player can use an HTTPS prefix such as `/clawvoice/`. Scoped credentials
stay in URL fragments and browser memory. The host service credential stays in
a protected file.

```sh
clawvoice doctor --json
clawvoice enroll start --name "My voice" --json
clawvoice enroll start --voice voc_... --json
clawvoice speak --voice voc_... --text-file greeting.txt --wait --json
clawvoice artifacts verify art_... --json
clawvoice deliver art_... --player --json
clawvoice artifacts keep art_... --name "Production greeting" --approved --json
clawvoice assets list --json
clawvoice voices archive voc_... --json
clawvoice voices list --all --json
clawvoice voices restore voc_... --json
clawvoice library schedule --apply --json
```

The base client has no Torch dependency. Doctor's service-worker assessment is
authoritative; local probes describe only the machine running the client.

Version 0.2.0 adds a schema migration. Take a verified private data snapshot
before upgrade. Automatic rollback across incompatible schemas is refused;
matching data must be restored deliberately. See the runbook. The pinned
Torch 2.6.0/CUDA 12.4 stack has generated speech on a GTX 1070 Ti; measured
hardware results and remaining owner listening checks are in
[compatibility](docs/compatibility.md).

## Development

Python 3.11 is supported. The fake backend exercises behavior with a tone and
cannot prove voice quality or GPU support:

```sh
uv sync --frozen --extra dev
CLAWVOICE_BACKEND=fake CLAWVOICE_ALLOW_FAKE=1 CLAWVOICE_DEVICE=cpu .venv/bin/clawvoice serve
```

Provision the pinned Turbo model explicitly for real inference; requests do
not download weights or silently fall back to CPU. See
[architecture](docs/architecture.md), [security](docs/security.md),
[API](docs/api.md), and [evaluation](docs/evaluation.md).

Project code is Apache-2.0. Chatterbox, the pinned Turbo checkpoint, and Perth
are MIT; see `THIRD_PARTY_NOTICES.md`.
