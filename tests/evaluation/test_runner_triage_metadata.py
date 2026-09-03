"""EvalRunner failure-triage collection — tool-event trail + container ids.

Exercises ``execute_sample`` against a stubbed executor (no Docker, no
NATS) and asserts the diagnostic fields the .eval log relies on for
failure attribution:

  * ``observed_tool_events`` — one {tool, ts_ms, input_preview} entry
    per ToolUseEvent, in call order, previews taken from the wire's
    ``metadata["input"]``;
  * ``container_name`` / ``job_id`` — captured from on_process so a
    failed sample in ``inspect view`` links straight to its
    container-side transcript;
  * both survive the error path (fields captured before the failure
    must not be dropped by the exception return).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import rolemesh.evaluation.runner as runner_mod
from rolemesh.agent.executor import AgentOutput
from rolemesh.evaluation.runner import EvalRunner


def _coworker() -> Any:
    return SimpleNamespace(
        id="cw-1",
        name="Test Coworker",
        tenant_id="t-1",
        agent_backend="claude",
        permissions=None,
        system_prompt="be helpful",
    )


class _StubTransport:
    """Only the shutdown RPC path touches transport; make it a no-op."""

    class _NC:
        async def request(self, *_a: Any, **_kw: Any) -> None:
            return None

    nc = _NC()


def _make_runner() -> EvalRunner:
    return EvalRunner(
        runtime=SimpleNamespace(),
        transport=_StubTransport(),
        get_coworker=lambda _cid: None,
        run_id="run-1",
        timeout_s=5.0,
    )


def _stub_executor_class(script: list[AgentOutput], *, raise_after: bool = False):
    """Executor double: reports a process, replays ``script`` through
    on_output, then returns success or raises."""

    class _StubExecutor:
        def __init__(self, **_kw: Any) -> None:
            pass

        async def execute(
            self, _agent_input: Any, *, on_process: Any, on_output: Any,
        ) -> AgentOutput:
            on_process("cont-abc", "job-42")
            for out in script:
                await on_output(out)
            if raise_after:
                msg = "backend exploded"
                raise RuntimeError(msg)
            return AgentOutput(status="success", result=None)

    return _StubExecutor


def _tool(tool: str, preview: str) -> AgentOutput:
    return AgentOutput(
        status="tool_use", result=None, is_final=False,
        metadata={"tool": tool, "input": preview},
    )


@pytest.mark.asyncio
async def test_tool_events_and_container_ids_collected(monkeypatch) -> None:
    script = [
        _tool("bash", "ls -la"),
        _tool("mcp__jira__update_issue", '{"issue": "OPS-12"}'),
        AgentOutput(status="success", result="done", is_final=True),
    ]
    monkeypatch.setattr(
        runner_mod, "ContainerAgentExecutor", _stub_executor_class(script),
    )
    execution = await _make_runner().execute_sample(
        coworker=_coworker(), sample_idx=0, prompt="do the thing",
    )

    assert execution.status == "success"
    assert execution.container_name == "cont-abc"
    assert execution.job_id == "job-42"
    assert execution.observed_tool_calls == [
        "bash", "mcp__jira__update_issue",
    ]
    assert [e["tool"] for e in execution.observed_tool_events] == [
        "bash", "mcp__jira__update_issue",
    ]
    assert [e["input_preview"] for e in execution.observed_tool_events] == [
        "ls -la", '{"issue": "OPS-12"}',
    ]
    for event in execution.observed_tool_events:
        assert isinstance(event["ts_ms"], int)
        assert event["ts_ms"] >= 0


@pytest.mark.asyncio
async def test_non_string_preview_becomes_empty(monkeypatch) -> None:
    """A malformed wire payload must not poison the trail."""
    script = [
        AgentOutput(
            status="tool_use", result=None, is_final=False,
            metadata={"tool": "bash", "input": {"not": "a string"}},
        ),
        AgentOutput(status="success", result="ok", is_final=True),
    ]
    monkeypatch.setattr(
        runner_mod, "ContainerAgentExecutor", _stub_executor_class(script),
    )
    execution = await _make_runner().execute_sample(
        coworker=_coworker(), sample_idx=0, prompt="x",
    )
    assert execution.observed_tool_events[0]["input_preview"] == ""


@pytest.mark.asyncio
async def test_error_path_keeps_triage_fields(monkeypatch) -> None:
    """Fields captured before the failure survive the exception return —
    a crashed sample is exactly when the triage trail matters most."""
    script = [_tool("bash", "rm -rf /tmp/x")]
    monkeypatch.setattr(
        runner_mod,
        "ContainerAgentExecutor",
        _stub_executor_class(script, raise_after=True),
    )
    execution = await _make_runner().execute_sample(
        coworker=_coworker(), sample_idx=3, prompt="x",
    )
    assert execution.status == "error"
    assert execution.container_name == "cont-abc"
    assert execution.job_id == "job-42"
    assert execution.observed_tool_events[0]["tool"] == "bash"
    assert execution.observed_tool_events[0]["input_preview"] == "rm -rf /tmp/x"
