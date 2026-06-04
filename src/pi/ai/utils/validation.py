"""Tool call validation — Python port of packages/ai/src/utils/validation.ts.

Uses jsonschema for JSON Schema validation instead of AJV.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from pi.ai.types import Tool, ToolCall

try:
    import jsonschema

    _HAS_JSONSCHEMA = True
except ImportError:
    _HAS_JSONSCHEMA = False


def validate_tool_call(tools: list[Tool], tool_call: ToolCall) -> dict[str, Any]:
    """Find a tool by name and validate the tool call arguments.

    Returns the validated arguments.
    Raises ValueError if tool not found or validation fails.
    """
    tool = next((t for t in tools if t.name == tool_call.name), None)
    if tool is None:
        raise ValueError(f'Tool "{tool_call.name}" not found')
    return validate_tool_arguments(tool, tool_call)


def validate_tool_arguments(tool: Tool, tool_call: ToolCall) -> dict[str, Any]:
    """Validate tool call arguments against the tool's JSON Schema.

    Returns the validated arguments.
    Raises ValueError if validation fails.
    """
    if not _HAS_JSONSCHEMA:
        # Without jsonschema, trust the LLM's output
        return tool_call.arguments

    args = copy.deepcopy(tool_call.arguments)

    try:
        jsonschema.validate(instance=args, schema=tool.parameters)
    except jsonschema.ValidationError as e:
        path = ".".join(str(p) for p in e.absolute_path) if e.absolute_path else "root"
        error_msg = (
            f'Validation failed for tool "{tool_call.name}":\n'
            f"  - {path}: {e.message}\n\n"
            f"Received arguments:\n{json.dumps(tool_call.arguments, indent=2)}"
        )
        raise ValueError(error_msg) from None

    return args
