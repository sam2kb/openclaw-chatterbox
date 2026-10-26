# OpenClaw integration

The `chatterbox-voice` skill lets the authorized host agent save a voice from an
attached audio file or a browser microphone recording, then generate and return
an audio file from the owner's exact text. It uses a local HTTP service and the
`clawvoice` client; it does not require Torch in the agent's environment.

## Install for the host agent

1. Check the installed OpenClaw version, workspace and effective tool policy.
   The main agent must be able to execute the client and read the downloaded
   artifacts. Host binary discovery does not prove access from a sandbox.
2. Install the client wheel in a Python 3.11 environment. Expose `clawvoice` on
   the host agent's PATH, for example with a link in `~/.local/bin/`.
3. Copy the service credential privately to
   `~/.config/clawvoice/service-token`, owned by the OpenClaw service account
   with mode 0600. The client discovers it there by default. Set
   `CLAWVOICE_URL` only if the API is not at `http://127.0.0.1:8765`.
4. Configure the service's public URL for the recorder, such as
   `https://your-clawbox.example/clawvoice`, and allow that exact HTTPS origin.
   Proxy that prefix to the loopback API, stripping the prefix. Keep the service
   credential out of the browser; the recorder uses its scoped enrollment token.
5. Run the deployment installer with the authorized agent workspace. It stages
   only `skills/chatterbox-voice/`. Confirm `openclaw skills info chatterbox-voice`
   reports it eligible and visible, and `clawvoice doctor --require-ready --json`
   reports a ready worker with `real_model: true`.

A deployment that uses these default paths does not need an OpenClaw global
config edit, a Gateway restart, or credentials embedded in a skill file.

## Owner workflow

- "Use this audio file as my voice." The agent confirms the owner's permission,
  creates a named reference voice from the attached file, and reports its audition.
- "Let me record a voice." The agent returns the private HTTPS enrollment link.
  The owner records or uploads a sample, selects a clip, and saves the voice.
- "Use that voice to say this text." The agent preserves the supplied text,
  queues generation, waits for the result, downloads the artifact and returns it
  to the owner in the same conversation.

If several voices match, the agent asks which one. If no reference exists, it
asks for a recording/file. Enrollment conditions on the recording and does not
fine-tune weights. No Twilio, voice-call, Talk-mode or built-in provider change
is part of this workflow.

## Verify without external delivery

Use synthetic test speech for the canary. Verify file upload, a ready real-model
worker, job polling, artifact download, and the browser enrollment/audition flow.
A non-delivered agent run can confirm it discovers and executes the skill without
sending any message to another person. Then let the owner record their own voice
and listen for similarity. GPU success alone does not establish that quality.

## Keep the development harness isolated

Do not give `coder` or dsh the service credential, a private service route, GPU
access, host access or Docker socket. Do not install this operational skill into
the offline coding agent. Existing host access does not authorize expanding its
sandbox policy. If a different intended caller is sandboxed, configure only its
approved client/secret route and verify it from that sandbox separately.

## Rollback

Remove or restore only the dedicated skill and its client link. Keep voice data
and the token private. Service upgrade/rollback uses `deploy/clawbox` and retains
the data volume. Follow `runbook.md`; do not overwrite `openclaw.json` wholesale.
