---
name: chatterbox-voice
description: Create named local voices from recordings and generate private speech audio with clawvoice.
metadata:
  {
    "openclaw": {
      "emoji": "🎙️",
      "os": ["linux"],
      "requires": { "bins": ["clawvoice"] }
    }
  }
---

# Chatterbox Voice

Use this skill when the owner asks to create a voice, record or upload a voice
sample, generate speech, or build a phone greeting from a named voice.

## Start every run

1. Run `clawvoice doctor --json`. Continue only if the service is reachable and
   `service.ready.status` is `ready`, and the worker reports a real model
   (`service.ready.checks.worker.real_model`). The local `torch`/`model.ok`
   fields describe the machine running the client, not necessarily the host
   worker; for a sandboxed/remote caller trust the service readiness response.
   A fake backend is for tests only; say so and stop.
2. Use the CLI's `--json` output. Do not print the service token or an
   enrollment URL fragment into logs, shell history, or a group chat.
3. Decide the mode:
   - **Create voice** when a named profile is missing: read
     `{baseDir}/references/enrollment.md`.
   - **Generate speech** when the owner gave a voice (or a default exists):
     read `{baseDir}/references/generation.md`.
   - For failures and exit codes: `{baseDir}/references/errors.md`.
   - For delivery: `{baseDir}/references/delivery.md`.
   - If the agent runs in a sandbox/container, read
     `{baseDir}/references/sandbox.md` before invoking the client; host
     `requires.bins` does not prove sandbox reachability.

## Hard boundaries

- Enrollment stores and conditions on a reference recording. It does **not**
  train model weights. Never call it training or fine-tuning.
- Never run a `train` command, invent one, or modify Twilio, place calls, send
  messages to third parties, or publish audio. Generating audio only creates a
  local artifact.
- Deliver generated audio only to the owner in the conversation that asked for
  it. Do not send a voice sample or generated voice to another person.
- Do not claim speaker-identity verification or guaranteed voice similarity.
- If the owner has no usable voice profile, offer enrollment. Do not substitute
  OpenClaw's built-in TTS provider or a default Chatterbox voice.

## Fast path for an existing voice

```sh
clawvoice voices list --json
clawvoice speak --voice <voice-id> --text-file <exact-text-file> --preset master --json
clawvoice jobs status <job-id> --json
clawvoice artifacts fetch <artifact-id> --output <agent-accessible-path>/greeting.wav --json
```

Send the exact owner text. Do not summarize it. For a scripted phone greeting
with a real pause, use `render` with a segments file instead of punctuation;
see the generation reference.
