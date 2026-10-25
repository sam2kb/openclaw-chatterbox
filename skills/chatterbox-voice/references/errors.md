# Errors and exit codes

`clawvoice` writes machine JSON to stdout with `--json`; diagnostics go to
stderr. Exit codes:

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | success | continue |
| 2 | validation: bad audio, text, segment, name, or missing job/voice | ask the owner to correct the input; do not retry blindly |
| 3 | configuration or authentication | check `CLAWVOICE_URL`, token file mode/owner, `clawvoice doctor --json` |
| 4 | unavailable: service, worker, queue, timeout, model, decoder | report readiness reason; retry once after a bounded wait if transient, otherwise stop |
| 5 | job failed | surface `error.code`/`error.message`; no artifact should be delivered |
| 6 | job canceled | say it was canceled; do not treat partial files as success |

Common conditions:

- `worker_offline` / `worker_loading`: the model worker is starting or absent.
  Readiness is not a license to fall back to CPU or a default voice.
- `model_missing`: run only the documented operator provisioning command; do not
  download weights during a request.
- `job_timeout`: report the deadline. Do not resubmit indefinitely.
- `cuda_oom`: the worker ran out of GPU memory and did not fall back to CPU.
  Report it; a new smaller job may be attempted only if the owner agrees, with
  measurements, not by changing the device silently.
- `queue_full`: wait for bounded polling; do not flood the queue.
- `invalid enrollment credential`: the link expired or was already used; create
  a new session only with the owner's agreement.
- `audio_decode_failed`: the file is malformed or unsupported. Ask for another
  sample; do not parse or convert it with unsafe third-party tooling.

Never print service tokens, enrollment tokens, raw text, or raw recordings in
error reports. The API is designed to return safe messages; trust those rather
than dumping internal logs.
