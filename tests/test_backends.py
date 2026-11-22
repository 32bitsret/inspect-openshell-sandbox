"""Backend and transport tests. No gateway and no OpenShell SDK needed."""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from inspect_ai.util import ExecResult

from inspect_openshell import _transport
from inspect_openshell._sandbox import OpenShellConfig, OpenShellSandboxEnvironment
from inspect_openshell._transport import CliTransport, SdkTransport


# ---- config ---------------------------------------------------------------


def test_backend_default_and_validation():
    assert OpenShellConfig.load(None).backend == "cli"
    with pytest.raises(ValueError, match="backend must be one of"):
        OpenShellConfig(backend="grpc").validate()


def test_cli_only_and_sdk_only_keys_are_rejected(tmp_path: Path):
    (tmp_path / "a.yaml").write_text("backend: sdk\ncreate_args: ['--detach']\n")
    with pytest.raises(ValueError, match="apply only to backend: cli"):
        OpenShellConfig.load(str(tmp_path / "a.yaml"))
    (tmp_path / "b.yaml").write_text("endpoint: gw:8080\n")
    with pytest.raises(ValueError, match="apply only to backend: sdk"):
        OpenShellConfig.load(str(tmp_path / "b.yaml"))


def test_tls_needs_endpoint_and_all_three_files(tmp_path: Path):
    (tmp_path / "a.yaml").write_text("backend: sdk\ntls: {ca: a, cert: b, key: c}\n")
    with pytest.raises(ValueError, match="needs `endpoint`"):
        OpenShellConfig.load(str(tmp_path / "a.yaml"))
    (tmp_path / "b.yaml").write_text("backend: sdk\nendpoint: gw:8080\ntls: {ca: a}\n")
    with pytest.raises(ValueError, match="exactly ca, cert and key"):
        OpenShellConfig.load(str(tmp_path / "b.yaml"))


def test_tls_paths_resolve_next_to_the_config(tmp_path: Path):
    (tmp_path / "openshell.yaml").write_text(
        "backend: sdk\nendpoint: gw:8080\ntls: {ca: ca.crt, cert: /abs/tls.crt, key: tls.key}\n"
    )
    cfg = OpenShellConfig.load(str(tmp_path / "openshell.yaml"))
    assert cfg.tls == {
        "ca": str((tmp_path / "ca.crt").resolve()),
        "cert": "/abs/tls.crt",
        "key": str((tmp_path / "tls.key").resolve()),
    }
    transport = cfg.transport()
    assert isinstance(transport, SdkTransport) and transport.endpoint == "gw:8080"


def test_transport_selection():
    assert isinstance(OpenShellConfig().transport(), CliTransport)
    assert isinstance(OpenShellConfig(backend="sdk").transport(), SdkTransport)


# ---- CLI transport: command lines are unchanged by default ----------------


@pytest.fixture
def cli_calls(monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    async def fake_run(args, *, input=None, timeout=None):
        calls.append((args, {"input": input, "timeout": timeout}))
        return ExecResult(success=True, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_transport, "_run", fake_run)
    return calls


async def test_cli_default_command_lines(cli_calls):
    t = CliTransport(create_args=["--detach"], exec_args=["--tty"])
    await t.create("insp-1", "img", "/p.yaml")
    await t.exec("insp-1", ["bash", "-c", "x"], stdin=b"in", env={"A": "1"}, timeout=5)
    await t.delete("insp-1")
    assert cli_calls[0][0] == [
        "openshell", "sandbox", "create", "--name", "insp-1", "--from", "img", "--policy", "/p.yaml", "--detach",
    ]
    assert cli_calls[1][0] == [
        "openshell", "sandbox", "exec", "-n", "insp-1", "--no-login-shell", "--env", "A=1", "--tty", "--", "bash", "-c", "x",
    ]
    assert cli_calls[1][1] == {"input": b"in", "timeout": 5}
    assert cli_calls[2][0] == ["openshell", "sandbox", "delete", "insp-1"]
    assert t.shell_command("insp-1") == "openshell sandbox exec -n insp-1 --tty -- /bin/bash"


async def test_cli_gateway_and_workspace_are_passed(cli_calls):
    t = CliTransport(gateway="gw", workspace="ws")
    await t.create("n", "img", None)
    await t.exec("n", ["true"], stdin=None, env=None, timeout=None)
    await t.delete("n")
    for args, _ in cli_calls:
        assert "--gateway" in args and "gw" in args and "--workspace" in args and "ws" in args
    assert "--gateway gw --workspace ws --tty" in t.shell_command("n")


# ---- SDK transport against a fake client ----------------------------------


class FakeClient:
    def __init__(self, exec_result=None, sleep=0.0):
        self.exec_result = exec_result or SimpleNamespace(exit_code=0, stdout="", stderr="")
        self.sleep = sleep
        self.calls: list[tuple[str, tuple, dict]] = []
        self.sandbox_id: str | None = "sb-1"

    def exec(self, *args, **kwargs):
        self.calls.append(("exec", args, kwargs))
        time.sleep(self.sleep)
        return self.exec_result

    def delete(self, *args, **kwargs):
        self.calls.append(("delete", args, kwargs))
        return SimpleNamespace(sandbox_id=self.sandbox_id)

    def wait_deleted(self, *args, **kwargs):
        self.calls.append(("wait_deleted", args, kwargs))

    def health(self):
        return SimpleNamespace(version="0.1.9")


@pytest.fixture
def sdk(monkeypatch):
    client = FakeClient()
    t = SdkTransport(workspace="ws", max_exec_seconds=111)
    monkeypatch.setattr(SdkTransport, "_client", lambda self: client)
    return t, client


async def test_sdk_exec_maps_result_and_arguments(sdk):
    t, client = sdk
    client.exec_result = SimpleNamespace(exit_code=7, stdout="out", stderr="err")
    r = await t.exec("n", ["bash", "-c", "x"], stdin=b"in", env={"A": "1"}, timeout=None)
    assert (r.success, r.returncode, r.stdout, r.stderr) == (False, 7, "out", "err")
    _, args, kwargs = client.calls[0]
    assert args == ("n", ["bash", "-c", "x"])
    assert kwargs["workspace"] == "ws" and kwargs["stdin"] == b"in" and kwargs["env"] == {"A": "1"}
    assert kwargs["no_login_shell"] is True
    assert kwargs["timeout_seconds"] == 111  # no timeout given: the ceiling applies


async def test_sdk_timeout_is_rounded_up_and_raises(monkeypatch):
    client = FakeClient(exec_result=SimpleNamespace(exit_code=124, stdout="", stderr=""), sleep=1.1)
    monkeypatch.setattr(SdkTransport, "_client", lambda self: client)
    t = SdkTransport()
    with pytest.raises(TimeoutError):
        await t.exec("n", ["sleep", "9"], stdin=None, env=None, timeout=1)
    assert client.calls[0][2]["timeout_seconds"] == 1


async def test_sdk_exit_124_before_the_timeout_is_a_real_result(sdk):
    t, client = sdk
    client.exec_result = SimpleNamespace(exit_code=124, stdout="", stderr="")
    r = await t.exec("n", ["sh", "-c", "exit 124"], stdin=None, env=None, timeout=100)
    assert r.returncode == 124 and not r.success


async def test_sdk_delete_waits_for_the_deletion(sdk):
    t, client = sdk
    await t.delete("n")
    names = [c[0] for c in client.calls]
    assert names == ["delete", "wait_deleted"]
    assert client.calls[1][2]["expected_sandbox_id"] == "sb-1"
    assert client.calls[0][2]["allow_missing"] is True


async def test_sdk_delete_of_a_missing_sandbox_does_not_wait(sdk):
    t, client = sdk
    client.sandbox_id = None
    await t.delete("n")
    assert [c[0] for c in client.calls] == ["delete"]


async def test_sdk_version_mismatch_warns(sdk, monkeypatch, caplog):
    t, _ = sdk
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.1.8")
    with caplog.at_level(logging.WARNING, logger="inspect_openshell._transport"):
        await t.task_init()
    assert "gateway is 0.1.9 but the openshell SDK is 0.1.8" in caplog.text


# ---- provider on top of a transport ---------------------------------------


class FakeTransport:
    name = "fake"

    def __init__(self, max_stdin=8, fail_create=False):
        self.max_stdin = max_stdin
        self.fail_create = fail_create
        self.execs: list[tuple[list[str], bytes | None]] = []
        self.deleted: list[str] = []

    async def task_init(self):
        pass

    async def create(self, name, image, policy):
        if self.fail_create:
            raise RuntimeError("create failed")

    async def exec(self, name, argv, *, stdin, env, timeout):
        self.execs.append((argv, stdin))
        return ExecResult(success=True, returncode=0, stdout="", stderr="")

    async def delete(self, name):
        self.deleted.append(name)

    def shell_command(self, name):
        return f"shell {name}"


def _env(transport) -> OpenShellSandboxEnvironment:
    return OpenShellSandboxEnvironment("n", OpenShellConfig(workdir="/w"), transport)


@pytest.mark.parametrize("size", [0, 1, 5, 8, 9, 24, 100])
async def test_write_file_sends_pieces_that_reassemble(size):
    t = FakeTransport(max_stdin=8)
    data = bytes(range(256))[:size] if size <= 256 else b""
    await _env(t).write_file("f.bin", data)
    pieces = [stdin for _, stdin in t.execs]
    assert all(len(p) <= 8 and len(p) % 4 == 0 for p in pieces)
    assert b"".join(base64.b64decode(p) for p in pieces) == data
    scripts = [argv[2] for argv, _ in t.execs]
    assert "base64 -d > /w/f.bin" in scripts[0]
    assert all("base64 -d >> /w/f.bin" in s for s in scripts[1:])


async def test_write_file_larger_than_one_exec_is_allowed_on_the_cli_limit():
    t = FakeTransport(max_stdin=4 * 1024 * 1024)
    data = b"x" * (5 * 1024 * 1024)  # the old code refused anything over 3 MiB
    await _env(t).write_file("big", data)
    assert len(t.execs) == 2
    assert b"".join(base64.b64decode(s) for _, s in t.execs) == data


async def test_exec_stdin_over_the_transport_limit_is_refused():
    t = FakeTransport(max_stdin=8)
    with pytest.raises(ValueError, match="limited to 8 bytes on backend: fake"):
        await _env(t).exec(["cat"], input="x" * 9)


async def test_failed_create_deletes_the_half_built_sandbox(monkeypatch):
    t = FakeTransport(fail_create=True)
    monkeypatch.setattr(OpenShellConfig, "transport", lambda self: t)
    with pytest.raises(RuntimeError, match="create failed"):
        await OpenShellSandboxEnvironment.sample_init("task", None, {})
    assert len(t.deleted) == 1 and t.deleted[0].startswith("insp-") and len(t.deleted[0]) == 17


async def test_sample_cleanup_deletes_through_the_transport():
    t = FakeTransport()
    env = _env(t)
    await OpenShellSandboxEnvironment.sample_cleanup("task", None, {"default": env}, False)
    assert t.deleted == ["n"]


async def test_connection_uses_the_transport_command():
    conn = await _env(FakeTransport()).connection()
    assert conn.command == "shell n"
