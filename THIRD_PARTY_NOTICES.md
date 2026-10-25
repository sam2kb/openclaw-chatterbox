# Third-party notices

## Chatterbox

- Project: https://github.com/resemble-ai/chatterbox
- Pinned commit: `5de7a54aa4e5e2baadb0182dde554908b48b85c2`
- License: MIT (Copyright (c) 2025 Resemble AI)
- Model: `ResembleAI/chatterbox-turbo`
- Pinned revision: `749d1c1a46eb10492095d68fbcf55691ccf137cd`
- Model license: MIT (per the pinned model card metadata)
- Watermarking: Perth implicit watermark, applied by upstream Chatterbox

Chatterbox and the model are not vendored in this repository. Docker/model
provisioning fetches them at the pinned identities; weights are excluded from
Git and ordinary release archives.

## Perth

- Project: https://github.com/resemble-ai/Perth
- Pinned commit: `ff1c8ac55a976971245cdd53c18d6131ca00d993`
- License: MIT

## PyTorch / Torchaudio

- Candidate runtime: 2.6.0+cu124 from the official PyTorch CUDA 12.4 index
- License: BSD-style; see the installed package notices.
- This dependency is not installed in the lightweight client.

## FFmpeg

- Invoked as an external program; distribution licensing depends on the FFmpeg
  build. The container uses Debian's package and its notices.
- FFmpeg is not bundled in the Python wheel.

## Python dependencies

The locked Python dependencies retain their own licenses. Run
`uv tree --invert` or inspect the lock for the exact graph; no dependency is
relicensed by this project.
