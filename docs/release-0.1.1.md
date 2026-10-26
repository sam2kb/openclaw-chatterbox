# 0.1.1 deployment and verification

Tested on Clawbox on 2026-10-26 with OpenClaw 2026.10.6. The dedicated
`chatterbox-voice` skill is eligible and visible to the host main agent. Its
lightweight client is on the gateway's existing PATH. The development harness
and coder sandbox received no credentials, service access, or GPU access.

## Changes

- Place model/runtime caches in writable `/tmp` for read-only containers.
- Preserve real-model identity in worker readiness when CUDA diagnostics exist.
- Hash the normalized smoke reference before its temporary directory is removed.
- Keep model diagnostics on stderr so smoke `--json` is parseable.
- Support a validated HTTPS path prefix in recorder links, assets, and API calls.
- Preserve model mounts, env files, and device mode across release folders.
  Upgrade waits for service readiness before replacing the dedicated skill;
  failed startup attempts restore the previous service. Verify checks the actual
  running image IDs for both services.
- Exclude the uv download cache from runtime image layers.
- Restart the service after Docker/host restarts with `unless-stopped`, while
  respecting an operator stop. The original `on-failure` policy did not do this.
- Document the natural OpenClaw microphone/file enrollment and text-to-audio flow.

## Checks completed

- 75 tests passed, Ruff passed, and Python compilation passed. One upstream
  Starlette/anyio deprecation warning remains.
- Built both pinned images and the wheel; checked bundle `SHA256SUMS` and excluded
  runtime secrets, recordings, generated audio, and weights from the package.
- Real GPU loading, CUDA allocation/math, and cold/warm synthesis passed. The
  final-image smoke returned clean JSON: model load 12.533 s, cold 9.521 s,
  warm 1.189 s for 3.080 s audio; peak CUDA allocation 3,098.9 MiB and
  process RSS 5,041.5 MiB. See `compatibility.md` for the tested stack and
  the separate earlier measurement.
- Two synthetic references were queued A/B/A. Each job retained its own voice
  and reference metadata, and A/B generated distinct audio. Repeated A audio was
  not byte-identical; a fixed seed is not a bit-for-bit guarantee.
- WAV, MP3, 8 kHz PCM WAV, and raw mu-law outputs passed format and checksum
  checks. A structured 2,000 ms pause inserted 48,000 zero samples at 24 kHz;
  the measured zero run was 48,337 samples including adjacent natural silence.
- Cancellation produced no artifacts. A job queued while the worker was stopped
  completed after restart, and saved profiles persisted.
- Chromium completed upload and microphone enrollment over HTTPS, saved each
  voice, and downloaded a real audition without page errors or failed requests.
  Microphone capture used a synthetic WAV device fixture. The enrollment fragment
  was removed from the browser URL after loading.
- An isolated, non-delivered OpenClaw turn read the skill, checked real-model
  readiness, generated speech, and fetched a checksum-verified 24 kHz WAV.
- Same-version upgrade from the source checkout to the extracted release bundle,
  verify, rollback to the source checkout, and verify all passed with data retained.
  Cross-version rollback has unit coverage but was not exercised live.

## Deployment

The service project is `/srv/openclaw-chatterbox`. API binds only to
`127.0.0.1:8765`; nginx proxies the existing HTTPS site's `/clawvoice/` prefix.
The named data volume is `openclaw-chatterbox_clawvoice-data`. Both containers
run as UID 10001 with a read-only root filesystem; only the worker gets the GPU.

Private configuration is `config/clawvoice.env` (0600). The host client's token
is `~/.config/clawvoice/service-token` (0600), outside Git. The dedicated skill
is under the authorized OpenClaw workspace's `skills/chatterbox-voice` directory.
No OpenClaw global configuration change or gateway restart was needed.

Compose 5.5.1 required a newer Buildx than Debian's 0.13.1. The official Buildx
0.38.1 binary was installed in the system CLI plugin directory after checking its
published SHA-256. The existing Debian plugin and Docker daemon were retained;
no NVIDIA driver, CUDA toolkit, daemon configuration, or reboot change was needed.

## Remaining owner checks

Personal voice similarity, subjective listening quality, delivery as an audio
attachment on the owner's actual messaging channel, and playback through the
intended phone system remain unverified. The release manifest keeps the candidate
status for those checks. Synthetic test profiles were removed after acceptance;
create your own saved voice through OpenClaw before ordinary generation.
