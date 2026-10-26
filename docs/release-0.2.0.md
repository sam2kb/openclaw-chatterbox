# 0.2.0 voice workflow and verification

Deployed and tested on Clawbox on 2026-10-26 with OpenClaw 2026.10.7.
The host main agent uses the tracked `chatterbox-voice` skill and lightweight
client. The duplicate learned skill was archived through OpenClaw Workshop.

## Enrollment and diagnostics

Recorder links can create a profile or replace an existing profile's reference.
Replacement preserves the voice ID, name, and earlier reference versions. New
profile names remain editable before saving. The page explains single use and
expiry; the webchat Talk/dictation control is not a reference recorder.

Reference warnings describe stereo downmixing, clipping, background noise, and
speaker-identity limits. Doctor leads with the real service worker's readiness;
a client without local Torch or model files can still use the ready worker.

## Rendering and review

Sentence-first splitting preserves the input characters and reduces the amount
of text in each inference call. Rendering remains stochastic. Seeds do not
promise matching bytes, duration, or spoken words; sampling controls do not
change the reference accent.

`artifacts verify` stages the original under the existing transcription inbox,
records a checksum-bound transcript and signal measurements, then removes its
own staging directory. Article changes and missing final words stay visible.
ASR agreement is a check, and listening is still required to judge speech,
naturalness, and similarity.

The private HTTPS player compares original masters, requested wording,
transcripts, and listening decisions. Its credential is scoped to the selected
files, stored as a hash, removed from the address bar, and revocable. Downloads
are checked against the recorded audio checksum in the browser.

## Delivery and retention

`deliver` exports checksum-verified original 24 kHz mono WAV files into the
agent's managed media directory and produces the full reply with final
standalone `MEDIA:` lines. This avoids relying on the agent to reconstruct
attachment directives. Byte-identical exports share one file. The normal
workflow generates and delivers only original masters.

Retained assets keep the exact chosen original and request text without automatic
expiry. Deliver again after retaining a take to obtain a durable player link;
older draft links keep their existing expiry. Profile archive/restore is
reversible, and deletion is refused while a voice has retained production assets.

Service cleanup runs at startup and hourly, with its latest result visible in
Doctor. Managed exports have a daily user timer; retained exports survive it.
Existing temporary workspace WAVs were removed only after matching their private
backup checksums, as requested. Saved user profiles were preserved.

## Migration and recovery

Schema 3 adds the enrollment target field. The installer now makes a quiescent,
private, checksum-verified volume snapshot and checks SQLite integrity before a
schema upgrade. An explicit installation root keeps state and backups stable
when runtime code lives in an immutable release folder. Upgrade records that
folder, the snapshot, and both image identities.

An incompatible rollback is refused before touching the service. Recovery
requires a matching application and data snapshot; the installer does not
silently overwrite writes made after a migration.

## Checks completed

- 93 automated tests passed; Ruff and Python compilation passed. One upstream
  Starlette/anyio deprecation warning remains.
- Both built images, the wheel, bundle checksums, and generated OpenAPI checked.
- Live schema 2 to 3 upgrade passed after a verified quiescent backup. All three
  saved user profiles, their reference versions, default voice, and 52 existing
  artifact checksums matched the baseline.
- Browser tests covered editable default names, same-ID reference replacement,
  single-use/expiry behavior, scoped access, retained links, listening decisions,
  download checksums, and mobile layout. Live HTTPS replacement kept the test
  voice ID/name, added a reference version, and produced a real CUDA audition.
- Two real CUDA test masters were each 2.920 seconds. Local ASR agreed with both
  requested texts; signal checks flagged neither clipping nor very quiet audio.
  Transcription left no temporary staging directory behind.
- The live HTTPS player played both originals and downloaded matching files,
  removed the credential fragment, and had no page errors or mobile overflow.
- A fresh, isolated, non-delivered OpenClaw turn loaded the canonical skill and
  returned both files in native media payloads. No message was sent to a channel.
- The incompatible rollback guard preserved container IDs and process IDs.
  Periodic cleanup removed an expired isolated test artifact and preserved its
  retained counterpart. The live export cleanup timer and one-shot run passed.
- Synthetic test profiles, review links, and managed exports were removed.
  The user's three saved profiles remained intact.

## Remaining owner checks

Owner listening, similarity to personal references, and playback in the owner's
actual messaging channel remain to be checked. Automatic transcription and
signal metrics cannot settle those questions. The release manifest retains
candidate status for these limits. Torch, CUDA, model, Perth, and driver pins
are unchanged.
