"""Inspect sandbox provider backed by NVIDIA OpenShell.

Each Inspect sample gets its own OpenShell sandbox, created from an image and
governed by an OpenShell policy. Commands run through ``openshell sandbox exec``
and files move through the same channel, so the kernel-level policy applies to
everything the agent does, including the scorer's reads.

Config (``openshell.yaml`` beside the task file, all keys optional):

    image: python:3.12-slim   # passed to `openshell sandbox create --from`
    build: images/python      # optional: docker build this dir (relative to the
                              # config file) and tag it as `image` at task_init
    workdir: /sandbox         # where sample files land and where exec runs.
                              # Must exist in the image and be writable by the
                              # sandbox user (UID 1000); /sandbox always is. On
                              # the Docker driver the image's WORKDIR becomes the
                              # workspace, so set both to the same path.
    policy: policy.yaml       # passed to `openshell sandbox create --policy`
    create_args: []           # extra flags for `openshell sandbox create`
    exec_args: []             # extra flags for every `openshell sandbox exec`
    hide_env: [OPENSHELL_SANDBOX]  # unset these in the agent's shell so the
                              # environment does not announce itself; [] to keep

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

CLI = "openshell"
STDIN_LIMIT = 4 * 1024 * 1024  # the CLI caps piped stdin at 4 MiB


@dataclass
class OpenShellConfig:
    image: str = "python:3.12-slim"
    build: str | None = None
    workdir: str = "/sandbox"
    policy: str | None = None
    create_args: list[str] = field(default_factory=list)
    exec_args: list[str] = field(default_factory=list)
    hide_env: list[str] = field(default_factory=lambda: ["OPENSHELL_SANDBOX"])

    @classmethod
    def load(cls, config: str | None) -> "OpenShellConfig":
        if config is None:
            return cls()
        path = Path(config)
        data = yaml.safe_load(path.read_text()) or {}
        for key in ("policy", "build"):
            if data.get(key) and not os.path.isabs(data[key]):
                data[key] = str((path.parent / data[key]).resolve())
        return cls(**data)


async def _run(args: list[str], *, input: bytes | None = None, timeout: float | None = None) -> ExecResult[str]:
    """Run a local `openshell ...` command and capture its result."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"timed out after {timeout}s: {' '.join(args[:6])} ...")
    return ExecResult(
        success=proc.returncode == 0,
        returncode=proc.returncode or 0,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
    )


@sandboxenv(name="openshell")
class OpenShellSandboxEnvironment(SandboxEnvironment):
    """One OpenShell sandbox per sample."""

    def __init__(self, name: str, config: OpenShellConfig) -> None:
        super().__init__()
        self.name = name
        self.config = config

    # ---- lifecycle -------------------------------------------------------

    @classmethod
    def config_files(cls) -> list[str]:
        return ["openshell.yaml", "openshell.yml"]

    @classmethod
    def default_concurrency(cls) -> int | None:
        return 4  # sandbox creation is heavier than docker; tune per gateway

    @classmethod
    async def task_init(cls, task_name: str, config: str | None) -> None:
        if shutil.which(CLI) is None:
            raise RuntimeError(
                "the `openshell` CLI is not on PATH; install it from "
                "https://github.com/NVIDIA/OpenShell and start a gateway"
            )
        cfg = OpenShellConfig.load(config)
        if cfg.policy and not Path(cfg.policy).exists():
            raise FileNotFoundError(f"openshell policy file not found: {cfg.policy}")
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
        # The gateway caps sandbox names at 19 characters, so the task name
        # cannot be part of it; "insp-" + 12 hex = 17.
        name = f"insp-{uuid.uuid4().hex[:12]}"
        args = [CLI, "sandbox", "create", "--name", name, "--from", cfg.image]
        if cfg.policy:
            args += ["--policy", cfg.policy]
        args += cfg.create_args
        result = await _run(args, timeout=600)
        if not result.success:
            raise RuntimeError(f"openshell sandbox create failed for {name}:\n{result.stderr or result.stdout}")
        env = cls(name, cfg)
        made = await env._exec_raw(["mkdir", "-p", cfg.workdir], timeout=60)
        if not made.success:
            await _run([CLI, "sandbox", "delete", name], timeout=120)
            raise RuntimeError(
                f"cannot create workdir {cfg.workdir!r} in the sandbox (runs as an unprivileged user; "
                f"/sandbox is writable): {made.stderr.strip()}"
            )
        return {"default": env}

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
                await _run([CLI, "sandbox", "delete", env.name], timeout=300)

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
        args = [CLI, "sandbox", "exec", "-n", self.name, "--no-login-shell"]
        for k, v in (env or {}).items():
            args += ["--env", f"{k}={v}"]
        args += self.config.exec_args
        args += ["--", *cmd]
        return await _run(args, input=input, timeout=timeout)

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
        if data is not None and len(data) > STDIN_LIMIT:
            raise ValueError("openshell sandbox: piped stdin is limited to 4 MiB")
        return await self._exec_raw(["bash", "-c", script], input=data, env=env, timeout=timeout)

    # ---- files -----------------------------------------------------------

    async def write_file(self, file: str, contents: str | bytes) -> None:
        path = self._resolve(file)
        data = contents.encode() if isinstance(contents, str) else contents
        # base64 over stdin keeps binary safe and avoids any upload path semantics.
        b64 = base64.b64encode(data)
        if len(b64) > STDIN_LIMIT:
            raise ValueError("openshell sandbox: write_file over 3 MiB is not supported yet; use upload")
        script = f"mkdir -p {shlex.quote(os.path.dirname(path) or '/')} && base64 -d > {shlex.quote(path)}"
        result = await self._exec_raw(["bash", "-c", script], input=b64, timeout=120)
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
            command=f"{CLI} sandbox exec -n {self.name} --tty -- /bin/bash",
        )
