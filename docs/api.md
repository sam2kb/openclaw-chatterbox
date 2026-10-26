# API

The API is versioned under `/api/v1`. Generate the exact schema with:

```sh
clawvoice openapi --output docs/api/openapi.json
```

All routes except `/api/v1/health/live` require
`Authorization: Bearer <service-token>`. Enrollment routes instead accept the
scoped single-enrollment token. Review routes accept only the scoped review token.
Artifact and voice identifiers are opaque.

## Key routes

| Method | Route | Scope |
| --- | --- | --- |
| GET | `/api/v1/health/live` | public, basic status only |
| GET | `/api/v1/health/ready` | service; 200 ready / 503 with reason |
| GET | `/api/v1/capabilities` | service |
| POST | `/api/v1/enrollment-sessions` | service; returns a one-use recorder URL |
| POST | `/api/v1/enrollment-sessions/{id}/uploads` | enrollment; raw audio body |
| POST | `/api/v1/enrollment-sessions/{id}/voices` | enrollment; creates a voice or revises the bound profile |
| GET | `/api/v1/enrollment-sessions/{id}/audition` | enrollment |
| GET | `/api/v1/enrollment-sessions/{id}/artifacts/{artifact}/download` | enrollment |
| GET/POST | `/api/v1/voices` | service; raw audio body for direct create |
| PATCH/DELETE | `/api/v1/voices/{id}` | service |
| POST | `/api/v1/voices/{id}/reference` | service; raw audio body, adds a reference version |
| POST | `/api/v1/voices/{id}/select` | service |
| POST | `/api/v1/voices/{id}/preview` | service |
| POST | `/api/v1/synthesis-jobs` | service |
| GET | `/api/v1/jobs/{id}` | service |
| POST | `/api/v1/jobs/{id}/cancel` | service |
| GET | `/api/v1/artifacts/{id}` | service |
| GET | `/api/v1/artifacts/{id}/download` | service |
| POST | `/api/v1/voices/{id}/archive` and `/restore` | service; reversible profile visibility |
| POST | `/api/v1/artifacts/{id}/verify` | service; audio-hash-bound transcript and signal metrics |
| POST | `/api/v1/artifacts/{id}/retain` | service; retain this original without automatic expiry |
| GET | `/api/v1/assets` | service; retained originals |
| POST/DELETE | `/api/v1/reviews` and `/api/v1/reviews/{id}` | service; create or revoke scoped review |
| GET | `/api/v1/reviews/{id}` | review; selected originals and requested wording |
| GET | `/api/v1/reviews/{id}/artifacts/{artifact}/download` | review; selected original only |
| POST | `/api/v1/reviews/{id}/artifacts/{artifact}/listen` | review; checksum-bound listening decision |

Enrollment creation accepts exactly one of `name` (editable initial default) or
`voice_id` (replace that profile's reference). Review creation accepts original
24 kHz mono masters only. Draft review links expire with the earliest selected
artifact; a review of retained originals has no automatic expiry.

## Synthesis body

```json
{
  "voice_id": "voc_...",
  "segments": [
    {"type": "text", "text": "Thank you for calling."},
    {"type": "pause", "pause_ms": 2000},
    {"type": "text", "text": "Press 2 for billing."}
  ],
  "preset": "master",
  "controls": {"temperature": 0.8, "top_p": 0.95, "top_k": 1000, "repetition_penalty": 1.2},
  "seed": 2026
}
```

Use `text` instead of `segments` for plain literal text. Supplying both is a
validation error. Unsupported fields are rejected rather than ignored. `voice_id`
may be omitted only when a default voice has been selected.

The response is `202` with a queued job. Poll `GET /api/v1/jobs/{id}` until
`status` is `succeeded`, `failed`, or `canceled`; `cancel_pending` is not a
terminal state. Successful jobs include artifact metadata and per-artifact
manifests. Ordinary job responses omit request text and private filesystem paths.
Verification records include the ASR transcript; scoped review responses include
the selected originals' requested wording and transcript for comparison.

## Errors

```json
{"error":{"code":"validation_error","message":"...","retryable":false,"details":{}}}
```

Status mappings include 401 auth, 409 expired/conflict, 413 byte limit, 415
unsupported media, 422 validation, 429 queue full, 503 not ready/model/worker
unavailable, and 507 storage full. Error messages are designed to be safe to
show to the owner; they never contain the service token.
