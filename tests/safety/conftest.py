"""Shared fixtures for safety tests.

Provides a minimal fake publisher and a helper to build SafetyContexts
without dragging in the full agent runtime. The fake publisher captures
published events as plain dicts so assertions can pin exact shape.
"""

from __future__ import annotations

from typing import Any

import pytest

from rolemesh.safety.types import SafetyContext, Stage, ToolInfo


class CapturePublisher:
    """Records ``publisher(subject, data)`` calls in order.

    Matches the signature ``ToolContext.publish`` exposes — a sync
    callable that returns None. The pipeline treats publish as
    fire-and-forget, so a sync fake is sufficient.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.should_raise: Exception | None = None

    def __call__(self, subject: str, data: dict[str, Any]) -> None:
        if self.should_raise is not None:
            raise self.should_raise
        self.events.append((subject, dict(data)))


@pytest.fixture
def publisher() -> CapturePublisher:
    return CapturePublisher()


def make_context(
    *,
    stage: Stage = Stage.PRE_TOOL_CALL,
    tenant_id: str = "tenant-1",
    coworker_id: str = "cw-1",
    user_id: str = "user-1",
    job_id: str = "job-1",
    conversation_id: str = "conv-1",
    payload: dict[str, Any] | None = None,
    tool_name: str = "github__create_issue",
    reversible: bool = False,
) -> SafetyContext:
    return SafetyContext(
        stage=stage,
        tenant_id=tenant_id,
        coworker_id=coworker_id,
        user_id=user_id,
        job_id=job_id,
        conversation_id=conversation_id,
        payload=payload or {"tool_name": tool_name, "tool_input": {}},
        tool=ToolInfo(name=tool_name, reversible=reversible),
    )


def make_rule(
    *,
    rule_id: str = "rule-1",
    stage: Stage = Stage.PRE_TOOL_CALL,
    check_id: str = "pii.regex",
    config: dict[str, Any] | None = None,
    coworker_id: str | None = None,
    priority: int = 100,
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "id": rule_id,
        "tenant_id": "tenant-1",
        "coworker_id": coworker_id,
        "stage": stage.value,
        "check_id": check_id,
        "config": config or {"patterns": {"SSN": True}},
        "priority": priority,
        "enabled": enabled,
        "description": "",
    }
