# Sandboxed OpenClaw agents

Read this only when the agent runs in a sandbox or container.

A skill that passes readiness on the host may still be unusable in a sandbox:

- The `clawvoice` binary must be installed **inside** the sandbox image (or
  reachable by its `exec` environment), not just on the host. `doctor`'s local
  `model.ok`/`torch` fields may be false there; use the remote
  `service.ready.checks.worker.real_model` signal instead.
- Skill `env`/`apiKey` injection is host-run only; it does not reach a sandbox.
  Provide the URL and token file through the sandbox's documented environment or
  a read-only mounted secret. Do not put the service token in a skill file.
- Container `localhost` is the container, not the host. Use a private host
  address/FQDN and firewall it to the owner's network. Do not expose the Docker
  socket or disable sandboxing to make it work.
- The artifact must be downloaded to a path the sandbox and the channel sender
  can both read. A service container path is not agent-accessible.

Test from the real execution context:

```sh
clawvoice doctor --json
clawvoice voices list --json
clawvoice healthcheck
```

If those fail in the sandbox, do not claim the skill is installed for that
agent. Report the exact boundary and ask the operator to supply a scoped client
environment. The `coder` agent's offline/no-device policy is not to be changed
for this skill.
