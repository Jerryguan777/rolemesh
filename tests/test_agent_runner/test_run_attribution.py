"""Run attribution in the agent runner (single-writer refactor).

The orchestrator's terminal-write closure (``active_run_id``) goes stale
when follow-ups are piped into a warm container: the closure was captured
at cold start and never learns about the newer runs. The fix is to make
the container itself track which ``runs`` row each prompt answers — seeded
from ``AgentInitData.run_id``, updated from the ``run_id`` on follow-up
input payloads — and echo it on every output event.

Bug-bait focus:

* FIFO discipline: batch replies must attribute one queue entry per
  ResultEvent, in arrival order — off-by-one here terminal-writes the
  WRONG run, which the WHERE status='running' guard then makes permanent.
* Non-consuming reads: progress/error/stop events describe the prompt
  still being served; consuming on them would shift the whole batch.
* Wire shape stays legacy-compatible: ``runId`` appears in the JSON only
  when there is something to attribute, mirroring isFinal/retryable.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_runner.main import ContainerOutput, _RunAttribution, drain_nats_input

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# _RunAttribution semantics
# ---------------------------------------------------------------------------


async def test_single_prompt_consume_then_fallback_to_last() -> None:
    a = _RunAttribution()
    a.enqueue("run-1")
    assert a.current() == "run-1"
    assert a.current() == "run-1", "current() must not consume"
    assert a.consume() == "run-1"
    # Queue drained: the batch-final marker (and any straggler event)
    # still attributes to the last prompt served.
    assert a.current() == "run-1"
    assert a.consume() == "run-1"


async def test_fifo_across_batched_prompts() -> None:
    a = _RunAttribution()
    a.enqueue("run-1")
    a.enqueue("run-2")
    a.enqueue("run-3")
    assert a.current() == "run-1"
    assert a.consume() == "run-1"
    assert a.current() == "run-2", "after consuming, events belong to the next prompt"
    assert a.consume() == "run-2"
    assert a.consume() == "run-3"
    assert a.current() == "run-3"


async def test_none_entries_flow_through() -> None:
    """A prompt with no run (scheduled task, legacy orchestrator) must
    yield None — not resurrect a stale earlier id."""
    a = _RunAttribution()
    a.enqueue("run-1")
    a.enqueue(None)
    assert a.consume() == "run-1"
    assert a.current() is None
    assert a.consume() is None
    assert a.current() is None, "last must track the None, not keep run-1"


async def test_empty_attribution_yields_none() -> None:
    a = _RunAttribution()
    assert a.current() is None
    assert a.consume() is None


async def test_error_mid_batch_attributes_to_prompt_being_served() -> None:
    """An error while serving prompt 1 of a batch must attribute to run-1
    (non-consuming) — the queued run-2 stays untouched for the retry path."""
    a = _RunAttribution()
    a.enqueue("run-1")
    a.enqueue("run-2")
    assert a.current() == "run-1", "error event attributes to the in-flight prompt"
    assert a.current() == "run-1", "and does not advance the queue"


# ---------------------------------------------------------------------------
# Input drain: (text, run_id) pairs
# ---------------------------------------------------------------------------


class _FakeMsg:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.data = json.dumps(payload).encode()
        self.acked = False

    async def ack(self) -> None:
        self.acked = True


class _FakeSub:
    def __init__(self, msgs: list[_FakeMsg]) -> None:
        self._msgs = list(msgs)

    async def next_msg(self, timeout: float = 0.1) -> _FakeMsg:
        if not self._msgs:
            raise TimeoutError
        return self._msgs.pop(0)


async def test_drain_returns_text_and_run_id_pairs() -> None:
    sub = _FakeSub(
        [
            _FakeMsg({"type": "message", "text": "a", "run_id": "run-1"}),
            _FakeMsg({"type": "message", "text": "b"}),
            _FakeMsg({"type": "message", "text": "c", "run_id": "run-3"}),
        ]
    )
    assert await drain_nats_input(sub) == [
        ("a", "run-1"),
        ("b", None),
        ("c", "run-3"),
    ]


async def test_drain_junk_run_id_becomes_none() -> None:
    """A non-string or empty run_id must not leak into attribution."""
    sub = _FakeSub(
        [
            _FakeMsg({"type": "message", "text": "a", "run_id": 42}),
            _FakeMsg({"type": "message", "text": "b", "run_id": ""}),
        ]
    )
    assert await drain_nats_input(sub) == [("a", None), ("b", None)]


async def test_drain_skips_non_message_payloads() -> None:
    sub = _FakeSub(
        [
            _FakeMsg({"type": "ping"}),
            _FakeMsg({"type": "message", "text": "real", "run_id": "run-1"}),
        ]
    )
    assert await drain_nats_input(sub) == [("real", "run-1")]


# ---------------------------------------------------------------------------
# Wire shape
# ---------------------------------------------------------------------------


async def test_run_id_absent_from_wire_when_none() -> None:
    """No attribution → serialize exactly as before this change."""
    d = ContainerOutput(status="success", result="ok").to_dict()
    assert "runId" not in d


async def test_run_id_emitted_when_set() -> None:
    d = ContainerOutput(status="success", result="ok", run_id="run-1").to_dict()
    assert d["runId"] == "run-1"
