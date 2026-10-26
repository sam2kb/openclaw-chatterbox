# Generate original audio and check the wording

Reuse a retained approved asset from `clawvoice assets list --json` whenever
that is what the owner wants. Its original audio is the production asset.

For a new take, preserve the exact requested text in a private UTF-8 file:

```sh
clawvoice speak --voice <voice-id> --text-file <text-file> --wait --timeout 300 --json
```

The service splits at sentence boundaries while preserving every supplied
character; long sentences also respect the size bound. Explicit text/pause
segments are available through `render --segments-file`. A `pause_ms: 2000`
segment inserts 48,000 zero samples at 24 kHz. Do not substitute punctuation for
an explicitly requested pause or change its length without instruction.

Deliver the master artifact from the successful job. New renders are
stochastic, including with the same seed; duration can vary too. Controls do
not set an accent. A shorter render or sentence chunk can still omit, add, or
mispronounce words. Do not promise exact speech from an input-text hash.

```sh
clawvoice artifacts verify <master-id> --json
```

This stages only a private temporary copy under the existing approved inbox
root, runs local transcription, removes that copy, and records the comparison
against the audio checksum. `needs_review` means to listen, not to assume TTS
failed or silently normalize the difference. `asr_agreement` also needs owner
listening for sound and voice similarity. Preserve articles and trailing words
in the comparison. Do not produce unbounded retry batches.

Give the owner the checked take and private player with `clawvoice deliver`.
After an explicit choice, retain that exact master with `artifacts keep`.
No telephony/downsampled output is needed. If an owner separately asks for an
encoded format, derive it from the approved original, never by synthesizing
again with another preset.
