# Deliver audio without publishing it

Only deliver to the owner in the conversation that requested the audio. If the
request arrived in a group or shared channel, ask for a private destination.

## Use the installed channel's documented media mechanism

OpenClaw's outbound-media command (current docs checked 2026-10-25) is:

```sh
openclaw message send --channel <current-channel> --target <current-target> \
  --message "Here is the generated greeting." \
  --media <agent-accessible-path>/greeting.wav --json
```

Use the current channel and target already known to the session; do not invent
one. If the agent's native message tool has documented `media`/`filePath`
fields, you may use those instead. The file must be readable by the OpenClaw
process/channel sender, not merely by the clawvoice worker container.

Audio support varies by channel. If `openclaw message send` reports that audio
cannot be attached, tell the owner plainly and give the local artifact path plus:

```sh
clawvoice artifacts fetch <artifact-id> --output ./greeting.wav --json
```

Do not fall back to another person's chat, upload to a public URL, publish to
Twilio, or call any voice service. Do not claim delivery succeeded unless the
send command returns success. For phone greetings, also state that carrier
compatibility is not verified until tested.

## Do not

- Send the enrollment link or service token to a group.
- Attach the raw enrollment recording to a third party.
- Use OpenClaw's built-in `tts`, Talk mode, or a voice-call provider as if this
  skill installed it. Those are separate provider integrations.
- Post the artifact to a web service or object store.
