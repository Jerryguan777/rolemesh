"""Typebox helper utilities -- Python port of packages/ai/src/utils/typebox-helpers.ts.

Provides JSON Schema helpers that mirror the TS StringEnum helper.
"""

from __future__ import annotations

from typing import Any


def string_enum(
    values: list[str],
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a string enum JSON Schema compatible with Google's API.

    Returns a plain dict JSON Schema with type "string" and an enum constraint.
    This is the Python equivalent of the TS StringEnum() TypeBox helper.

    Args:
        values: The allowed string values.
        options: Optional dict with "description" and/or "default" keys.

    Returns:
        A JSON Schema dict.
    """
    schema: dict[str, Any] = {"type": "string", "enum": list(values)}
    if options:
        if "description" in options:
            schema["description"] = options["description"]
        if "default" in options:
            schema["default"] = options["default"]
    return schema
