"""Lock the Pi-side PreCompact schema against silent drift.

Motivation
----------
Our Pi bridge registers its PreCompact handler against the literal event
name "session_before_compact" and reads CompactionPreparation.
messages_to_summarize. Both of those bindings are contracts with
upstream Pi code we do not control. If either drifts:

  - event name rename (e.g. to "session_before_compaction") → our
    handler never fires on compaction; no error, no log, just silent
    loss of transcript archival.
  - CompactionPreparation.messages_to_summarize rename → getattr
    returns None → our bridge passes CompactionEvent(messages=[]) →
    TranscriptArchiveHandler short-circuits and writes nothing.

These failures are invisible in CI unless something forces the bridge
to *run against Pi's own types*. This file does exactly that by:

  1. Importing the real SessionBeforeCompactEvent / CompactionPreparation
     classes from pi.* at module load time (a Pi rename/removal turns
     into an ImportError, failing the suite immediately).
  2. Using the real classes' fields as the source of truth for the
     event dict we hand to the bridge — if either the dataclass field
     name or default literal changes, these tests fail.
  3. Also feeding the bridge a *dataclass-instance* form of the event
     (not just a dict), since Pi's runner.emit() is a union-typed
     channel and different call sites choose different shapes. Both
     must round-trip.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from agent_runner.hooks import CompactionEvent, HookRegistry
from agent_runner.pi_backend import _build_bridge_extension
from pi.coding_agent.core.compaction.compaction import CompactionPreparation
from pi.coding_agent.core.extensions.types import (
    SessionBeforeCompactEvent,
    SessionBeforeCompactResult,
)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[CompactionEvent] = []

    async def on_pre_compact(self, event: CompactionEvent) -> None:
        self.events.append(event)


def _bridge_handler(registry: HookRegistry) -> Any:
    ext = _build_bridge_extension(registry)
    handlers = ext.handlers.get("session_before_compact")
    assert handlers, (
        "bridge did not register a handler for 'session_before_compact'"
    )
    return handlers[0]


# ---------------------------------------------------------------------------
# Schema probes — fail if Pi renames the event or moves the field
# ---------------------------------------------------------------------------


def test_pi_event_type_literal_matches_bridge_registration_key() -> None:
    """The bridge registers its handler under the event type string
    'session_before_compact'. That string MUST match the default value
    of SessionBeforeCompactEvent.type — otherwise our handler is dead
    code from Pi's perspective.

    We read the *default value* of the type field rather than the
    Literal typing, because the Literal is not introspectable at
    runtime. A refactor that changes only the default string (while
    leaving the Literal type hint intact, or vice versa) still trips
    this probe."""
    default_type = SessionBeforeCompactEvent().type
    assert default_type == "session_before_compact", (
        f"Pi's SessionBeforeCompactEvent.type has drifted to {default_type!r}; "
        f"update pi_backend._build_bridge_extension handler registration "
        f"to match."
    )


def test_pi_preparation_exposes_messages_to_summarize_field() -> None:
    """Our bridge reads preparation.messages_to_summarize. If that field
    is renamed (e.g. to messages_to_compact), getattr silently returns
    None and every compaction ends up with empty archive output."""
    field_names = {f.name for f in fields(CompactionPreparation)}
    assert "messages_to_summarize" in field_names, (
        f"CompactionPreparation has dropped or renamed "
        f"messages_to_summarize; present fields: {sorted(field_names)}"
    )


def test_pi_session_before_compact_result_has_cancel_field() -> None:
    """Defensive: downstream spec change could remove the cancel slot.
    Our bridge returns None (which Pi's runner.emit interprets as
    'do not cancel'), but if the result dataclass added required
    fields or renamed 'cancel', the contract we rely on has shifted."""
    result_fields = {f.name for f in fields(SessionBeforeCompactResult)}
    assert "cancel" in result_fields, (
        f"SessionBeforeCompactResult no longer exposes 'cancel'; "
        f"present fields: {sorted(result_fields)}"
    )


# ---------------------------------------------------------------------------
# Round-trip: feed the bridge a real-classes event payload
# ---------------------------------------------------------------------------


async def test_bridge_handles_dict_event_built_from_real_pi_types() -> None:
    """Production path: Pi's agent_session.py calls
      runner.emit({"type": "session_before_compact", "preparation": <CompactionPreparation>, ...})
    The 'type' string is hardcoded at the call site rather than sourced
    from the dataclass — but the dataclass's own default is the canonical
    value. We build the dict using the dataclass default to guarantee
    both call sites agree.

    If the bridge's handler is registered under the wrong key, Pi's
    runner.emit would dispatch to zero handlers and this test's
    recorder would stay empty — caught here."""
    registry = HookRegistry()
    recorder = _Recorder()
    registry.register(recorder)
    handler = _bridge_handler(registry)

    # Build a real CompactionPreparation using Pi's constructor. Using
    # required positional args forces the test to stay in sync with any
    # breaking constructor change.
    preparation = CompactionPreparation(
        first_kept_entry_id="entry-1",
        messages_to_summarize=[],  # empty is fine; we're testing dispatch
        turn_prefix_messages=[],
        is_split_turn=False,
        tokens_before=0,
        file_ops=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )

    # Build the event dict the way Pi actually does in agent_session.py —
    # but source the event-type string from the dataclass so a Pi rename
    # propagates through both test probes AND the on-wire dispatch path.
    event_dict: dict[str, Any] = {
        "type": SessionBeforeCompactEvent().type,
        "preparation": preparation,
    }

    result = await handler(event_dict, None)

    # No cancellation — bridge is observational for PreCompact
    assert result is None
    # Recorder saw the event
    assert len(recorder.events) == 1
    # messages field propagated (even though empty — the point is it's
    # bound to preparation.messages_to_summarize)
    assert recorder.events[0].messages == []


async def test_bridge_handles_dataclass_event_form_too() -> None:
    """Pi's emit() accepts both dict and dataclass event forms (different
    emit_* methods construct one or the other). The bridge must tolerate
    a SessionBeforeCompactEvent instance in case Pi ever unifies to
    dataclass-only emission."""
    registry = HookRegistry()
    recorder = _Recorder()
    registry.register(recorder)
    handler = _bridge_handler(registry)

    preparation = CompactionPreparation(
        first_kept_entry_id="entry-1",
        messages_to_summarize=[],
        turn_prefix_messages=[],
        is_split_turn=False,
        tokens_before=0,
        file_ops=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    event_obj = SessionBeforeCompactEvent(preparation=preparation)

    result = await handler(event_obj, None)

    assert result is None
    assert len(recorder.events) == 1


async def test_bridge_propagates_messages_to_summarize_content() -> None:
    """When CompactionPreparation carries actual messages, the bridge
    must copy them onto CompactionEvent.messages. Mutation: if the
    bridge reads `preparation.messages` (wrong attr) or
    `preparation.to_summarize` (typo), the list arrives empty."""
    registry = HookRegistry()
    recorder = _Recorder()
    registry.register(recorder)
    handler = _bridge_handler(registry)

    # Use a sentinel object list — we don't need real AgentMessage types
    # because the bridge is supposed to pass through opaquely.
    sentinels = [object(), object(), object()]
    preparation = CompactionPreparation(
        first_kept_entry_id="entry-1",
        messages_to_summarize=sentinels,
        turn_prefix_messages=[],
        is_split_turn=False,
        tokens_before=0,
        file_ops=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    await handler(
        {"type": SessionBeforeCompactEvent().type, "preparation": preparation},
        None,
    )

    assert len(recorder.events) == 1
    # Propagated verbatim — opaque list, order preserved
    assert recorder.events[0].messages == sentinels
