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

# optional: the SDK backend (Python 3.11+), see "Backends" below
uv pip install 'inspect-openshell-sandbox[sdk]'
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

## Backends

The provider talks to the gateway one of two ways, chosen with `backend:` in
`openshell.yaml`.

| | `backend: cli` (default) | `backend: sdk` |
|---|---|---|
| Transport | runs the `openshell` binary | OpenShell Python SDK, gRPC |
| Needs at run time | the `openshell` CLI and a registered gateway | the `sdk` extra (Python 3.11+); a registered gateway **or** `endpoint` + `tls` |
| `create_args`, `exec_args` | yes | no (CLI flags) |
| `policy:` file | passed to OpenShell unchanged | converted to the gateway's message (see below) |
| Timeout of a command | the local `openshell` process is killed | enforced by the gateway; raises `TimeoutError` |
| One piped stdin | up to 4 MiB | up to about 960 KiB (a gRPC message limit) |

Both backends give the same results for the same task: the smoke test and a
real Inspect run pass on each. `write_file` sends large files in several pieces,
so neither limit caps the size of a file.

Keys that apply to both: `gateway` (the name of a registered gateway; default
is the active one) and `workspace`.

```yaml
backend: sdk
endpoint: openshell.openshell.svc.cluster.local:8080   # instead of a registered gateway
tls: {ca: ca.crt, cert: tls.crt, key: tls.key}         # mTLS files, relative to this file
max_exec_seconds: 3600                                  # ceiling for a command with no timeout
```

Kubernetes keys, `backend: sdk` only (the CLI has no flag for them; for the CLI
use `create_args`):

```yaml
runtime_class: kata            # Kubernetes RuntimeClass for the sandbox pod
resources:                     # Kubernetes requests / limits for the sandbox container
  requests: {cpu: 2, memory: 3Gi}
  limits: {cpu: 2, memory: 3Gi}
```

The gateway does not take any RuntimeClass the caller names. With the Kubernetes
driver's `resource_admission` (on in the Helm chart), a cluster operator must
label the RuntimeClass first, with the default settings
`kubectl label runtimeclass kata openshell.ai/sandbox-attachable=true`.
Without the label `sample_init` fails with the gateway's message plus that
command. `resources` values are sent as strings, as Kubernetes quantities are
(YAML `cpu: 2` is fine).

`endpoint` and `tls` are for places where `openshell gateway add` was never run,
such as a pod. Only mTLS (or a plain endpoint) is supported there; for an OIDC
gateway register it with the CLI and leave `endpoint` unset.

With `backend: sdk` the policy file is converted by this package, because the
SDK has no loader for it. The conversion handles the documented schema
(`filesystem_policy`, `landlock`, `process`, `network_policies`, including the
lower-case enum spellings) and rejects anything it does not recognise with the
field path, so a policy is never silently weaker than the file. If a file uses
something it cannot express, use `backend: cli`. The SDK and the gateway should
be the same OpenShell release; `task_init` logs a warning when they differ.

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
- `OPENSHELL_SANDBOX` is set inside every sandbox. The provider unsets it in the agent's shell by default (`hide_env`) so the agent under test does not learn it is sandboxed. `HOME` is the workdir. OpenShell also sets six CA-bundle variables (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`, `NODE_EXTRA_CA_CERTS`, `DENO_CERT`) whose values name `/run/openshell-supervisor-ca`; they are needed for HTTPS through the supervisor, so they are left alone by default. A task with no network can add them to `hide_env`. With a policy that omits `/run` and `/proc`, nothing else on the filesystem or in the process list names OpenShell.
- Per-command `user` is not supported.
- One piped `stdin` is limited by the backend (4 MiB for `cli`, about 960 KiB
  for `sdk`); `exec` refuses more. `write_file` has no size cap: it sends the
  file in base64 pieces that each fit one exec (about 40 MiB took 2 to 4 s in
  the one test). It is still base64 through exec, so a very large file is slow;
  `openshell sandbox upload` is not wired in.
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

- **More of the SDK backend.** `backend: sdk` exists (see [Backends](#backends));
  what is not there yet is OIDC for `endpoint`, sandbox environment variables
  and `openshell sandbox upload`-style transfers.
  [CONTRIBUTING.md](CONTRIBUTING.md) lists the behaviours any backend must keep.
- **Other platforms.** Linux hosts and the Podman, Kubernetes and VM drivers
  are untested. The CLI mapping should hold but defaults may differ. A report
  from any of them, with the smoke test output, is worth an issue on its own.

Fork it, try it, open an issue or a pull request. Small fixes can go straight
to a PR; for a backend or a new config key, an issue first saves a rewrite.
If a fork goes somewhere this repo does not, say so and it gets linked from
here.
