# Compatibility and target verification

**Status: the target GPU path is not verified.** This release was built in an
environment with the same reported CPU/kernel shape but no usable GPU, no
NVIDIA Torch, and no model weights. The test suite uses a synthetic backend and
does not prove CUDA support, audio quality, latency, or VRAM use.

## Owner-supplied target (not independently probed)

- Linux x86_64, kernel `6.12.101+deb13-amd64`
- Ryzen 7 5800XT, 8 cores / 16 threads, 32 GB RAM
- GTX 1070 Ti, 8 GB VRAM, driver `550.163.01`
- Pascal compute capability 6.1

The build session's `doctor` did confirm Debian 13, the same kernel family and
CPU model, 16 logical threads, about 31 GiB RAM, and about 328 GiB free disk. It
also reported `nvidia-smi` failing (no usable GPU exposed to this session) and
Torch not installed. That is not target verification.

## Pins

| Component | Pin |
| --- | --- |
| Chatterbox source | `5de7a54aa4e5e2baadb0182dde554908b48b85c2` |
| Chatterbox Turbo model | `ResembleAI/chatterbox-turbo` at `749d1c1a46eb10492095d68fbcf55691ccf137cd` |
| Perth | `ff1c8ac55a976971245cdd53c18d6131ca00d993` |
| Python | 3.11 (development used 3.11.16) |
| Candidate inference runtime | torch/torchaudio `2.6.0+cu124`, Python 3.11 |
| FFmpeg | system/container package; probe records the actual version |

The lock pins `torch==2.6.0` and `torchaudio==2.6.0` through the explicit
`https://download.pytorch.org/whl/cu124` index. This is a **candidate** selected
because the Pascal 6.1 architecture is old and newer CUDA/PyTorch packaging has
removed Pascal in some builds. CUDA 12.4 supports Pascal, and the 2.6.0 cu124
wheels are the intended starting point. Do not upgrade CUDA, PyTorch, the
NVIDIA driver, or the model revision as part of an application upgrade.

The lockfile is generated and checksummed; it has not been proven on the target
GPU. The API image was built and smoke-tested in this session. The worker image
build did not complete here because the large CUDA/NVIDIA dependency downloads
stalled; that is not a substitute for the target build. The container digest
becomes release evidence only after the operator builds on the target and
records it.

## Mandatory operator gate

Run these on the authorized host, not in a restricted development container:

```sh
clawvoice doctor --json
clawvoice models fetch --backend turbo \
  --revision 749d1c1a46eb10492095d68fbcf55691ccf137cd
clawvoice models verify --backend turbo --deep --json
clawvoice models smoke --reference /path/to/owner-approved-sample.wav \
  --text-file /path/to/smoke.txt --output /tmp/clawvoice-smoke.wav --json
clawvoice doctor --json
```

Accept the GPU path only if all are true:

- `doctor.torch.allocation_math.passed` is true on the GTX 1070 Ti.
- `doctor.torch.capability` is `6.1`, and the compiled arch list includes the
  device's architecture or a compatible one.
- `models smoke` produces non-empty audio and a finite, measured duration.
- Peak VRAM, process RAM, cold load time, warm render latency, generated
  duration, and real-time factor are recorded from the actual run. Do not
  promise a footprint before measuring.
- A two-profile acceptance run passes (see `evaluation.md`).

Then update this document with measured numbers and the tested image digest and
mark the target verified. Until then, all install instructions produce a
release candidate, not a target-supported deployment.

## Runtime policy

- Eager inference, FP32. No FlashAttention, Triton, BF16, or `torch.compile`
  requirement.
- FP16 is not enabled. Evaluate it only if the chosen backend supports it and
  measurements justify audio/latency/memory tradeoffs.
- No silent CPU fallback. If CUDA is unavailable, allocation/math fails, or the
  model OOMs, the worker reports failure and readiness becomes not-ready.
- CPU mode is available through the Compose CPU override for evaluation, but it
  is not performance-verified. Nano is not shipped in this release.
- Exposed controls are only those the pinned Turbo implementation supports:
  temperature, top_p, top_k, repetition_penalty, and seed. CFG and exaggeration
  are intentionally not accepted; the inspected Turbo code ignores them.
- A seed is not a cross-hardware bit-for-bit reproducibility promise.
