"""Forward-compatible dataclass deserialization helper for IPC payloads.

Background
----------
Orchestrator and container code run on independently shipped versions:
during a rolling upgrade, the orchestrator may emit a NATS payload that
contains fields the older container's dataclass does not yet declare.
The pre-refactor code happened to be safe by accident — every
``from_bytes`` cherry-picked keys with ``d["x"]`` / ``d.get("x")``, so
unknown keys silently fell on the floor — but the moment anyone
"simplifies" it to ``cls(**d)`` the contract breaks.

This helper makes the safe path the obvious path.

Required-field semantics
------------------------
Filtering only drops *unknown* keys. Missing *required* keys (declared
on the dataclass with neither a default nor a default_factory) raise
``KeyError`` with the offending name. That mirrors the original
``d["x"]`` behaviour and is what INV-2's pinned test cares about — we
never want a missing required field to be silently defaulted.
"""

from __future__ import annotations

from dataclasses import MISSING, fields
from typing import Any, TypeVar

T = TypeVar("T")


def from_dict_filter_unknown(cls: type[T], data: dict[str, Any]) -> T:
    """Build a ``cls`` instance, silently dropping unknown keys.

    Raises ``KeyError`` if any field without a default or default_factory
    is missing from ``data``.
    """
    field_specs = fields(cls)  # type: ignore[arg-type]
    known = {f.name: f for f in field_specs}
    filtered = {k: v for k, v in data.items() if k in known}
    for name, spec in known.items():
        if name in filtered:
            continue
        if spec.default is MISSING and spec.default_factory is MISSING:
            raise KeyError(name)
    return cls(**filtered)
