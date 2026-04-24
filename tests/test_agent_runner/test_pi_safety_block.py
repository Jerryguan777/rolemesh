"""Pi backend: UserPromptSubmit block emits SafetyBlockEvent.

Parity with tests/test_agent_runner/test_claude_safety_block.py. Pi's
block surface has always been visible to the user (unlike Claude's
silent-hang pre-refactor) because it emitted a ResultEvent at block
time. The refactor swapped that for a dedicated SafetyBlockEvent so
blocks don't pose as assistant replies in the messages table or in
metrics. These tests pin the new emission shape:

  1. Block verdict → emits SafetyBlockEvent(stage="input_prompt",
     reason=...) AND returns None so the caller doesn't forward the
     prompt to session.prompt().
  2. Allow verdict → returns original text unchanged, no SafetyBlock
     event emitted (zero-overhead for the common path).
  3. Verdict with appended_context → returns text prefixed with the
     context; no SafetyBlock emitted. Pins that warn-style verdicts
     DO NOT flow through the block channel.
  4. Hook raising an exception → fail-close: emits SafetyBlockEvent
     with "Hook system error: ..." and returns None. A hook-layer bug
     must not silently let the prompt reach the LLM.
  5. No reason on block → default fallback string appears in the
     emitted SafetyBlockEvent (not swapped for a different placeholder).
"""

from __future__ import annotations

from typing import Any

import pytest

# Pi backend's import chain pulls in several third-party deps
# (boto3, partial_json_parser, google.genai, ...) that live behind the
# [safety-ml] / Pi SDK extras. In a minimal dev venv they are absent
# and any attempt to import pi_backend errors out at collection. Skip
# gracefully in that environment; CI installs the full extras and runs
# the cases. The goal is that the test EXISTS and documents the
# contract — not that every developer must install Pi's full stack
# to run the repo's test suite.
pi_backend = pytest.importorskip(
    "agent_runner.pi_backend",
    reason="Pi backend deps not installed in this env",
)

from agent_runner.backend import SafetyBlockEvent  # noqa: E402
from agent_runner.hooks import HookRegistry, UserPromptEvent, UserPromptVerdict  # noqa: E402

PiBackend = pi_backend.PiBackend


class _BlockingPromptHook:
    def __init__(self, reason: str | None = None) -> None:
        self._reason = reason

    async def on_user_prompt_submit(
        self, _event: UserPromptEvent
    ) -> UserPromptVerdict:
        return UserPromptVerdict(block=True, reason=self._reason)


class _AllowingPromptHook:
    async def on_user_prompt_submit(
        self, _event: UserPromptEvent
    ) -> UserPromptVerdict | None:
        return None


class _ContextAppendingHook:
    def __init__(self, ctx: str) -> None:
        self._ctx = ctx

    async def on_user_prompt_submit(
        self, _event: UserPromptEvent
    ) -> UserPromptVerdict:
        return UserPromptVerdict(block=False, appended_context=self._ctx)


class _RaisingPromptHook:
    async def on_user_prompt_submit(
        self, _event: UserPromptEvent
    ) -> UserPromptVerdict:
        raise RuntimeError("synthetic hook failure")


def _backend_with_hook(hook: Any) -> tuple[PiBackend, list[Any]]:
    """Build a minimal PiBackend wired with a recorder for _emit events.

    Doesn't call start() — we only exercise _apply_user_prompt_hook,
    which reads self._hooks and self._session_file (set to None by
    __init__). No Pi session is needed.
    """
    backend = PiBackend()
    reg = HookRegistry()
    reg.register(hook)
    backend._hooks = reg  # type: ignore[attr-defined]

    emitted: list[Any] = []

    async def _record(event: Any) -> None:
        emitted.append(event)

    backend.subscribe(_record)
    return backend, emitted


async def test_block_emits_safety_block_event_and_returns_none() -> None:
    backend, emitted = _backend_with_hook(
        _BlockingPromptHook("Blocked: detected PII.PHONE_US")
    )

    result = await backend._apply_user_prompt_hook("My number is 555-1234")  # type: ignore[attr-defined]

    # None signals to the caller: do NOT forward to session.prompt().
    assert result is None
    # Exactly one SafetyBlockEvent emitted.
    assert len(emitted) == 1
    event = emitted[0]
    assert isinstance(event, SafetyBlockEvent)
    assert event.stage == "input_prompt"
    assert event.reason == "Blocked: detected PII.PHONE_US"
    assert event.rule_id is None


async def test_allow_verdict_returns_text_with_no_emission() -> None:
    backend, emitted = _backend_with_hook(_AllowingPromptHook())

    result = await backend._apply_user_prompt_hook("hello world")  # type: ignore[attr-defined]

    # Original text passes through unchanged.
    assert result == "hello world"
    # Zero-overhead contract for the common case: no event emitted.
    assert emitted == []


async def test_appended_context_prepends_text_and_does_not_emit_block() -> None:
    """warn-style verdicts (appended_context, not block) must flow
    through the text channel, NOT the SafetyBlock channel. If this
    regresses, every warn-level rule will look like a block in the UI.
    """
    backend, emitted = _backend_with_hook(
        _ContextAppendingHook("Heads up: PII detected but allowed")
    )

    result = await backend._apply_user_prompt_hook("original prompt")  # type: ignore[attr-defined]

    assert result == "Heads up: PII detected but allowed\n\noriginal prompt"
    # Context was prepended via the normal text channel; no block.
    assert emitted == []


async def test_hook_raises_fails_closed_with_safety_block_event() -> None:
    """A bug inside the hook handler must NOT let the prompt slip
    through unsafely. We emit a SafetyBlockEvent with the exception
    message and return None so the caller short-circuits — same block
    outcome as an explicit verdict.block, just with a different reason.
    """
    backend, emitted = _backend_with_hook(_RaisingPromptHook())

    result = await backend._apply_user_prompt_hook("anything")  # type: ignore[attr-defined]

    assert result is None
    assert len(emitted) == 1
    event = emitted[0]
    assert isinstance(event, SafetyBlockEvent)
    assert event.stage == "input_prompt"
    assert event.reason.startswith("Hook system error:")
    assert "synthetic hook failure" in event.reason


async def test_block_without_reason_uses_default_in_event() -> None:
    """Fallback text is consistent with the Claude backend's default
    ('Prompt blocked by hook') so UI copy is uniform across backends.
    """
    backend, emitted = _backend_with_hook(_BlockingPromptHook(reason=None))

    result = await backend._apply_user_prompt_hook("x")  # type: ignore[attr-defined]

    assert result is None
    assert len(emitted) == 1
    assert emitted[0].reason == "Prompt blocked by hook"
