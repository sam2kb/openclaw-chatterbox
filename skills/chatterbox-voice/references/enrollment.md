# Create or update a saved voice

1. Run `clawvoice voices list --json` and resolve the requested name or ID.
2. For a new voice use `clawvoice enroll start --name "My voice" --json`.
   The name is an editable suggestion on the recorder page. Report the final
   saved name and ID, not the link's original default.
3. To update an existing voice from the microphone or a browser upload, use
   `clawvoice enroll start --voice <voice-id> --json`. The page binds the update
   to that ID, keeps the name and old reference versions, and creates no extra
   profile. A direct local file can use `voices revise <id> --audio <path>
   --authorized --json`.
4. Give `recorder_url` only to the owner. It is single-use and normally expires
   after about 30 minutes. A used or expired link needs a fresh session; do not
   try to recover a credential from browser history. Browser refresh removes
   the fragment credential, so use a fresh link if needed.

The webchat composer mic performs Talk/dictation. It does not create an
uploadable voice note. The recorder link captures the browser microphone and
also accepts an audio file. No host microphone-device access is needed.

For an attachment, resolve its existing agent-readable media path. Use
`voices create --name <name> --audio <path> --authorized --json` for a new
profile, or `voices revise` for an existing one. Do not pass `--authorized`
unless the owner owns the voice or has permission; a clear earlier statement
is enough.

Recommend 10-15 seconds of one speaker with a quiet background. The clip must
be longer than 5 seconds and no longer than 20 seconds. The UI previews and
selects a segment; the original upload is retained. The model uses a mono
reference, so stereo input is downmixed. Noise is not removed automatically,
clipping can affect quality, and speaker identity is not verified. Surface
relevant `voice.warnings` instead of burying them in JSON.

After saving, check the final profile and audition job. A queued job is not a
completed preview. Fetch/deliver the audition with the standard delivery
helper, or report its safe error. Do not select a default voice unless asked.
Archive stale profiles reversibly when requested; do not delete recordings to
resolve duplicate names.
