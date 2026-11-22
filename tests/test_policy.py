"""Policy YAML to protobuf conversion for backend: sdk. Needs the OpenShell SDK (Python 3.11+)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("openshell")

from google.protobuf import json_format  # noqa: E402

from inspect_openshell._policy import load_policy  # noqa: E402


def _load(tmp_path: Path, text: str):
    path = tmp_path / "policy.yaml"
    path.write_text(text)
    return json_format.MessageToDict(load_policy(path))


def test_filesystem_policy_key_is_renamed(tmp_path: Path):
    got = _load(
        tmp_path,
        "version: 1\nfilesystem_policy:\n  include_workdir: false\n  read_only: [/usr]\n  read_write: [/tmp]\n",
    )
    assert got == {"version": 1, "filesystem": {"readOnly": ["/usr"], "readWrite": ["/tmp"]}}


def test_network_rule_enums_are_normalised(tmp_path: Path):
    got = _load(
        tmp_path,
        """
version: 1
network_policies:
  pypi:
    endpoints:
      - {host: pypi.org, port: 443, protocol: rest, enforcement: enforce, access: read-only}
    binaries:
      - {path: /usr/local/bin/python}
""",
    )
    endpoint = got["networkPolicies"]["pypi"]["endpoints"][0]
    assert endpoint["enforcement"] == "NETWORK_ENFORCEMENT_MODE_ENFORCE"
    assert endpoint["access"] == "NETWORK_ACCESS_PRESET_READ_ONLY"
    assert got["networkPolicies"]["pypi"]["binaries"] == [{"path": "/usr/local/bin/python"}]


def test_landlock_and_process_pass_through(tmp_path: Path):
    got = _load(tmp_path, "version: 1\nlandlock: {compatibility: best_effort}\nprocess: {run_as_user: '1500'}\n")
    assert got["process"] == {"runAsUser": "1500"}
    assert "landlock" in got


def test_unknown_field_is_an_error_that_points_to_the_cli_backend(tmp_path: Path):
    with pytest.raises(ValueError, match="backend: cli"):
        _load(tmp_path, "version: 1\nbogus: 1\n")


def test_unknown_enum_value_is_an_error(tmp_path: Path):
    with pytest.raises(ValueError, match="is not one of"):
        _load(
            tmp_path,
            "version: 1\nnetwork_policies:\n  r:\n    endpoints:\n      - {host: a, port: 1, enforcement: sometimes}\n",
        )


def test_non_mapping_policy_is_an_error(tmp_path: Path):
    with pytest.raises(ValueError, match="mapping"):
        _load(tmp_path, "- a\n- b\n")
