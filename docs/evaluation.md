# Real-model acceptance

Mocks in CI prove API, storage, and assembly behavior. They prove **nothing**
about voice quality, CUDA, latency, memory, clipping, or speaker similarity.
Run this on the authorized Pascal host with owner-provided samples.

## Prerequisites

- `clawvoice doctor --json` shows CUDA allocation/math passed, model files
  verified, service ready, and real backend.
- `models smoke` has completed once and recorded its honest metrics in
  `data/state/compatibility.json`.
- Two distinct, consented reference samples exist. Do not use public figures.

## Script

Use natural text that covers a greeting, names, numbers, a question, a longer
paragraph, and the exact phone greeting:

```text
Thank you for calling Acme Dental. My name is Morgan Lee.
Our office is at 1420 West Market Street, Suite 8.
Would you like to schedule an appointment for Tuesday at 3:30?
We offer cleanings, whitening, emergency visits, and pediatric care.
If you are a new patient, please have your insurance information ready.
Press 1 to schedule. Press 2 for billing.
```

Create a structured file with the exact two-second pause:

```json
[
  {"type": "text", "text": "Thank you for calling Acme Dental."},
  {"type": "pause", "pause_ms": 2000},
  {"type": "text", "text": "Press 2 for billing."}
]
```

```sh
clawvoice render --voice <voice-a> --segments-file greeting.json \
  --preset master --seed 2026 --json --wait --timeout 300
# Fetch both master and telephony artifacts with artifacts fetch.
```

Repeat for voice B. Then alternate A/B/A jobs submitted back-to-back while the
worker is busy, and submit overlapping requests for both profiles. Confirm each
job's output is its own profile and that the worker reports one active render.

## Listen and measure

Record on the target:

| Measurement | A | B |
| --- | --- | --- |
| GPU / driver / capability | | |
| Torch / CUDA / torchaudio | | |
| Model commit/revision | | |
| Cold model load seconds | | |
| Warm render seconds | | |
| Generated audio duration | | |
| Peak VRAM MiB | | |
| Process RSS MiB | | |
| Real-time factor | | |

Listen for duration, inserted silence (measure the master sample count; 2,000 ms
at 24 kHz must be 48,000 samples), clipping, missing or repeated words,
intelligibility of names/numbers, naturalness, and resemblance to each
reference. Check both master and telephony files on the intended consumer. State
explicitly if carrier compatibility is not tested.

Optional local ASR can flag obvious word errors, but it is not a substitute for
listening and cannot prove speaker similarity. Do not make unsupported
competitive claims.

## Failure handling

- OOM: record the job id and metrics. Do not silently switch to CPU or a smaller
  model. Tune with measurements and a new release decision.
- CUDA unavailable or a compiled-architecture mismatch: stop; the release is not
  target-supported.
- Worker crash: confirm the same job id is recovered or failed with
  `worker_interrupted`; verify no partial artifact is downloadable.
- A/B/A mismatch or output from the previous profile: block the release and
  investigate conditioning restore and worker serialization.

## Definition of done

Only after a fresh supported host can install the pinned release, create two
voices, generate and receive playable audio through OpenClaw, listen to both
presets, restart without losing profiles/jobs, and uninstall/roll back while
retaining data may this be called a supported deployment. Until then, ship the
release candidate and list the target-only gates above as unverified.
