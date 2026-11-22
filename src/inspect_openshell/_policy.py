"""Load an OpenShell policy YAML file as the protobuf message the gateway takes.

The CLI reads a policy YAML file and sends the gateway a ``SandboxPolicy``
message. The SDK has no loader for the file, so the SDK backend does the same
conversion here. The YAML schema is documented at
https://docs.nvidia.com/openshell/latest/how-it-works/policies/schema.html and
differs from the message in two ways that this module bridges:

* the top-level key ``filesystem_policy`` is the message field ``filesystem``;
* enum values are written in lower case without the type prefix
  (``enforcement: enforce``, ``access: read-only``) while the message names them
  ``NETWORK_ENFORCEMENT_MODE_ENFORCE`` and ``NETWORK_ACCESS_PRESET_READ_ONLY``.

Anything else that does not match the message is rejected with the field path,
rather than ignored: a policy that is silently weaker than the file says is
worse than an error. If a policy file uses something this converter cannot
express, use ``backend: cli``, which hands the file to OpenShell itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_TOP_LEVEL_RENAMES = {"filesystem_policy": "filesystem"}


def _is_repeated(fd: Any) -> bool:
    # protobuf >= 5 exposes ``is_repeated``; older releases only have ``label``.
    value = getattr(fd, "is_repeated", None)
    return bool(value) if value is not None else fd.label == fd.LABEL_REPEATED


def _enum_name(fd: Any, value: Any) -> str:
    want = str(value).strip().upper().replace("-", "_")
    names = [v.name for v in fd.enum_type.values]
    for name in names:
        if name == want or name.endswith("_" + want):
            return str(name)
    raise ValueError(f"policy field {fd.full_name}: {value!r} is not one of {names}")


def _normalise(descriptor: Any, obj: Any) -> Any:
    """Rewrite enum spellings in ``obj`` to the names the message uses."""
    from google.protobuf.descriptor import FieldDescriptor as FD

    if not isinstance(obj, dict):
        return obj
    out: dict[str, Any] = {}
    for key, value in obj.items():
        fd = descriptor.fields_by_name.get(key)
        if fd is None:
            out[key] = value  # unknown: ParseDict reports it with its path
        elif fd.type == FD.TYPE_ENUM:
            out[key] = [_enum_name(fd, v) for v in value] if _is_repeated(fd) else _enum_name(fd, value)
        elif fd.type == FD.TYPE_MESSAGE:
            if fd.message_type.GetOptions().map_entry:
                vfd = fd.message_type.fields_by_name["value"]
                out[key] = {
                    k: _normalise(vfd.message_type, v) if vfd.type == FD.TYPE_MESSAGE else v
                    for k, v in value.items()
                }
            elif _is_repeated(fd):
                out[key] = [_normalise(fd.message_type, v) for v in value]
            else:
                out[key] = _normalise(fd.message_type, value)
        else:
            out[key] = value
    return out


def load_policy(path: str | Path) -> Any:
    """Return the ``SandboxPolicy`` message for the policy YAML file at ``path``."""
    from google.protobuf import json_format
    from openshell._proto import sandbox_pb2

    data = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"policy file {path} must contain a mapping at the top level")
    for old, new in _TOP_LEVEL_RENAMES.items():
        if old in data:
            data[new] = data.pop(old)
    try:
        return json_format.ParseDict(
            _normalise(sandbox_pb2.SandboxPolicy.DESCRIPTOR, data),
            sandbox_pb2.SandboxPolicy(),
            ignore_unknown_fields=False,
        )
    except json_format.ParseError as exc:
        raise ValueError(
            f"policy file {path} cannot be converted for backend: sdk ({exc}). "
            "Use backend: cli, which passes the file to OpenShell unchanged."
        ) from exc
