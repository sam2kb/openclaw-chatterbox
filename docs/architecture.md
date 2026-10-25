# Architecture

```text
Owner browser (record / upload / listen)
        |
        |  single-enrollment bearer credential in memory
        v
FastAPI API + static recorder UI          OpenClaw skill / clawvoice CLI
        |                                        |
        |  SQLite metadata + transactional queue |
        +------------------+---------------------+
                           |
                 one inference worker process
                           |
           Chatterbox Turbo model kept loaded
                           |
        private originals, normalized references, artifacts
```

## Processes

- `clawvoice serve` runs the API and recorder UI. It can include an embedded
  worker for development, or run with `--no-worker` when a dedicated worker
  container is used.
- `clawvoice worker` runs one dedicated inference worker. It acquires a SQLite
  lease that permits exactly one active worker across processes. The model is
  loaded once and reused.
- The worker never downloads a model during a job. Model files must be
  provisioned by the explicit `models fetch` flow and carry the pinned marker.
- API and worker share the SQLite database and private data volume. SQLite WAL
  stays on a local filesystem; do not put it on a network share.

## Persistence

`data/clawvoice.sqlite3` stores voice/profile metadata, reference provenance,
job state, artifacts, the default voice selection, and worker leases. Private
files live under:

```text
data/voices/<voice-id>/reference/<ref-id>/original.<ext>
data/voices/<voice-id>/reference/<ref-id>/reference.wav
data/artifacts/<job-id>/<artifact-id>.<ext>
data/enrollment/<session-id>/<upload-id>/original.<ext>
data/tmp/
data/state/compatibility.json   # explicit real-model smoke record
data/secrets/service-token      # mode 0600
```

Artifact rows are written only after an atomically replaced file exists. Job
success and its artifact rows commit in one transaction. A crash can leave an
unreferenced file, cleaned by `clawvoice cleanup`; it cannot expose a partial
successful artifact because downloads require an artifact row and an opaque ID.

## Job state machine

```text
queued --claim--> running --commit--> succeeded
  |                 |    \--failure--> failed
  |                 \--cancel--> canceled
  \--cancel-------------------------> canceled

running + expired lease --recovery--> queued (bounded attempts)
queued (attempts exhausted) ---------> failed (worker_interrupted)
```

One worker claims one job. Profile/conditioning switching, seed setup, and
generation are serialized. The backend restores the previous model
conditioning in a `finally` block even if a render fails. The worker checks
cancellation before/after each text chunk and again immediately before the
success transaction; a cancellation that arrives during a model call is
reported as `cancel_pending`, not as success.

## Text and audio assembly

- Literal text is preserved as the request input and chunked only at whitespace
  boundaries. A single overlong token is rejected, never truncated. The plan
  asserts that concatenating chunks reconstructs the input exactly.
- Each text chunk is rendered by Chatterbox. Chunks within a text segment are
  joined with a small configured gap; explicit pause segments insert exact
  sample-count silence at the native output rate.
- `pause_ms: 2000` at 24 kHz inserts 48,000 zero PCM samples. The model is not
  asked to interpret punctuation or a pause tag.
- The master is a 24 kHz mono PCM WAV at the actual model sample rate. `mp3`
  and `telephony` are derived outputs; the master remains.
- Chatterbox's Perth watermark is applied upstream and retained in the master.
  Derived MP3/telephony files are re-encoded copies of that master.

## Authentication and scopes

- One service bearer token, stored in a mode-0600 file or runtime environment,
  protects voices, synthesis, jobs, artifacts, readiness, and capabilities.
- A separate single-enrollment token is available only for one enrollment
  session. It can stage a sample, create at most one voice, read that session's
  audition job, and download that audition. It cannot call voice, synthesis,
  job, or general artifact endpoints.
- The recorder URL carries the enrollment credential in a URL fragment. The
  fragment is never sent to the server, is removed from browser history on
  load, and is held in memory only. It is not placed in localStorage or a
  cookie, so no cookie-based CSRF path exists.
- The service token is never placed in a URL, command argument, log, or skill
  file.

## Deliberate omissions

No Redis, Kubernetes, queue plugin, frontend framework, third-party page asset,
analytics, fine-tuning command, Twilio integration, or automatic publishing is
part of this service. See `training.md` for the training boundary.
