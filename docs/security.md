# Security and privacy

## Trust boundaries

- The owner's browser is untrusted input.
- The OpenClaw skill/agent is a caller with a service credential and must not
  receive other agents' data.
- The inference worker is the only process that needs the model and GPU.
- The API does not need GPU access. Compose keeps GPU reservation on the worker.
- Model weights, recordings, normalized references, artifacts, generated text,
  and secrets are excluded from Git and ordinary release archives.

## Authentication

- All profile, synthesis, upload, job, artifact, readiness, and capability
  routes require an `Authorization: Bearer` service token, including loopback.
- A liveness route is the only unauthenticated API response and discloses only
  service/version/status.
- Enrollment sessions use a separate random token; only its SHA-256 hash is
  stored. The token is scoped to one session, can create one voice or revise
  its bound profile, expires promptly, and cannot call profile/synthesis/admin routes.
- Review tokens are separate hashed bearer credentials, scoped to selected
  original artifacts and listening decisions. They cannot list profiles, retain
  assets, or call service administration. Draft links expire with the earliest
  selected artifact; retained-only links are explicitly revocable.
- Browser enrollment and review tokens live in a URL fragment and then in
  memory. They are stripped from the address bar on load and never written to localStorage,
  sessionStorage, cookies, or analytics. No cookie authentication is used, so
  cookie CSRF is not applicable.
- Service token files must be mode 0600; the CLI refuses group/other-readable
  token files. Secrets are never accepted as command arguments.

## Input and files

- Uploads are opaque bytes. FFprobe decides the actual format; filename and MIME
  are not trusted. Only a small allowlist of common audio containers is accepted,
  and decoder invocations use `-protocol_whitelist file` to block playlist/remote
  protocol fetches.
- Byte limits, decoded-duration limits, decoder runtime limits, text length,
  segment count, pause totals, output duration, queue depth, storage use, and
  artifact retention are bounded independently.
- Temporary decodes are named by the service, kept under private storage, and
  removed on failure. ffmpeg is invoked with argument arrays, `-nostdin`, bounded
  timeouts, and no shell.
- Artifact and reference paths come from opaque IDs and private path columns.
  Public download code refuses absolute paths, `..`, symlinked components, and
  non-regular files by opening with `O_NOFOLLOW`.
- There is no public endpoint accepting a server filesystem path, a remote media
  URL, or an uploaded pickle/checkpoint. The only model inputs are the pinned
  local safetensors files fetched by an explicit operator command.
- Enrolled audio is never executed as code.

## Web

- CORS uses an exact origin allowlist; wildcard origins are rejected and
  credentialed CORS is disabled.
- The recorder and review pages use only same-origin assets. CSP blocks third-party script,
  style, frame, and connect sources; no analytics.
- Security headers: `X-Content-Type-Options`, `Referrer-Policy: no-referrer`,
  `X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy`, and a narrow
  `Permissions-Policy`.
- API responses use `Cache-Control: no-store`; artifact downloads set
  `private, no-store` and an attachment filename derived from the artifact ID.

## Logging and errors

- Do not log service tokens, enrollment tokens, raw recordings, or synthesis
  text. Uvicorn access logging is disabled by default in `clawvoice serve`.
- Job storage retains the request text while the job needs it. After the audio
  retention window, cleanup redacts terminal job text; API status responses
  expose only a text hash and counts. Error messages are stable, short, and do
  not include exception internals or user text.
- Input manifests record text hashes. Audio verification adds the ASR transcript.
  Scoped reviews disclose the selected files' requested wording and transcript;
  protect review links as credentials. Retained assets keep their request text
  for later wording review.
- The doctor does not print credentials or file contents; it reports model
  directories but not secret paths containing tokens.

## Authorization and abuse

- The service is single-owner by design. There is no multi-tenant identity
  system; do not expose it to an untrusted network.
- Enrollment links are a bearer capability. Anyone who obtains one before it is
  consumed can upload a sample. Send them only in a private channel.
- Generated audio is watermark-bearing upstream, but watermarking is not a
  provenance guarantee for re-encoded/edited copies. Refer to Chatterbox's
  notice and document derived outputs accordingly.
- Voice clones can be misused. The owner attestation is deliberately simple,
  not identity verification. Operators remain responsible for consent and law.

## Retention and deletion

- Generated artifacts default to 7 days. Startup and hourly cleanup (also
  available as `clawvoice cleanup`) removes expired
  rows/files and clears stale preview references.
- Saved voices and their original/normalized references persist until explicitly
  deleted. Voice deletion removes reference originals, normalized files, and all
  generated artifacts for that voice. Retained production assets prevent voice
  deletion; archive the profile to hide it while preserving those files.
  Retained originals have no automatic expiry, and their managed exports survive
  daily cache cleanup. Uninstall without `--delete-data` keeps
  the data volume.
- Back up the SQLite database together with the private files for consistent
  recovery. The token is a separate secret; do not include it in ordinary
  backups if the backup store is less protected.
- SQLite WAL must remain on a local filesystem for this single-host design.
  Connections enable `secure_delete`; explicit voice deletion checkpoints and
  truncates WAL so deleted metadata is not left in old WAL frames. This is
  best-effort deletion on the local filesystem, not cryptographic erasure.
