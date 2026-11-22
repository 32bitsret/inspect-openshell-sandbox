"""Inspect sandbox provider backed by NVIDIA OpenShell.

Each Inspect sample gets its own OpenShell sandbox, created from an image and
governed by an OpenShell policy. Commands run through the gateway (the
``openshell`` CLI, or the Python SDK with ``backend: sdk``) and files move
through the same channel, so the kernel-level policy applies to everything the
agent does, including the scorer's reads.

Config (``openshell.yaml`` beside the task file, all keys optional):

    image: python:3.12-slim   # passed to `openshell sandbox create --from`
    build: images/python      # optional: docker build this dir (relative to the
                              # config file) and tag it as `image` at task_init.
                              # Local docker only; on Kubernetes use a prebuilt image.
    workdir: /sandbox         # where sample files land and where exec runs.
                              # Must exist in the image and be writable by the
                              # sandbox user (UID 1000 on the Docker driver, assigned
                              # by the cluster on Kubernetes); /sandbox always is. On
                              # the Docker driver the image's WORKDIR becomes the
                              # workspace, so set both to the same path.
    policy: policy.yaml       # passed to `openshell sandbox create --policy`
    create_args: []           # extra flags for `openshell sandbox create` (cli only)
    exec_args: []             # extra flags for every `openshell sandbox exec` (cli only)
    hide_env: [OPENSHELL_SANDBOX]  # unset these in the agent's shell so the
                              # environment does not announce itself; [] to keep

    backend: cli              # cli (default): run the `openshell` binary.
                              # sdk: use the OpenShell Python SDK over gRPC; no binary
                              # needed at run time. Needs the `sdk` extra, Python 3.11+.
    gateway: null             # name of a registered gateway; default is the active one
    workspace: null           # OpenShell workspace; default is the gateway's ("default")
    endpoint: null            # sdk only: host:port of the gateway, instead of a registered
                              # gateway. Use this where no `openshell gateway add` was run
                              # (for example inside a pod).
    tls:                      # sdk only, with `endpoint`: mTLS files (relative to this file)
      ca: ca.crt
      cert: tls.crt
      key: tls.key
    max_exec_seconds: 3600    # sdk only: ceiling for a command that has no timeout of its own
    runtime_class: kata       # sdk only: Kubernetes RuntimeClass for the sandbox pod. The gateway
                              # accepts it only if an operator labelled it (see README)
    resources:                # sdk only: Kubernetes requests/limits for the sandbox container
      requests: {cpu: "2", memory: 4Gi}
      limits: {cpu: "2", memory: 4Gi}

Verified against the OpenShell CLI documentation (sandbox overview):
``create --from <image>``, ``exec -n <name> [--env K=V] [--no-login-shell] -- cmd``
with stdin piped and the command's exit status returned, ``delete <name>``.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shlex
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Union, overload

import yaml
from inspect_ai.util import (
    ExecResult,
    SandboxConnection,
    SandboxEnvironment,
    sandboxenv,
)

from ._transport import CliTransport, SdkTransport, Transport, _run

__all__ = ["OpenShellConfig", "OpenShellSandboxEnvironment"]

BACKENDS = ("cli", "sdk")


@dataclass
class OpenShellConfig:
    image: str = "python:3.12-slim"
    build: str | None = None
    workdir: str = "/sandbox"
    policy: str | None = None
    create_args: list[str] = field(default_factory=list)
    exec_args: list[str] = field(default_factory=list)
    hide_env: list[str] = field(default_factory=lambda: ["OPENSHELL_SANDBOX"])
    backend: str = "cli"
    gateway: str | None = None
    workspace: str | None = None
    endpoint: str | None = None
    tls: dict[str, str] | None = None
    max_exec_seconds: int = 3600
    runtime_class: str | None = None
    resources: dict[str, dict[str, Any]] | None = None

    @classmethod
    def load(cls, config: str | None) -> "OpenShellConfig":
        if config is None:
            return cls()
        path = Path(config)
        data = yaml.safe_load(path.read_text()) or {}
        for key in ("policy", "build"):
            if data.get(key) and not os.path.isabs(data[key]):
                data[key] = str((path.parent / data[key]).resolve())
        if data.get("tls"):
            data["tls"] = {
                k: v if os.path.isabs(v) else str((path.parent / v).resolve()) for k, v in data["tls"].items()
            }
        cfg = cls(**data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.backend not in BACKENDS:
            raise ValueError(f"openshell backend must be one of {BACKENDS}, got {self.backend!r}")
        if self.backend == "cli":
            sdk_only = [k for k in ("endpoint", "tls", "runtime_class", "resources") if getattr(self, k)]
            if sdk_only:
                raise ValueError(f"openshell config {sdk_only} apply only to backend: sdk")
        else:
            cli_only = [k for k in ("create_args", "exec_args") if getattr(self, k)]
            if cli_only:
                raise ValueError(f"openshell config {cli_only} are CLI flags and apply only to backend: cli")
        if self.resources is not None:
            bad = set(self.resources) - {"requests", "limits"}
            if bad or not all(isinstance(v, dict) for v in self.resources.values()):
                raise ValueError("openshell config `resources` takes `requests` and `limits`, each a mapping")
        if self.tls and not self.endpoint:
            raise ValueError("openshell config `tls` needs `endpoint`")
        if self.tls and set(self.tls) != {"ca", "cert", "key"}:
            raise ValueError("openshell config `tls` needs exactly ca, cert and key")

    def transport(self) -> Transport:
        if self.backend == "sdk":
            tls = self.tls or {}
            return SdkTransport(
                gateway=self.gateway,
                workspace=self.workspace,
                endpoint=self.endpoint,
                tls_ca=tls.get("ca"),
                tls_cert=tls.get("cert"),
                tls_key=tls.get("key"),
                max_exec_seconds=self.max_exec_seconds,
                runtime_class=self.runtime_class,
                # Kubernetes quantities are strings; YAML would read `cpu: 4` as a number.
                resources={k: {r: str(q) for r, q in v.items()} for k, v in (self.resources or {}).items()} or None,
            )
        return CliTransport(
            gateway=self.gateway,
            workspace=self.workspace,
            create_args=list(self.create_args),
            exec_args=list(self.exec_args),
        )


@sandboxenv(name="openshell")
class OpenShellSandboxEnvironment(SandboxEnvironment):
    """One OpenShell sandbox per sample."""

    def __init__(self, name: str, config: OpenShellConfig, transport: Transport | None = None) -> None:
        super().__init__()
        self.name = name
        self.config = config
        self.transport = transport or config.transport()

    # ---- lifecycle -------------------------------------------------------

    @classmethod
    def config_files(cls) -> list[str]:
        return ["openshell.yaml", "openshell.yml"]

    @classmethod
    def default_concurrency(cls) -> int | None:
        return 4  # sandbox creation is heavier than docker; tune per gateway

    @classmethod
    async def task_init(cls, task_name: str, config: str | None) -> None:
        cfg = OpenShellConfig.load(config)
        await cfg.transport().task_init()
        if cfg.policy and not Path(cfg.policy).exists():
            raise FileNotFoundError(f"openshell policy file not found: {cfg.policy}")
        if cfg.policy and cfg.backend == "sdk":
            from ._policy import load_policy

            load_policy(cfg.policy)  # fail now, not on the first sample
        if cfg.build:
            if not Path(cfg.build, "Dockerfile").exists():
                raise FileNotFoundError(f"no Dockerfile in openshell build dir: {cfg.build}")
            if shutil.which("docker") is None:
                raise RuntimeError("`build:` is set but the docker CLI is not on PATH")
            result = await _run(["docker", "build", "-t", cfg.image, cfg.build], timeout=1800)
            if not result.success:
                raise RuntimeError(f"docker build of {cfg.image} failed:\n{result.stderr[-2000:]}")

    @classmethod
    async def sample_init(
        cls, task_name: str, config: str | None, metadata: dict[str, str]
    ) -> dict[str, SandboxEnvironment]:
        cfg = OpenShellConfig.load(config)
        transport = cfg.transport()
        # The gateway caps sandbox names at 19 characters, so the task name
        # cannot be part of it; "insp-" + 12 hex = 17.
        name = f"insp-{uuid.uuid4().hex[:12]}"
        try:
            await transport.create(name, cfg.image, cfg.policy)
        except BaseException:
            # A create that failed or timed out can leave a half-built sandbox behind.
            await cls._delete_quietly(transport, name)
            raise
        env = cls(name, cfg, transport)
        made = await env._exec_raw(["mkdir", "-p", cfg.workdir], timeout=60)
        if not made.success:
            await cls._delete_quietly(transport, name)
            raise RuntimeError(
                f"cannot create workdir {cfg.workdir!r} in the sandbox (runs as an unprivileged user; "
                f"/sandbox is writable): {made.stderr.strip()}"
            )
        return {"default": env}

    @staticmethod
    async def _delete_quietly(transport: Transport, name: str) -> None:
        try:
            await asyncio.shield(transport.delete(name))
        except Exception:
            pass

    @classmethod
    async def sample_cleanup(
        cls,
        task_name: str,
        config: str | None,
        environments: dict[str, SandboxEnvironment],
        interrupted: bool,
    ) -> None:
        for env in environments.values():
            if isinstance(env, OpenShellSandboxEnvironment):
                await env.transport.delete(env.name)

    @classmethod
    async def task_cleanup(cls, task_name: str, config: str | None, cleanup: bool) -> None:
        return None

    # ---- exec ------------------------------------------------------------

    def _resolve(self, path: str) -> str:
        return path if os.path.isabs(path) else os.path.join(self.config.workdir, path)

    async def _exec_raw(
        self,
        cmd: list[str],
        *,
        input: bytes | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ExecResult[str]:
        return await self.transport.exec(self.name, cmd, stdin=input, env=env, timeout=timeout)

    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
        concurrency: bool = True,
    ) -> ExecResult[str]:
        if user is not None:
            raise NotImplementedError("openshell sandbox: per-command `user` is not supported")
        workdir = self._resolve(cwd) if cwd else self.config.workdir
        # Run inside a shell so cwd applies; the CLI itself already wraps in bash -c.
        # OPENSHELL_* variables cannot be overridden via --env (reserved prefix), so
        # the ones in hide_env are unset here, for the agent's process only.
        unset = f"unset {' '.join(shlex.quote(v) for v in self.config.hide_env)} && " if self.config.hide_env else ""
        script = f"{unset}cd {shlex.quote(workdir)} && {shlex.join(cmd)}"
        data = input.encode() if isinstance(input, str) else input
        limit = self.transport.max_stdin
        if data is not None and len(data) > limit:
            raise ValueError(f"openshell sandbox: piped stdin is limited to {limit} bytes on backend: {self.transport.name}")
        return await self._exec_raw(["bash", "-c", script], input=data, env=env, timeout=timeout)

    # ---- files -----------------------------------------------------------

    async def write_file(self, file: str, contents: str | bytes) -> None:
        path = self._resolve(file)
        data = contents.encode() if isinstance(contents, str) else contents
        # base64 over stdin keeps binary safe and avoids any upload path semantics. A file is
        # sent in pieces that each fit one exec: the first truncates, the rest append. A piece
        # is a multiple of four base64 characters so it decodes on its own.
        encoded = base64.b64encode(data)
        step = max(4, self.transport.max_stdin // 4 * 4)
        pieces = [encoded[i : i + step] for i in range(0, len(encoded), step)] or [b""]
        quoted = shlex.quote(path)
        for index, piece in enumerate(pieces):
            redirect = ">" if index == 0 else ">>"
            prefix = f"mkdir -p {shlex.quote(os.path.dirname(path) or '/')} && " if index == 0 else ""
            script = f"{prefix}base64 -d {redirect} {quoted}"
            result = await self._exec_raw(["bash", "-c", script], input=piece, timeout=120)
            if not result.success:
                if "Permission denied" in result.stderr:
                    raise PermissionError(f"{file}: {result.stderr.strip()}")
                if "Is a directory" in result.stderr:
                    raise IsADirectoryError(file)
                raise RuntimeError(f"write_file {file} failed: {result.stderr.strip()}")

    @overload
    async def read_file(self, file: str, text: Literal[True] = True) -> str: ...
    @overload
    async def read_file(self, file: str, text: Literal[False]) -> bytes: ...

    async def read_file(self, file: str, text: bool = True) -> Union[str, bytes]:
        path = self._resolve(file)
        result = await self._exec_raw(["bash", "-c", f"base64 < {shlex.quote(path)}"], timeout=120)
        if not result.success:
            err = result.stderr
            if "No such file" in err:
                raise FileNotFoundError(file)
            if "Is a directory" in err:
                raise IsADirectoryError(file)
            if "Permission denied" in err:
                raise PermissionError(file)
            raise RuntimeError(f"read_file {file} failed: {err.strip()}")
        data = base64.b64decode(result.stdout)
        if text:
            return data.decode("utf-8")
        return data

    # ---- interactive -----------------------------------------------------

    async def connection(self, *, user: str | None = None) -> SandboxConnection:
        return SandboxConnection(
            type="openshell",
            command=self.transport.shell_command(self.name),
        )
