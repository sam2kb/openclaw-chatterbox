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
run. It must not fetch weights, build images, start containers, restart
OpenClaw, register the skill, or write config.

## Install (authorized window)

```sh
deploy/clawbox install --apply \
  --openclaw-workspace /path/to/main-agent-workspace
```

Before the change, verify: service image digests match the release manifest;
the model marker matches the pinned revision; the target GPU gate passed; the
OpenClaw version/policy inventory is recorded; the backup location is
owner-only. The installer stages and validates skill files, backs up any
existing `chatterbox-voice` skill, preserves unrelated files and agents, and
starts only the API/worker. It does not fetch model weights or change NVIDIA
drivers/CUDA.

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
deploy/clawbox upgrade --apply --release /path/to/new-release
```

Use only a new signed/manifested release. The upgrade must not silently upgrade
the NVIDIA driver, CUDA compatibility, Python/Torch pins, or model revision.
It backs up the current skill and release pointer, stages the new files, and
keeps a rollback pointer. If model revision changes, it requires a separate
compatibility gate and explicit approval.

## Rollback

```sh
deploy/clawbox rollback --apply --backup <id> --json
```

Restores the previous skill and release pointer, recreates/repoints containers
to the previous image tags, and leaves the data volume untouched. Model files
and driver state are not changed.

## Uninstall

```sh
deploy/clawbox uninstall --apply
```

Stops and removes containers and the skill installed by this project. The data
volume and generated audio are retained by default. `--delete-data` is a
separate, explicit destructive operation that must name the volume and confirm
interactively; it is never implied by uninstall.

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
