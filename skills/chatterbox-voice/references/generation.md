# Generate speech

## Resolve the voice

```sh
clawvoice voices list --json
```

Use an explicit `--voice <id>` when the owner named one. If omitted, the CLI
uses the selected default. If there is no default, ask instead of guessing.

## Preserve the owner's text

Write the exact text to a private temp file in an agent-accessible directory,
then pass `--text-file`. Do not summarize, rewrite, or translate unless asked.
The service records the text and preserves it; it reports any explicit
normalization in the artifact manifest.

For a reusable phone greeting, use structured segments so the silence is exact:

```json
[
  {"type": "text", "text": "Thank you for calling Acme."},
  {"type": "pause", "pause_ms": 2000},
  {"type": "text", "text": "Press 2 for billing."}
]
```

```sh
clawvoice render --voice <voice-id> --segments-file <segments.json> \
  --preset telephony --json
```

`pause_ms: 2000` inserts exactly 2,000 ms of silence during assembly at the
model's native rate. Do not rely on punctuation or pause tags inside text.
Require the pause to remain 2000 unless the owner explicitly changes it.

## Presets

- `master`: mono PCM WAV at the model's actual 24 kHz sample rate.
- `mp3`: listening/download copy; the master is retained.
- `telephony`: derived 8 kHz mono PCM WAV; the master is retained.
- `telephony-mulaw`: optional raw 8 kHz mono mu-law, documented separately.

Do not promise that a carrier or phone system accepts a file until it has been
tested with that consumer. Do not relabel a sample rate.

## Follow the job

The default is asynchronous. Poll with a bounded timeout:

```sh
clawvoice jobs status <job-id> --json
```

or use `--wait --timeout 180` on `speak`/`render`. A job is done only when
`status` is `succeeded`. `cancel_pending` is not canceled yet. On `failed`,
report `error.code` and `error.message`; do not fabricate audio.

## Retrieve the artifact

```sh
clawvoice artifacts fetch <artifact-id> --output <agent-accessible-dir>/<name>.wav --json
```

Use the artifact's opaque ID, never a guessed path. The client verifies the
SHA-256 header and reports the output path. The service stores originals in
private runtime storage; a service container path is not automatically
accessible to the agent.

Then attach per `delivery.md`. A generated file is not published until you
deliver it to the owner's conversation.
