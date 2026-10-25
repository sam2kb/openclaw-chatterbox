# Create a voice

A browser records the owner's microphone; the server cannot open the owner's
microphone directly. The owner opens a short-lived, single-enrollment link in a
secure context. There is no host audio-device access for you.

## 1. Check readiness and existing profiles

```sh
clawvoice doctor --json
clawvoice voices list --json
```

If readiness is `model_missing`, `worker_offline`, or the backend is `fake`, stop
with the actionable reason from doctor. Do not create a profile against an
unready service.

## 2. Create the enrollment link

```sh
clawvoice enroll start --name "<owner-approved name>" --json
```

Return `recorder_url` to the owner in a private conversation. It contains a
one-enrollment credential in the URL fragment. Do not paste that URL into logs,
issue trackers, shared channels, or summaries. Tell the owner:

- Open the link on the machine/browser that can reach the service.
- Allow microphone access only after clicking **Record sample**, or choose
  **Upload sample**.
- Aim for 10–15 seconds of one speaker, select the reference clip, listen, and
  create the voice.

The session expires in about 30 minutes and can create one voice. If it expires,
start a new one.

## 3. After the owner says the voice is saved

```sh
clawvoice voices list --json
clawvoice voices show <voice-id> --json
```

Use the new voice ID, or ask the owner which saved name is theirs. The profile
is reference-conditioned: the **original** upload and a normalized mono
reference are stored; no model weights changed.

If `last_audition.status` is `succeeded`, fetch the audition artifact with
`clawvoice artifacts fetch <artifact-id> --output <path> --json` and attach it
per `delivery.md`. If it is still running, poll with bounded backoff. If it
failed, report its safe error message and the doctor readiness status; do not
pretend the voice was auditioned.

## 4. Voice selection

Only select a default when the owner asks:

```sh
clawvoice voices select <voice-id> --json
```

## Sample handling notes

- The server probes content, not filename or MIME type. It supports whatever the
  installed FFmpeg decoder supports, including common WAV/MP3/WebM/Opus/MP4/AAC
  browser outputs.
- The selected clip must be longer than 5 seconds and no longer than the pinned
  backend's 20-second bound. 10–15 seconds is recommended.
- The service flags silence, possible clipping, and unsuitable duration. It does
  not verify speaker identity and does not automatically denoise.
- If the owner uploads a long file, the UI asks them to select a segment; do not
  assume the whole file conditions the voice.
- If the owner sends a channel attachment, first resolve it to a path the agent
  can read, then use `clawvoice voices create --audio <path> --authorized`. Do
  not echo a local service-container path.
