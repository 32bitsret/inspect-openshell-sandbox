"""How the provider talks to an OpenShell gateway.

Two transports implement the same small interface:

* ``CliTransport`` runs the ``openshell`` binary on the host. This is the
  original behaviour and the default.
* ``SdkTransport`` uses the ``openshell`` Python SDK (gRPC) and needs no binary
  at run time. It is an optional extra: ``pip install inspect-openshell-sandbox[sdk]``
  (the SDK requires Python 3.11).

Everything above the transport (``exec`` wrapping, files, hygiene, error
mapping) lives in ``_sandbox.py`` and does not know which one answers.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import math
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from inspect_ai.util import ExecResult

CLI = "openshell"
log = logging.getLogger(__name__)

CLI_STDIN_LIMIT = 4 * 1024 * 1024  # the CLI caps piped stdin at 4 MiB
# The SDK sends stdin inside one gRPC message, and the gateway accepts at most
# 1 MiB per message (measured: 2 MB fails with "decoded message length too
# large ... limit is: 1048576 bytes"). Leave room for the command and env.
SDK_STDIN_LIMIT = 1024 * 1024 - 64 * 1024
DEFAULT_WORKSPACE = "default"


class Transport(Protocol):
    """The four operations the provider needs from a gateway."""

    name: str
    max_stdin: int  # largest piped stdin one exec may carry, in bytes

    async def task_init(self) -> None: ...

    async def create(self, name: str, image: str, policy: str | None) -> None: ...

    async def exec(
        self,
        name: str,
        argv: list[str],
        *,
        stdin: bytes | None,
        env: dict[str, str] | None,
        timeout: float | None,
    ) -> ExecResult[str]: ...

    async def delete(self, name: str) -> None: ...

    def shell_command(self, name: str) -> str: ...


# ---- CLI ------------------------------------------------------------------


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


@dataclass
class CliTransport:
    gateway: str | None = None
    workspace: str | None = None
    create_args: list[str] | None = None
    exec_args: list[str] | None = None
    name: str = "cli"
    max_stdin: int = CLI_STDIN_LIMIT

    def _target(self) -> list[str]:
        args: list[str] = []
        if self.gateway:
            args += ["--gateway", self.gateway]
        if self.workspace:
            args += ["--workspace", self.workspace]
        return args

    async def task_init(self) -> None:
        if shutil.which(CLI) is None:
            raise RuntimeError(
                "the `openshell` CLI is not on PATH; install it from "
                "https://github.com/NVIDIA/OpenShell and start a gateway"
            )

    async def create(self, name: str, image: str, policy: str | None) -> None:
        args = [CLI, "sandbox", "create", "--name", name, "--from", image, *self._target()]
        if policy:
            args += ["--policy", policy]
        args += self.create_args or []
        result = await _run(args, timeout=600)
        if not result.success:
            raise RuntimeError(f"openshell sandbox create failed for {name}:\n{result.stderr or result.stdout}")

    async def exec(
        self,
        name: str,
        argv: list[str],
        *,
        stdin: bytes | None,
        env: dict[str, str] | None,
        timeout: float | None,
    ) -> ExecResult[str]:
        args = [CLI, "sandbox", "exec", "-n", name, "--no-login-shell", *self._target()]
        for k, v in (env or {}).items():
            args += ["--env", f"{k}={v}"]
        args += self.exec_args or []
        args += ["--", *argv]
        return await _run(args, input=stdin, timeout=timeout)

    async def delete(self, name: str) -> None:
        await _run([CLI, "sandbox", "delete", name, *self._target()], timeout=300)

    def shell_command(self, name: str) -> str:
        target = " ".join(self._target())
        return f"{CLI} sandbox exec -n {name} {target + ' ' if target else ''}--tty -- /bin/bash"


# ---- SDK ------------------------------------------------------------------

_clients: dict[tuple[Any, ...], Any] = {}
_clients_lock = threading.Lock()


def _sdk_client(key: tuple[Any, ...], build: Any) -> Any:
    """One gRPC client per gateway for the whole process (channels are thread-safe)."""
    with _clients_lock:
        client = _clients.get(key)
        if client is None:
            client = build()
            _clients[key] = client
            atexit.register(client.close)
        return client


@dataclass
class SdkTransport:
    gateway: str | None = None
    workspace: str | None = None
    endpoint: str | None = None
    tls_ca: str | None = None
    tls_cert: str | None = None
    tls_key: str | None = None
    max_exec_seconds: int = 3600
    runtime_class: str | None = None
    resources: dict[str, dict[str, str]] | None = None
    name: str = "sdk"
    max_stdin: int = SDK_STDIN_LIMIT

    @property
    def _workspace(self) -> str:
        return self.workspace or DEFAULT_WORKSPACE

    def _client(self) -> Any:
        try:
            from openshell import SandboxClient, TlsConfig
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "backend: sdk needs the OpenShell Python SDK (Python 3.11+): "
                "pip install 'inspect-openshell-sandbox[sdk]'"
            ) from exc

        key = (self.endpoint, self.gateway, self.tls_ca, self.tls_cert, self.tls_key)

        def build() -> Any:
            if self.endpoint:
                tls = None
                if self.tls_ca or self.tls_cert or self.tls_key:
                    if not (self.tls_ca and self.tls_cert and self.tls_key):
                        raise ValueError("tls needs ca, cert and key together")
                    tls = TlsConfig(ca_path=Path(self.tls_ca), cert_path=Path(self.tls_cert), key_path=Path(self.tls_key))
                return SandboxClient(self.endpoint, tls=tls, timeout=120)
            return SandboxClient.from_active_cluster(cluster=self.gateway, timeout=120)

        return _sdk_client(key, build)

    async def task_init(self) -> None:
        from importlib.metadata import version

        client = await asyncio.to_thread(self._client)
        health = await asyncio.to_thread(client.health)
        sdk_version = version("openshell")
        if health.version != sdk_version:
            log.warning(
                "OpenShell gateway is %s but the openshell SDK is %s; OpenShell recommends matching "
                "them, and a mismatch may fail later than the CLI would.",
                health.version,
                sdk_version,
            )

    async def create(self, name: str, image: str, policy: str | None) -> None:
        from openshell._proto import openshell_pb2

        from ._policy import load_policy

        template = openshell_pb2.SandboxTemplate(image=image)
        if self.runtime_class:
            template.runtime_class_name = self.runtime_class
        if self.resources:
            template.resources.update(self.resources)
        spec = openshell_pb2.SandboxSpec(template=template)
        if policy:
            spec.policy.CopyFrom(load_policy(policy))
        client = await asyncio.to_thread(self._client)
        try:
            await asyncio.to_thread(client.create, workspace=self._workspace, name=name, spec=spec)
        except Exception as exc:
            raise RuntimeError(f"openshell sandbox create failed for {name}:\n{self._explain(exc)}") from exc
        await asyncio.to_thread(client.wait_ready, name, workspace=self._workspace, timeout_seconds=600)

    def _explain(self, exc: Exception) -> str:
        details = getattr(exc, "details", None)
        text = str(details() if callable(details) else exc)
        if "not admitted by required labels" in text:
            kind = "RuntimeClass" if self.runtime_class and f"'{self.runtime_class}'" in text else "resource"
            hint = (
                f"The gateway only accepts a {kind} that a cluster operator has labelled for it "
                "(Kubernetes driver `resource_admission`). With the default settings: "
                f"`kubectl label runtimeclass {self.runtime_class or '<name>'} openshell.ai/sandbox-attachable=true` (`oc` on OpenShift)."
            )
            return f"{text}\n{hint}"
        return text

    async def exec(
        self,
        name: str,
        argv: list[str],
        *,
        stdin: bytes | None,
        env: dict[str, str] | None,
        timeout: float | None,
    ) -> ExecResult[str]:
        client = await asyncio.to_thread(self._client)
        seconds = math.ceil(timeout) if timeout else self.max_exec_seconds
        started = time.monotonic()
        result = await asyncio.to_thread(
            client.exec,
            name,
            argv,
            workspace=self._workspace,
            env=env,
            stdin=stdin,
            timeout_seconds=seconds,
            no_login_shell=True,
        )
        # The gateway enforces the timeout and reports it as exit code 124. A command
        # that itself exits 124 sooner is a real result, not a timeout.
        if result.exit_code == 124 and time.monotonic() - started >= seconds - 1:
            raise TimeoutError(f"timed out after {seconds}s in sandbox exec")
        return ExecResult(
            success=result.exit_code == 0,
            returncode=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    async def delete(self, name: str) -> None:
        client = await asyncio.to_thread(self._client)
        deleted = await asyncio.to_thread(
            client.delete, name, workspace=self._workspace, allow_missing=True, timeout_seconds=300
        )
        # Wait, so that nothing is left behind once the eval returns.
        if deleted.sandbox_id:
            await asyncio.to_thread(
                client.wait_deleted,
                name,
                workspace=self._workspace,
                timeout_seconds=300,
                expected_sandbox_id=deleted.sandbox_id,
            )

    def shell_command(self, name: str) -> str:
        gateway = f"--gateway {self.gateway} " if self.gateway else ""
        workspace = f"--workspace {self.workspace} " if self.workspace else ""
        return f"{CLI} sandbox exec -n {name} {gateway}{workspace}--tty -- /bin/bash"
