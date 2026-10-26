# Operator runbook

All commands here are operational. `plan` is read-only. `install`, `upgrade`,
`rollback`, and `uninstall` require explicit authorization; none were run
against a live OpenClaw/Clawbox in this build.

## Plan

```sh
deploy/clawbox plan --json
deploy/clawbox plan --openclaw-workspace /path/to/agent/workspace
```

The plan reports prerequisites, disk, port, image identities, model state,
volume ownership, detected OpenClaw version, and the exact commands it would
run. It does not fetch weights, build images, start containers, restart
OpenClaw, register the skill, or write config. `models verify` is run in a
read-only mode.

## Install (authorized window)

```sh
deploy/clawbox install --apply --gpu --allow-unverified-target \
  --openclaw-workspace /path/to/main-agent-workspace
```

Before the change, verify: the image tags and any recorded local image IDs match
the release manifest; the model marker matches the pinned revision; the target
GPU gate passed (or you explicitly pass `--allow-unverified-target` for a
candidate); the OpenClaw version/policy inventory is recorded; the backup
location is owner-only. The installer starts Compose, waits for authenticated
readiness, then stages and validates skill files; it backs up any existing
`chatterbox-voice` skill and preserves unrelated files and agents. It does not
fetch model weights or change NVIDIA drivers/CUDA. A failed readiness check
leaves the previous skill untouched.

If the target agent is sandboxed, stop: the installer's host skill copy is not
enough. Complete the sandbox plan in `openclaw-integration.md` and test from the
real sandbox.

## Verify

```sh
deploy/clawbox verify --json
clawvoice doctor --json
clawvoice healthcheck
curl -fsS http://127.0.0.1:8765/api/v1/health/live
```

Check readiness is `ready`, the worker is online, the model revision is pinned,
and the skill is discoverable for the intended agent. Then run the canary from
`openclaw-integration.md` and the real acceptance in `evaluation.md`.

## Upgrade

```sh
deploy/clawbox upgrade --apply --gpu --allow-unverified-target \
  --release /path/to/new-release
```

Use only a release bundle whose `SHA256SUMS` you verified. The installer checks
the manifest's model identity and image tags/recorded local IDs, but this project
does not implement package signatures. The upgrade must not silently upgrade the
NVIDIA driver, CUDA compatibility, Python/Torch pins, or model revision. It
backs up the current skill, stages the new skill, and restores it if Compose or
readiness fails; it records the new release only after readiness succeeds. If
the model revision changes, it requires a separate compatibility gate.

## Rollback

```sh
deploy/clawbox rollback --apply --backup <id> --json
```

Restores the previous skill from a backup under `deploy/backups`, points
Compose at the recorded previous release tag, and leaves the data volume
untouched. It records the rollback only after readiness succeeds. Model files
and driver state are not changed. Backups outside `deploy/backups` are refused.

## Uninstall

```sh
deploy/clawbox uninstall --apply
```

Stops and removes containers and the skill installed by this project. The data
volume and generated audio are retained by default. `--delete-data` is a
separate destructive operation and must be paired with the actual Compose
volume name:

```sh
deploy/clawbox uninstall --apply --delete-data \
  --confirm-delete-data openclaw-chatterbox_clawvoice-data
```

The tool removes only that named volume after teardown; it does not use
`compose down -v`, and it removes the skill only after teardown succeeds.

## Backups

Back up the data volume and SQLite database together while the service is
stopped or after a quiescent checkpoint. Include `voices/` and `artifacts/` with
the database so reference paths remain consistent. The service token is a
secret; store it in the existing operator secret flow rather than in the same
unprotected archive. Do not back up model weights as voice data.

## Restore

Restore the data volume, start the API first to validate schema compatibility,
then the worker. Readiness distinguishes `model_missing`, `worker_loading`, and
`worker_failed`; do not assume success from a running container alone.

## Logs and incidents

Logs must not be copied into tickets with authorization headers or request
bodies. If a token was exposed, rotate it and restart the API; enrollment links
are single-use and expire. If a job was interrupted, confirm it was recovered
under the same ID or failed with `worker_interrupted`. Never delete a partially
written artifact by hand while the worker is active; stop the worker and run
`clawvoice cleanup`.
