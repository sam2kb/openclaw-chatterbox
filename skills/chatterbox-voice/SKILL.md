---
name: chatterbox-voice
description: Record or upload a voice sample, update an existing voice, generate original-quality speech, check its wording, retain approved takes, and deliver playable audio attachments.
metadata:
  {"openclaw": {"emoji": "🎙️", "os": ["linux"], "requires": {"bins": ["clawvoice"]}}}
---

# Chatterbox Voice

This is the canonical voice workflow. Use the installed client and references
here; avoid maintaining a second learned recipe for the same service.

## Check the service

Run `clawvoice doctor --json`. Continue when `assessment.status` is `ready`
and `assessment.real_model` is true. Service worker readiness is authoritative;
the lightweight client does not need local Torch or model weights. Never use a
fake backend for owner audio or claim that its tone proves voice quality.

## Record, upload, or update a voice

Read `{baseDir}/references/enrollment.md`. List voices before creating one.
Reuse an unambiguous existing voice; clarify multiple matches. A request to
update a voice should replace that profile's reference, preserving its ID.

The webchat mic is Talk/dictation, not an audio-file recorder. For microphone
enrollment give the owner a private recorder link from `clawvoice enroll start`.
An attached audio file can be used directly. Ownership or permission must be
clear; do not repeat a consent question the owner has already answered.

Enrollment conditions synthesis on a recording; it does not train model weights.
Read and report relevant reference warnings, including mono downmix, clipping,
noise, and the absence of speaker-identity verification.

## Generate and check

Read `{baseDir}/references/generation.md`. First check `clawvoice assets list
--json`: an approved saved asset should be delivered unchanged, without another
synthesis. For new speech save the exact supplied text to a private UTF-8 file.
Do not rewrite it. Use original 24 kHz WAV; do not create telephony/downsampled
files. Produce one take unless the owner requests a batch.

Each new render is stochastic. A seed does not guarantee the same bytes,
duration, timing, or wording. Controls affect sampling and stability; accent
comes from the reference. Sentence chunks reduce long-render drift but cannot
guarantee correct speech.

Run `clawvoice artifacts verify <master-id> --json` for a new take. It uses the
existing local transcription helper and cleans up its own temporary inbox copy.
The result is bound to the audio checksum. Treat `asr_agreement` as a useful
second check. It does not prove the spoken words. A mismatch can be TTS error or
ASR error, including articles, names, numbers, and trailing words. Do not hide
those differences or rerender indefinitely. Offer the private player for the
owner to listen and choose. Never claim listening or speaker similarity without
an actual owner review.

## Deliver every described take

Read `{baseDir}/references/delivery.md`. Run:

```sh
clawvoice deliver <master-id> [<master-id> ...] --player --json
```

Use the returned `reply` as the final reply, verbatim and outside a code fence.
It includes short labels, a private player link, and standalone `MEDIA:` lines
at the end. Add any necessary explanation before that block. Do not replace the
lines with Markdown links, a table, prose, or a claim that files are attached.
Every take described must have a corresponding real, readable media file.

The visible transcript may omit `MEDIA:` directives after OpenClaw extracts the
attachments. This is not evidence that delivery failed. Check returned media
payloads or the actual player. Do not infer that webchat cannot attach files
from share-tray documentation.

## Keep an approved original

After the owner chooses a take, use `clawvoice artifacts keep <master-id>
--name <asset-name> --approved --json`. Pass `--approved` only for that exact
owner-accepted file. The master keeps its checksum, has no automatic expiry,
and is discoverable with `assets list`. Never regenerate a production asset to
make a different format or retrieve it again. A retained but unreviewed draft
can omit `--approved`; describe it honestly.

Use `voices archive <id>` to hide an unwanted profile reversibly. `voices list
--all` includes archived profiles; `voices restore <id>` restores them. Do not
delete existing profiles or audio merely to tidy the list.

## Boundaries

Keep recordings, text, service credentials, and scoped link fragments private.
Deliver only to the owner in the requesting conversation. No third-party
messages, public uploads, calls, Twilio changes, built-in TTS substitution, or
new runtime access for the development harness. See `references/sandbox.md`
when the caller is contained. Do not weaken transcription read-root checks.
