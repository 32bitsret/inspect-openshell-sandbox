# Changelog

## 0.1.0 (2026-09-30)

First release.

- `sandbox="openshell"` for Inspect AI tasks, backed by the OpenShell CLI.
- One OpenShell sandbox per sample; `exec`, `read_file`, `write_file`, cleanup.
- `openshell.yaml` config: `image`, `build` (docker build at task_init),
  `workdir`, `policy`, `create_args`, `exec_args`, `hide_env`.
- Unsets `OPENSHELL_SANDBOX` in the agent's shell by default.
- Tested with OpenShell 0.1.x on Docker Desktop (macOS, Apple Silicon) and
  Inspect AI 0.3.266.
