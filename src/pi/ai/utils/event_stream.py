"""Event stream types — ported from packages/ai/src/utils/event-stream.ts.

In Python we use native async generators instead of the TS EventStream class.
This module re-exports the type aliases from types.py.
"""

from __future__ import annotations

from pi.ai.types import AssistantMessageEvent, AssistantMessageEventStream

__all__ = [
    "AssistantMessageEvent",
    "AssistantMessageEventStream",
]
