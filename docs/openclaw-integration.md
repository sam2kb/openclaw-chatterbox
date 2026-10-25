# OpenClaw integration plan

> This is a plan and a review artifact, not evidence that a live OpenClaw
> installation was changed. The actual OpenClaw version and effective tool
> policy were **not inspectable** from this development session: `/srv/openclaw`
> returned permission denied, and the referenced `D:/repos/openclaw-infra`
> files are not present on this host. No live change was attempted.

## Current facts checked for this build (2026-10-25)

- OpenClaw loads `SKILL.md` from `<workspace>/skills` (highest precedence),
  managed `<state-dir>/skills`, and extra dirs. The skill here uses the
  AgentSkills format with `name`, `description`, and `metadata.openclaw`.
- `metadata.openclaw.requires.bins` is checked on the host at load time. A
  sandboxed agent must also have the binary inside the sandbox.
- `skills.entries.*.env`/`apiKey` inject into the host agent turn, **not** the
  sandbox.
- The documented outbound media path is `openclaw message send ... --media
  <path>` and agent-side structured `media`/`filePath` fields.
- These are current docs, not a claim about every installed version. Confirm
  them with the installed version's `--help`, `openclaw skills check`, and
  `openclaw doctor` before changing anything.

## Boundary: `coder` stays contained

The existing policy review says the `coder` container is offline and denies
GPU, devices, network, credentials, Docker socket, and host access. This skill
must not change that:

- Do not add `CLAWVOICE_URL`, tokens, skill mounts, GPU devices, or network
  access to `coder`.
- Do not modify or repurpose its protected `autonomous-coding-run` installer.
- Do not install this runtime skill into `coder` merely because the host can
  reach the service.
- Host environment injection does not automatically enter a sandbox.

The intended first caller is the authorized host `main` agent, after reviewing
its effective allowlists and tool policy.

## Version-aware rollout

1. **Inventory (read-only).** On the authorized host, record
   `openclaw --version`, the active state/workspace dirs, `openclaw skills list`
   and `openclaw skills check`, the effective `agents.entries.main` skill
   allowlist, and whether `main` has `exec` and outbound media. Do not edit
   config during this step.
2. **Stage.** Install the `clawvoice` client wheel into the execution
   environment that will actually call it. Put the skill in a private workspace
   skill directory (for the default single-admin setup,
   `<workspace>/skills/chatterbox-voice/`). Stage the service token in the
   operator's existing secret flow, mode 0600.
3. **Readiness.** `clawvoice doctor --json` must show the service ready and the
   real model. `openclaw skills check` must show this skill ready for `main`.
   Start a new session or run the version-appropriate refresh so the catalog is
   rebuilt.
4. **Canary.** In a new private session with `main`, ask for a short speech
   artifact from a test voice; verify job polling, artifact download, and
   channel attachment. Then ask for the enrollment-link flow. Do not use a real
   person's voice for the canary.
5. **Rollback.** Remove only the staged skill entry and its scoped client
   environment; preserve voice data and service containers. No OpenClaw
   restart unless its version requires one.

Use supported version-specific mechanisms for any config edit. Do not overwrite
`openclaw.json` wholesale or copy Codex-specific UI metadata into the skill.
If version detection is ambiguous, stop at stage 2 and ask the owner.

## Sandbox path

If the intended calling agent is sandboxed:

- install the lightweight client **inside** the sandbox image or mount a
  trusted, executable client bundle;
- provide a private route to the service (container `localhost` is not host
  `localhost`) and a scoped token via the sandbox's documented mount/secret
  mechanism;
- make an agent-writable artifact staging directory that the channel sender can
  read;
- test `clawvoice doctor`, a job, and attachment from the real sandbox.

Do not expose the Docker socket, use host networking for convenience, or grant
the sandbox GPU access. The inference worker owns the GPU.

## Not a provider replacement

This skill is a private workflow around a local HTTP service and CLI. It does
not automatically replace OpenClaw's built-in `tts` provider, Talk mode, or
voice-call providers. Each would require a version-specific provider contract
and separate authorization. No such integration is claimed here.

## Install artifacts

- Skill: `skills/chatterbox-voice/`
- Client wheel: built by `deploy/release.sh` as `clawvoice-<version>-py3-none-any.whl`
- CLI: `clawvoice`
- Service: `deploy/compose.yaml` plus the GPU/CPU overlay
- The integration plan above is the only supported path documented by this
  build; there is no live-install attestation.
