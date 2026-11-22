"""runtime_class and resources for backend: sdk."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from inspect_openshell._sandbox import OpenShellConfig
from inspect_openshell._transport import SdkTransport


def test_runtime_class_and_resources_are_sdk_only(tmp_path: Path):
    for text in ("runtime_class: kata\n", "resources: {limits: {cpu: '1'}}\n"):
        (tmp_path / "a.yaml").write_text(text)
        with pytest.raises(ValueError, match="apply only to backend: sdk"):
            OpenShellConfig.load(str(tmp_path / "a.yaml"))


@pytest.mark.parametrize("resources", [{"limit": {"cpu": "1"}}, {"limits": "2"}, {"requests": ["cpu"]}])
def test_resources_shape_is_validated(resources):
    with pytest.raises(ValueError, match="`requests` and `limits`"):
        OpenShellConfig(backend="sdk", resources=resources).validate()


def test_resource_quantities_become_strings():
    cfg = OpenShellConfig(backend="sdk", runtime_class="kata", resources={"limits": {"cpu": 4, "memory": "4Gi"}})
    cfg.validate()
    t = cfg.transport()
    assert isinstance(t, SdkTransport)
    assert t.runtime_class == "kata"
    assert t.resources == {"limits": {"cpu": "4", "memory": "4Gi"}}


def test_defaults_leave_the_template_alone():
    t = OpenShellConfig(backend="sdk").transport()
    assert isinstance(t, SdkTransport) and t.runtime_class is None and t.resources is None


class FakeCreateClient:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.spec = None
        self.waited = False

    def create(self, *, workspace, name, spec):
        self.spec = spec
        if self.error:
            raise self.error

    def wait_ready(self, name, *, workspace, timeout_seconds):
        self.waited = True


class AdmissionError(Exception):
    def __init__(self, text: str):
        super().__init__(text)
        self._text = text

    def details(self):
        return self._text


@pytest.fixture
def client(monkeypatch):
    pytest.importorskip("openshell")
    c = FakeCreateClient()
    monkeypatch.setattr(SdkTransport, "_client", lambda self: c)
    return c


async def test_create_puts_runtime_class_and_resources_in_the_template(client):
    t = SdkTransport(runtime_class="kata", resources={"requests": {"cpu": "2"}, "limits": {"cpu": "2", "memory": "4Gi"}})
    await t.create("insp-1", "img:1", None)
    template = client.spec.template
    assert template.image == "img:1" and template.runtime_class_name == "kata"
    assert dict(template.resources["limits"]) == {"cpu": "2", "memory": "4Gi"}
    assert dict(template.resources["requests"]) == {"cpu": "2"}
    assert client.waited


async def test_create_without_the_keys_sets_neither(client):
    await SdkTransport().create("insp-1", "img:1", None)
    assert client.spec.template.runtime_class_name == "" and not client.spec.template.HasField("resources")


async def test_admission_failure_says_how_to_fix_it(client):
    client.error = AdmissionError("RuntimeClass 'kata': external resource not admitted by required labels")
    with pytest.raises(RuntimeError) as err:
        await SdkTransport(runtime_class="kata").create("insp-1", "img", None)
    message = str(err.value)
    assert "create failed for insp-1" in message
    assert "not admitted by required labels" in message
    assert "label runtimeclass kata openshell.ai/sandbox-attachable=true" in message
    assert not client.waited


async def test_other_create_errors_keep_their_text(client):
    client.error = AdmissionError("image not found")
    with pytest.raises(RuntimeError, match="image not found") as err:
        await SdkTransport().create("insp-1", "img", None)
    assert "sandbox-attachable" not in str(err.value)
