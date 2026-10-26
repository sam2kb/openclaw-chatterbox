# Playable audio in the requesting conversation

```sh
clawvoice deliver <master-id> [<master-id> ...] --player --json
```

Copy the returned `reply` verbatim into the final answer as plain text. Put any
extra explanation before it. The helper resolves the original master, verifies
its checksum and readability, deduplicates identical bytes, and places exports
in the host agent's managed media library. Each described file has a standalone
`MEDIA:` line in the final block. Do not wrap it in backticks, quotes, bullets,
a table, a Markdown link, or a code fence. Put nothing after that block.

Those directives become OpenClaw media payloads and may be stripped from the
visible transcript. Check the media payload or actual audio player before
concluding that an attachment failed. A prose description without a directive
is not delivery. The helper's receipt is the presentation specification; avoid
re-litigating the format across turns.

The optional private player is built into the voice service. It shows the
original audio, duration, requested wording, transcription agreement or
differences, and owner listening decisions. It uses a scoped link, not the
share tray. Draft links expire with their audio; links for retained masters do
not expire automatically and can be revoked. Keep link fragments private.

After the owner chooses an original:

```sh
clawvoice artifacts keep <master-id> --name "Production greeting" --approved --json
clawvoice assets list --json
```

This keeps the service original and managed export without automatic expiry.
Run `deliver <master-id> --player --json` again to hand over the retained
asset and its new durable review link. An earlier draft link keeps its original
expiry; retaining audio does not silently extend that old credential.
Draft artifacts expire after seven days. Service cleanup runs at startup and
hourly; doctor exposes the last run. The host's managed export cache has daily
maintenance installed by `clawvoice library schedule --apply`. It removes only
expired checksum-named exports, preserves retained assets, and leaves arbitrary
workspace files alone. Never delete old handoff paths or real takes as part of
a comparison without an explicit owner instruction.

Deliver only to the owner in the requesting conversation. Do not publish audio,
send it to a third party, change Twilio, or substitute built-in TTS. A raw share
URL is not a player, and the share tray's network policy does not describe
webchat's attachment support.
