"""Verify the W3C trace-context carrier survives the orchestrator ->
KV -> agent-runner serialisation round-trip used by every container
spawn.

This is the only IPC contract change in the spike — if it regresses,
the agent runner can't attach its spans to the orchestrator's parent
span, and the cross-process trace tree silently breaks.
"""

from __future__ import annotations

from rolemesh.ipc.protocol import AgentInitData


def test_trace_context_round_trips_through_serialize_deserialize() -> None:
    """A populated ``trace_context`` survives ``serialize`` ->
    ``deserialize`` byte-equal."""
    carrier = {
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "tracestate": "rojo=00f067aa0ba902b7",
    }
    init = AgentInitData(
        prompt="hi",
        group_folder="t",
        chat_jid="c@chat",
        trace_context=carrier,
    )
    raw = init.serialize()
    rebuilt = AgentInitData.deserialize(raw)
    assert rebuilt.trace_context == carrier


def test_trace_context_defaults_to_none() -> None:
    """Missing ``trace_context`` is the default — observability off."""
    init = AgentInitData(prompt="hi", group_folder="t", chat_jid="c@chat")
    rebuilt = AgentInitData.deserialize(init.serialize())
    assert rebuilt.trace_context is None


def test_trace_context_absent_in_legacy_payload_is_none() -> None:
    """Old payloads (pre-spike) deserialize without crashing — the new
    field reads as None via ``raw.get("trace_context")``.
    """
    import json

    legacy_payload = json.dumps(
        {
            "prompt": "hi",
            "group_folder": "t",
            "chat_jid": "c@chat",
            # Deliberately no trace_context.
        }
    ).encode()
    init = AgentInitData.deserialize(legacy_payload)
    assert init.trace_context is None
