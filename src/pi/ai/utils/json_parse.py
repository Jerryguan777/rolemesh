"""Streaming JSON parser — Python port of packages/ai/src/utils/json-parse.ts."""

from __future__ import annotations

import json
from typing import Any

from partial_json_parser import loads as partial_json_loads


def parse_streaming_json(partial_json: str | None) -> dict[str, Any]:
    """Parse potentially incomplete JSON during streaming.

    Always returns a valid dict, even if the JSON is incomplete.
    """
    if not partial_json or partial_json.strip() == "":
        return {}

    # Try standard parsing first (fastest for complete JSON)
    try:
        result = json.loads(partial_json)
        if isinstance(result, dict):
            return result
        return {}
    except json.JSONDecodeError:
        pass

    # Use partial-json-parser for incomplete JSON
    try:
        result = partial_json_loads(partial_json)
        if isinstance(result, dict):
            return result
        return {}
    except (ValueError, KeyError, IndexError, TypeError):
        return {}
