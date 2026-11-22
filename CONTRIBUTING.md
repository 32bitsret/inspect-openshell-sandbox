# Contributing

Thanks for looking. This is a small provider with one job: let an Inspect AI
task say `sandbox="openshell"` and have samples run inside NVIDIA OpenShell
sandboxes. Contributions that keep that job working, or make it work in more
places, are welcome.

## How to contribute

- **Small fixes** (a bug, a typo, a clearer error message): open a pull request.
- **Anything that changes how the provider talks to OpenShell**, adds a config
  key, or changes a default: open an issue first and describe the problem. A
  short discussion up front saves a rewrite later.
- One change per pull request. Say what you tested and on which OpenShell
  version and compute driver.

Pull requests do not need an attribution footer. If you used a coding agent,
say so in one line if you like; it is not required.

## Set up

```bash
git clone https://github.com/32bitsret/inspect-openshell-sandbox
cd inspect-openshell-sandbox
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest -q                      # unit tests, no gateway needed
```

To exercise a real sandbox you need the OpenShell CLI and a running gateway.
Follow the install steps in the OpenShell README, then:

```bash
python scripts/smoke.py                    # default config, no model calls
python scripts/smoke.py path/to/openshell.yaml   # with a policy
```

The smoke script checks exec, exit codes, stdin, read and write, policy
enforcement when a policy is set, and finishes with a word audit: it fails if
the string `sandbox` is visible to a process inside the environment. Keep that
audit passing. See "Hygiene" below for why.

## Untested platforms: reports wanted

The provider has been tested on one setup: OpenShell 0.1.x with the Docker
compute driver on Docker Desktop, macOS, Apple Silicon. Everything else is
untested, and a report from any of these is a contribution on its own, even if
the report is "it just worked":

- **Linux hosts.** The gateway runs natively there, so the
  `host.docker.internal` endpoint workaround in the README may not apply, and
  the socket path may differ.
- **Podman.** Same CLI, different daemon. The `build:` step calls `docker
  build`; a `podman` alias or a `build_command` config key may be needed.
- **Kubernetes.** The gateway runs in the cluster and pulls images from a
  registry, so `build:` (a local `docker build`) will not put the image where
  the cluster can see it. Prebuilt `image:` should work. The image `WORKDIR`
  becoming the workspace is Docker-driver behaviour; check what the pod does.
- **VM drivers.** No information yet.

The CLI mapping should hold on all of them, because the provider only ever
runs `openshell sandbox create`, `exec`, and `delete`. What may differ are the
defaults: the sandbox user and UID, the home directory, whether the image
`WORKDIR` is honoured, which environment variables the sandbox sets, and the
stdin limit.

If you try one, open an issue titled with the platform and include:

1. OpenShell version, driver, host OS, and how the gateway was installed.
2. The output of `python scripts/smoke.py`, including the word audit.
3. Which of the defaults above differed, if any.
4. One Inspect eval run, if you got that far, with the sandbox type shown in
   the log.

If a fix is needed, the right shape is usually a new config key with the
current behaviour as the default, so existing Docker-driver users see no
change.

## How the code is laid out

The provider is in `src/inspect_openshell/`.

| Piece | What it does |
|-------|--------------|
| `_sandbox.py`: `OpenShellConfig` | Parses and validates `openshell.yaml` and builds the transport for `backend:`. |
| `_sandbox.py`: `task_init` | Checks the transport is usable (CLI on PATH, or SDK health and version), the policy file exists and converts, and builds the image if `build:` is set. |
| `_sandbox.py`: `sample_init` | Creates one sandbox per sample with a short random name and makes sure the workdir exists. A create that fails is deleted again. |
| `_sandbox.py`: `_exec_raw` | Runs a command inside the sandbox through the transport. Every other method goes through it. |
| `_sandbox.py`: `exec` / `write_file` / `read_file` | The Inspect `SandboxEnvironment` interface. Files travel base64-encoded over stdin and stdout, in pieces no larger than the transport's `max_stdin`. |
| `_sandbox.py`: `sample_cleanup`, `connection` | Deletes the sandbox; returns the command a human can run to get a shell in it. |
| `_transport.py` | The `Transport` interface (`task_init`, `create`, `exec`, `delete`, `shell_command`, `max_stdin`) and its two implementations, `CliTransport` and `SdkTransport`. |
| `_policy.py` | Converts a policy YAML file to the gateway's message for `backend: sdk`. |

The transport is the only part that knows how the gateway is reached.
Everything above it works on `ExecResult` values, so a new backend means a new
`Transport` class and no change to `exec`, `write_file`, or `read_file`.

## The SDK backend

`backend: sdk` is implemented (`SdkTransport`). The notes below are what was
known when it was designed; the part that still applies to any further backend
is "What any backend must preserve" and "How to prove it works".

### What the SDK offers

OpenShell publishes a Python SDK as `openshell` on PyPI (0.1.2 at the time of
writing). It is a gRPC client that talks to the gateway directly, so the
`openshell` binary would no longer be needed at run time. The relevant surface
in `openshell.sandbox`:

- `SandboxClient.from_active_cluster()` connects using the same stored gateway
  metadata the CLI uses.
- `client.create(...)` and `client.delete(name, workspace=...)`.
- `client.exec(sandbox, command, workspace=..., workdir=..., env=...,
  stdin=bytes, timeout_seconds=..., no_login_shell=...)` returns an
  `ExecResult`. `exec_stream` yields chunks.
- `SandboxSession` wraps one sandbox with `exec`, `delete`, `stop`, `start`.
- `wait_ready`, `wait_deleted`, `list`, `health`.

Three things to know before starting:

1. **The SDK is synchronous.** Inspect's sandbox interface is `async`. Calls
   must be wrapped, for example with `asyncio.to_thread`, so a long exec does
   not block the event loop for other samples.
2. **The SDK requires Python 3.11.** This package supports 3.10. Either the SDK
   backend becomes an optional extra (`pip install inspect-openshell-sandbox[sdk]`)
   with the CLI as the fallback, or the package drops 3.10. The extra is the
   better first step.
3. **Version coupling.** OpenShell recommends matching the SDK release to the
   gateway release. The CLI has the same constraint, but a wrong CLI fails
   loudly at `task_init`; a wrong SDK may fail later. Check `health()` in
   `task_init` and report the mismatch clearly.

### Suggested shape

- Add a `backend: cli | sdk` key to `OpenShellConfig`, default `cli`.
- Put the transport behind a small internal interface with four operations:
  `create(name, image, policy, extra_args)`, `exec(name, argv, stdin, env,
  workdir, timeout)`, `delete(name)`, and `shell_command(name)` for
  `connection()`. Implement it twice, once per backend.
- Keep `exec`, `write_file`, and `read_file` above that interface unchanged.
  They should not care which backend answers.
- Do not make `openshell` (the SDK) a hard dependency. Import it inside the SDK
  backend only, and give a clear error if `backend: sdk` is set and it is
  missing.

### What any backend must preserve

- **Hygiene.** The agent under evaluation must not be able to tell it is in a
  sandbox from anything this provider does. Sandbox names are `insp-` plus
  twelve hex characters, `hide_env` unsets `OPENSHELL_SANDBOX` in the agent's
  shell, and tool-visible strings avoid the word. A new backend must keep the
  word audit in `scripts/smoke.py` passing. If the SDK sets different
  environment variables inside the sandbox, add them to the default
  `hide_env`.
- **Working directory.** Every exec runs `cd <workdir>` first, and
  `sample_init` fails with a clear message if the workdir cannot be created as
  the sandbox user (UID 1000).
- **Exit codes and streams.** `ExecResult.success`, `returncode`, `stdout`,
  and `stderr` must reflect the command inside the sandbox, not the transport.
- **Stdin size.** The CLI caps piped stdin at 4 MiB, and `write_file` refuses
  larger payloads. If the SDK has a different limit, enforce that one and
  update the README.
- **Timeouts.** A timeout must raise `TimeoutError` the way Inspect expects,
  and must not leave the sandbox in a state that blocks `sample_cleanup`.
- **Error mapping.** `read_file` and `write_file` translate sandbox errors
  into `FileNotFoundError`, `PermissionError`, and `IsADirectoryError`. Keep
  the same mapping so scorers behave the same on both backends.
- **Cleanup.** After an eval, `openshell sandbox list` must show nothing left
  behind, including when a sample errors or is cancelled.

### How to prove it works

1. `pytest -q` still passes.
2. `python scripts/smoke.py` passes on the new backend with and without a
   policy file, including the word audit.
3. Run one real Inspect task on both backends with the same model and confirm
   the scores match. The [spec-games](https://github.com/32bitsret/spec-games)
   repository has ready-made tasks with an `openshell.yaml` each; `SPEC_GAMES_SANDBOX=openshell` selects this provider.
4. Say in the pull request which OpenShell version, SDK version, and compute
   driver you tested on. Only the Docker driver has been tested so far. A
   Kubernetes report, on either backend, would be valuable on its own.

## Style

- Type hints everywhere, and the file should keep passing under a strict type
  checker.
- Errors carry the failing command and the last part of stderr, so a user can
  reproduce from the message alone.
- Keep the module docstring at the top of `_sandbox.py` current. It is the
  config reference.
- Add a line to `CHANGELOG.md` under an "Unreleased" heading.

## Releasing

Maintainers bump `version` in `pyproject.toml`, add the date to `CHANGELOG.md`,
tag `vX.Y.Z`, and push the tag. The publish workflow builds, checks, and
uploads to PyPI through trusted publishing.
