# Compatibility and target verification

Versions 0.1.1 and 0.2.0 have loaded the pinned model and generated speech on the actual
Clawbox GTX 1070 Ti. This establishes GPU execution for the measured stack.
Owner listening, speaker similarity, and playback in the owner's messaging channel
remain unverified, so the release remains a candidate.

## Measured target, 2026-10-26

- GTX 1070 Ti, 8 GiB VRAM, NVIDIA driver `550.163.01`, capability `6.1`.
- Python 3.11.16, Torch and torchaudio `2.6.0+cu124`, eager FP32 inference.
- CUDA allocation and matrix math passed. The wheel includes compatible `sm_60`.
- All required model files passed the pinned SHA-256 checks before loading.
- API and worker run in separate containers with a read-only root filesystem.
  Runtime caches use `/tmp`; the worker has GPU access and the API does not.

The smoke used a synthetic speech reference, not a real person's voice. It
produced 5.920 seconds of mono 24 kHz audio:

| Measurement | Result |
| --- | --- |
| Model load | 12.435 s |
| Cold synthesis | 10.339 s (RTF 1.747) |
| Warm synthesis | 2.086 s (RTF 0.352) |
| Peak CUDA allocation | 3,146.1 MiB |
| Peak process RSS | 5,147.7 MiB |

These are observations from one short run, not resource or latency guarantees.
Synthetic-reference tests cannot establish resemblance to an owner's voice.
The release manifest records the final image identities and remaining checks.

## 0.2.0 acceptance, 2026-10-26

The same pinned runtime generated two synthetic-reference original masters,
each 2.920 seconds at 24 kHz mono. Local ASR agreed with the requested words in
both, and signal checks found no clipping or very quiet flag. Live HTTPS browser
checks played and downloaded both originals with matching SHA-256 checksums.
Same-ID browser re-enrollment also produced a real CUDA audition.

A fresh non-delivered OpenClaw 2026.10.7 turn loaded the canonical skill and
extracted both master files into native media payloads. This establishes the
attachment path through response parsing; owner-channel playback and personal
voice similarity still require owner review. See [the release checks](release-0.2.0.md).
## Pins

| Component | Pin |
| --- | --- |
| Chatterbox source | `5de7a54aa4e5e2baadb0182dde554908b48b85c2` |
| Chatterbox Turbo model | `ResembleAI/chatterbox-turbo` at `749d1c1a46eb10492095d68fbcf55691ccf137cd` |
| Perth | `ff1c8ac55a976971245cdd53c18d6131ca00d993` |
| Python | 3.11 (development used 3.11.16) |
| Candidate inference runtime | torch/torchaudio `2.6.0+cu124`, Python 3.11 |
| FFmpeg | system/container package; probe records the actual version |

The lock uses the explicit `https://download.pytorch.org/whl/cu124` index.
Keep the driver, CUDA/PyTorch stack, model revision, and upstream source pins
stable during ordinary application upgrades. A change to those pins needs a new
hardware and audio evaluation.

## Repeat the hardware check

Run on the authorized host, not the restricted development harness:

```sh
clawvoice doctor --json
clawvoice models verify --backend turbo --deep --json
clawvoice models smoke --reference /path/to/owner-approved-sample.wav \
  --text-file /path/to/smoke.txt --output /tmp/clawvoice-smoke.wav --json
```

The lightweight host client does not need Torch; its service readiness reports
the container worker. Run `models smoke` inside the worker runtime for GPU
measurements. See `evaluation.md` for owner listening and two-profile checks.

## Runtime policy

- Eager inference, FP32. No FlashAttention, Triton, BF16, or `torch.compile`
  requirement.
- FP16 is not enabled. Evaluate it only if the chosen backend supports it and
  measurements justify audio/latency/memory tradeoffs.
- No silent CPU fallback. If CUDA is unavailable, allocation/math fails, or the
  model OOMs, the worker reports failure and readiness becomes not-ready until
  the worker reloads/probes successfully. Other CUDA faults should be treated as
  a degraded worker if observed in acceptance.
- CPU mode is available through the Compose CPU override for evaluation, but it
  is not performance-verified. Nano is not shipped in this release.
- Exposed controls are only those the pinned Turbo implementation supports:
  temperature, top_p, top_k, repetition_penalty, and seed. CFG and exaggeration
  are intentionally not accepted; the inspected Turbo code ignores them.
- A seed is not a cross-hardware bit-for-bit reproducibility promise.
