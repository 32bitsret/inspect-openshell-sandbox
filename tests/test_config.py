"""Unit tests that need no OpenShell install."""

from pathlib import Path

from inspect_openshell._sandbox import OpenShellConfig, OpenShellSandboxEnvironment


def test_defaults():
    cfg = OpenShellConfig.load(None)
    assert cfg.image == "python:3.12-slim" and cfg.workdir == "/sandbox" and cfg.policy is None


def test_yaml_and_relative_policy(tmp_path: Path):
    (tmp_path / "policy.yaml").write_text("{}")
    (tmp_path / "openshell.yaml").write_text("image: ubuntu:24.04\nworkdir: /work\npolicy: policy.yaml\n")
    cfg = OpenShellConfig.load(str(tmp_path / "openshell.yaml"))
    assert cfg.image == "ubuntu:24.04"
    assert cfg.workdir == "/work"
    assert cfg.policy == str(tmp_path / "policy.yaml")


def test_relative_paths_resolve_to_workdir():
    env = OpenShellSandboxEnvironment("x", OpenShellConfig(workdir="/app"))
    assert env._resolve("discount.py") == "/app/discount.py"
    assert env._resolve("/etc/hosts") == "/etc/hosts"


def test_config_files_and_registration():
    assert "openshell.yaml" in OpenShellSandboxEnvironment.config_files()
    from inspect_ai.util._sandbox.registry import registry_find_sandboxenv
    assert registry_find_sandboxenv("openshell") is OpenShellSandboxEnvironment


def test_hide_env_default_and_override(tmp_path: Path):
    assert OpenShellConfig.load(None).hide_env == ["OPENSHELL_SANDBOX"]
    (tmp_path / "openshell.yaml").write_text("hide_env: []\n")
    assert OpenShellConfig.load(str(tmp_path / "openshell.yaml")).hide_env == []
