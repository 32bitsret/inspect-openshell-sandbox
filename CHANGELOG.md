# Changelog

## Unreleased

- `backend: sdk` (optional `sdk` extra, Python 3.11+): talks to the gateway with
  the OpenShell Python SDK instead of the `openshell` binary. `backend: cli`
  stays the default and its command lines are unchanged. New keys: `backend`,
  `gateway`, `workspace` (both backends), `endpoint`, `tls`, `max_exec_seconds`
  (SDK). The SDK backend enforces command timeouts on the gateway, can connect
  without a registered gateway (for example from a pod), and converts a `policy:`
  file itself.
- `write_file` sends a file in several pieces, so it no longer fails above
  about 3 MiB. One piped `stdin` is limited per backend (4 MiB `cli`, about
  960 KiB `sdk`).
- A sandbox whose creation fails or times out is now deleted instead of being
  left behind.
- The provider is split into `_sandbox.py`, `_transport.py` and `_policy.py`.
- `runtime_class` and `resources` keys for `backend: sdk` (Kubernetes RuntimeClass
  and requests/limits for the sandbox container). A sandbox create that the
  gateway refuses because the RuntimeClass is not labelled for OpenShell now
  says how to fix it; other create errors keep the gateway's text.

## 0.1.0 (2026-09-30)

First release.

- `sandbox="openshell"` for Inspect AI tasks, backed by the OpenShell CLI.
- One OpenShell sandbox per sample; `exec`, `read_file`, `write_file`, cleanup.
- `openshell.yaml` config: `image`, `build` (docker build at task_init),
  `workdir`, `policy`, `create_args`, `exec_args`, `hide_env`.
- Unsets `OPENSHELL_SANDBOX` in the agent's shell by default.
- Tested with OpenShell 0.1.x on Docker Desktop (macOS, Apple Silicon) and
  Inspect AI 0.3.266.
