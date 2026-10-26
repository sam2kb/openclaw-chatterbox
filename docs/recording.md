# Recording and reference samples

## User flow

1. OpenClaw creates a single-enrollment session and gives the owner a recorder
   URL. The URL fragment holds the scoped credential; the page removes it from
   the address bar as soon as it loads.
2. The owner clicks **Record sample**. That click is the first point at which
   the page requests microphone permission. The page never records in the
   background and stops all tracks on Stop, re-record, navigation, page hide,
   timeout, error, and successful stop.
3. The owner listens and can re-record, or switches to **Upload sample**. The
   browser checks `MediaRecorder.isTypeSupported` rather than assuming WAV.
4. The server probes the actual bytes with FFprobe, decodes a normalized mono
   24 kHz PCM WAV, and reports duration, source rate/channels, silence,
   possible clipping, and limitations.
5. The owner selects the reference segment. The UI shows the proposed segment;
   the selection is passed explicitly to the server. Long uploads are never
   silently treated as one conditioning clip.
6. The owner attests "my voice or permission obtained", names the voice, and
   creates it. A short audition is queued. The UI polls and downloads the
   audition with its scoped credential.

## Practical guidance

- Target 10–15 seconds of one person speaking naturally.
- The pinned Turbo backend asserts the reference is **longer than 5 seconds**
  and uses bounded conditioning windows (15 s encoder, 10 s decoder). The
  service enforces a 5.001 s minimum and a 20 s selected-clip maximum.
- Prefer a quiet room and a consistent distance from the microphone. The
  service does not claim perfect denoising; denoising is not part of v1.
- Avoid clipping. The checks flag peaks near 0 dBFS as possible clipping.
- The service does not verify speaker identity. The attestation is a simple
  consent record, not an identity product.
- The original upload is kept byte-for-byte in private storage. The normalized
  reference is a separate file. Neither is written to Git or the public API.
- The service flags a largely silent source and low source sample rates, but
  these checks are advisory; listening remains the authority.

## Supported input

Content is detected by FFprobe; extensions and client MIME types are not
trusted. WAV, MP3, WebM/Opus, and MP4/AAC browser outputs are supported when the
installed FFmpeg decoder supports them. The service permits a small allowlist of
common audio containers and passes `-protocol_whitelist file` to FFprobe/FFmpeg,
so a playlist/container cannot turn uploads into arbitrary network fetches.
Unsupported or malformed files return a useful `audio_decode_failed` error
without exposing decoder internals.

Limits are configured in `Settings`: upload bytes, decoded source duration,
decoder timeout, normalized reference duration, and total private storage.

## Revising a saved voice

When the owner wants a better reference clip, create a new reference version
without losing the old one:

```sh
clawvoice voices revise <voice-id> --audio ./new-sample.wav --authorized
```

The old original/normalized files remain as version rows until the voice is
explicitly deleted. The new version starts an audition job and becomes current
only after the normalization succeeds. Jobs already queued snapshot their
reference-version ID and continue with the clip they were submitted against.
The recording still does not fine-tune weights.

## Microphone secure context

Browsers require a secure context for `getUserMedia`: localhost or an explicitly
configured private HTTPS origin. Use SSH forwarding to localhost or a private
HTTPS reverse proxy. Do not expose the recorder on a public interface just to
make microphone permission work. If the owner cannot use a microphone, upload
is the standard alternative.

## Server-side physical microphone

A microphone attached to the service host is intentionally not captured in v1.
That would be a separate device-discovery adapter with explicit owner consent,
not a hidden fallback.
