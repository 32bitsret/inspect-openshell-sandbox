# inspect-openshell-sandbox

An [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) sandbox environment
for [Inspect AI](https://inspect.aisi.org.uk/). Each sample runs in its own
OpenShell sandbox: kernel-enforced file, syscall and network policy, with
credentials that only work at approved endpoints. Inspect tasks need no change
beyond `sandbox="openshell"`.

## Status

0.1.0, alpha. Tested with OpenShell 0.1.x on Docker Desktop (macOS, Apple
Silicon) with the `grpc_endpoint` workaround for host networking, and Inspect
AI 0.3.266. Linux hosts and the Podman, Kubernetes and VM drivers are
untested; the CLI mapping should hold but defaults may differ. If you run one
of those and it works, or does not, an issue saying so is a contribution. See
[Contributing](#contributing).

## Install

```bash
# OpenShell CLI + local gateway (see the OpenShell README for options)
curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh

# this provider, into the venv that runs inspect
uv pip install inspect-openshell-sandbox        # from PyPI
# or, from a checkout of this repo:
uv pip install -e .
```

## Use

Put an `openshell.yaml` next to the task file:

```yaml
image: spec-games-openshell:python   # openshell sandbox create --from
build: ./images/python               # optional: docker build this dir + tag as `image` at task_init
workdir: /space                      # must exist in the image, writable by UID 1000
# policy: policy.yaml               # optional openshell policy for this task
```

Without `image:` and `workdir:` the defaults are `python:3.12-slim` and
`/sandbox`. Any other workdir must be created in the image, owned by UID 1000,
and set as `WORKDIR`, which OpenShell's Docker driver adopts as the workspace.
`images/python/Dockerfile` does this for `/space`.

and in the task:

```python
Task(..., sandbox="openshell")
```

Inspect finds `openshell.yaml` automatically, the same way it finds
`compose.yaml` for docker.

## What it does

| Inspect call | OpenShell |
|---|---|
| `sample_init` | `openshell sandbox create --name insp-<12 hex> --from <image> [--policy ...]` |
| `exec(cmd, cwd, env, input, timeout)` | `openshell sandbox exec -n <name> --no-login-shell [--env K=V] -- bash -c "cd <cwd> && <cmd>"` |
| `write_file` | base64 over stdin, `base64 -d > file` |
| `read_file` | `base64 < file`, decoded locally |
| `sample_cleanup` | `openshell sandbox delete <name>` |

Files and commands all go through `exec`, so the sandbox policy governs the
scorer's reads as well as the agent's writes.

## Limits, first version

- Sandboxes run as an unprivileged user (UID 1000); only `/sandbox` and below is writable unless the image says otherwise. A `workdir` outside it fails at `sample_init` with a clear error.
- Sandbox names are capped at 19 characters by the gateway, so the task name is not part of the name.
- `OPENSHELL_SANDBOX` is set inside every sandbox. The provider unsets it in the agent's shell by default (`hide_env`) so the agent under test does not learn it is sandboxed. `HOME` is the workdir. OpenShell's own paths under `/run/openshell` remain visible.
- Per-command `user` is not supported.
- `write_file` is capped at about 3 MiB (the CLI's 4 MiB stdin limit after
  base64). Larger files should use `openshell sandbox upload`; not wired yet.
- Sandbox creation is slower than docker. `default_concurrency` is 4.
- No policy is applied unless `policy:` is set. The interesting use is a policy
  that makes the test file read-only, so a spec edit is refused by the kernel
  instead of detected afterwards.

## Smoke test without a model

```bash
python scripts/smoke.py
```

Creates a sandbox from `python:3.12-slim`, writes a file, runs a command,
reads the file back, deletes the sandbox, and prints each step.

## Origin

Built for [spec-games](https://github.com/32bitsret/spec-games), where it runs a
contradictory-spec task with the test file locked read-only by an OpenShell
policy. That task's `policy.yaml` and `Dockerfile` are a worked example.

## Contributing

This provider does one thing and does it through the OpenShell CLI. That was
the fastest way to get a working sandbox, not the final design. Two things
would make it much more useful, and both are open for anyone who wants to fork
and take them on:

- **An SDK backend.** NVIDIA ships a Python SDK (`openshell` on PyPI) that
  talks gRPC to the gateway directly. A `backend: sdk` option would remove the
  CLI from the run-time path. [CONTRIBUTING.md](CONTRIBUTING.md) has what the
  SDK exposes, the two catches (it is synchronous, and it needs Python 3.11),
  a suggested shape, and the list of behaviours a backend must keep.
- **Other platforms.** Linux hosts and the Podman, Kubernetes and VM drivers
  are untested. The CLI mapping should hold but defaults may differ. A report
  from any of them, with the smoke test output, is worth an issue on its own.

Fork it, try it, open an issue or a pull request. Small fixes can go straight
to a PR; for a backend or a new config key, an issue first saves a rewrite.
If a fork goes somewhere this repo does not, say so and it gets linked from
here.
