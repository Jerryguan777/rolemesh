"""Claude SDK ResultMessage → UsageSnapshot extraction.

The Claude Agent SDK doesn't pin its ResultMessage shape across versions:
``usage`` keys come and go as the API evolves, and ``total_cost_usd`` is
absent against custom proxies that don't compute pricing. The extraction
must be defensive enough that an SDK bump or a misconfigured deployment
doesn't crash the agent loop or produce a half-populated snapshot.

Tests here drive _build_usage_snapshot directly with mock objects so the
SDK isn't a hard import-time dependency. The string-match on
``type(message).__name__ == "ResultMessage"`` upstream means the mock
class can keep the same name without subclassing the real SDK type.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

# claude_agent_sdk is only shipped inside the agent container image; stub
# it in sys.modules BEFORE importing claude_backend so its module-level
# ``from claude_agent_sdk import ...`` resolves. Same pattern other
# claude_backend tests use (test_claude_abort.py).
_fake_sdk = types.ModuleType("claude_agent_sdk")
_fake_sdk.ClaudeAgentOptions = type(  # type: ignore[attr-defined]
    "ClaudeAgentOptions", (), {"__init__": lambda self, **kw: None}
)
_fake_sdk.HookMatcher = type(  # type: ignore[attr-defined]
    "HookMatcher", (), {"__init__": lambda self, **kw: None}
)
_fake_sdk.ToolUseBlock = type("ToolUseBlock", (), {})  # type: ignore[attr-defined]
_fake_sdk.query = lambda **kw: iter(())  # type: ignore[attr-defined]
_fake_sdk.create_sdk_mcp_server = lambda **kw: object()  # type: ignore[attr-defined]
_fake_sdk.tool = lambda *a, **kw: (lambda fn: fn)  # type: ignore[attr-defined]
sys.modules.setdefault("claude_agent_sdk", _fake_sdk)

from agent_runner.claude_backend import _build_usage_snapshot  # noqa: E402


class _ResultMessage:
    """Minimal mock with the same class NAME the SDK uses.

    type(self).__name__ == "ResultMessage" is what claude_backend matches
    on, so the literal class name matters more than the inheritance.
    """

    def __init__(self, **fields: Any) -> None:
        for k, v in fields.items():
            setattr(self, k, v)


# Force the literal class name to "ResultMessage" so __name__ checks pass.
ResultMessage = type("ResultMessage", (_ResultMessage,), {})


class TestBuildUsageSnapshot:
    def test_full_usage_dict_extracts_all_fields(self) -> None:
        msg = ResultMessage(
            usage={
                "input_tokens": 1000,
                "output_tokens": 250,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 800,
            },
            total_cost_usd=0.0042,
            model="claude-sonnet-4-6",
        )
        snap = _build_usage_snapshot(msg, model_id=None)
        assert snap is not None
        assert snap.input_tokens == 1000
        assert snap.output_tokens == 250
        assert snap.cache_write_tokens == 30  # cache_creation_input_tokens
        assert snap.cache_read_tokens == 800
        assert snap.cost_usd == 0.0042
        assert snap.cost_source == "sdk"
        assert snap.model_id == "claude-sonnet-4-6"

    def test_missing_cache_subfields_default_to_zero(self) -> None:
        """Older SDK versions / non-Anthropic providers may omit cache_*
        keys. The extraction must NOT collapse the whole snapshot to
        None — the input/output counts are still meaningful and need
        to be persisted. Only the missing fields go to zero."""
        msg = ResultMessage(
            usage={"input_tokens": 100, "output_tokens": 50},
            total_cost_usd=0.001,
            model="claude-haiku-4-5",
        )
        snap = _build_usage_snapshot(msg, model_id=None)
        assert snap is not None
        assert snap.input_tokens == 100
        assert snap.output_tokens == 50
        # CRITICAL: missing → 0, not None. NULL would break sum-aggregations
        # downstream (NULL + int = NULL in SQL).
        assert snap.cache_read_tokens == 0
        assert snap.cache_write_tokens == 0

    def test_missing_total_cost_keeps_snapshot_with_none_cost(self) -> None:
        """Custom proxies / self-hosted Claude routers may not compute
        total_cost_usd. The snapshot must still emit (with cost=None) —
        otherwise we'd lose the token counts too."""
        msg = ResultMessage(
            usage={"input_tokens": 10, "output_tokens": 5},
            model="claude-opus-4-7",
        )
        snap = _build_usage_snapshot(msg, model_id=None)
        assert snap is not None
        assert snap.cost_usd is None
        # cost_source still "sdk" — the source attribution is about
        # WHO computes cost, not whether cost was reported on this
        # particular turn. None means "SDK didn't compute it this time".
        assert snap.cost_source == "sdk"
        assert snap.input_tokens == 10

    def test_message_without_usage_returns_none(self) -> None:
        """No usage dict at all → snapshot=None. We don't fabricate a
        zero snapshot because zero is meaningfully different from
        unknown — sum-of-cost reports must be able to filter unknowns."""
        msg = ResultMessage(total_cost_usd=0.0)
        snap = _build_usage_snapshot(msg, model_id=None)
        assert snap is None

    def test_message_with_non_dict_usage_returns_none(self) -> None:
        """Defensive: if a future SDK ships usage as an object instead
        of a dict, fall back to None rather than crashing.

        The current implementation does ``isinstance(usage_raw, dict)``
        — this test pins that contract."""
        msg = ResultMessage(usage="oops a string", total_cost_usd=0.0)
        snap = _build_usage_snapshot(msg, model_id=None)
        assert snap is None

    def test_falls_back_to_active_model_id(self) -> None:
        """If the ResultMessage doesn't carry .model (older SDK), use the
        captured init-time model_id. This is why _consume_query in
        claude_backend keeps ``active_model_id`` around."""
        msg = ResultMessage(
            usage={"input_tokens": 1, "output_tokens": 1},
            total_cost_usd=0.0,
        )
        snap = _build_usage_snapshot(msg, model_id="claude-sonnet-4-6")
        assert snap is not None
        assert snap.model_id == "claude-sonnet-4-6"

    def test_message_model_overrides_init_model(self) -> None:
        """If both the message and init carry a model, prefer the
        message's — that's the authoritative one for THIS turn (model
        switching mid-conversation is supported by Claude Code)."""
        msg = ResultMessage(
            usage={"input_tokens": 1, "output_tokens": 1},
            total_cost_usd=0.0,
            model="claude-haiku-4-5",
        )
        snap = _build_usage_snapshot(msg, model_id="claude-sonnet-4-6")
        assert snap is not None
        assert snap.model_id == "claude-haiku-4-5"

    def test_neither_model_yields_none_model_id(self) -> None:
        msg = ResultMessage(
            usage={"input_tokens": 1, "output_tokens": 1},
            total_cost_usd=0.0,
        )
        snap = _build_usage_snapshot(msg, model_id=None)
        assert snap is not None
        assert snap.model_id is None

    @pytest.mark.parametrize(
        "bad_value",
        [None, "not a number", [], {}],
        ids=["none", "string", "list", "dict"],
    )
    def test_garbage_total_cost_becomes_none(self, bad_value: Any) -> None:
        """A future SDK that breaks total_cost_usd's contract must
        not crash extraction or coerce nonsense to a phantom dollar
        amount."""
        msg = ResultMessage(
            usage={"input_tokens": 1, "output_tokens": 1},
            total_cost_usd=bad_value,
        )
        snap = _build_usage_snapshot(msg, model_id=None)
        assert snap is not None
        assert snap.cost_usd is None
