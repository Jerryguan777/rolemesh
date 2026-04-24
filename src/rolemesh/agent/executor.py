"""Agent execution data types and backend configs.

Defines AgentInput, AgentOutput, and AgentBackendConfig.
The concrete executor (ContainerAgentExecutor) lives in container_executor.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class AgentInput:
    """Input to an agent execution."""

    prompt: str
    group_folder: str
    chat_jid: str
    permissions: dict[str, object]
    tenant_id: str = ""
    coworker_id: str = ""
    conversation_id: str = ""
    user_id: str = ""
    session_id: str | None = None
    is_scheduled_task: bool = False
    assistant_name: str | None = None
    system_prompt: str | None = None
    role_config: dict[str, object] | None = None


# Progress statuses are transient UX indicators; terminal statuses carry the
# final result or error. The tuple is the single source of truth — the Literal
# below and is_progress() both derive from it.
PROGRESS_STATUSES: tuple[str, ...] = ("queued", "container_starting", "running", "tool_use")
TERMINAL_STATUSES: tuple[str, ...] = ("success", "error", "stopped", "safety_blocked")

ProgressStatus = Literal["queued", "container_starting", "running", "tool_use"]
TerminalStatus = Literal["success", "error", "stopped", "safety_blocked"]


@dataclass(frozen=True)
class AgentOutput:
    """Output from an agent execution.

    Terminal statuses end the current turn:
      - success:         turn completed normally with a result
      - error:           turn failed; error field carries the reason
      - stopped:         user interrupted the turn via Stop button;
                         container is still alive for subsequent prompts
      - safety_blocked:  safety framework intercepted the turn
                         (INPUT_PROMPT hook, PRE_TOOL_CALL hook, or
                         orchestrator-side MODEL_OUTPUT pipeline).
                         ``result`` carries the user-facing reason;
                         ``metadata`` carries ``{stage, rule_id?}``.
                         Orchestrator routes this to a dedicated WS
                         frame without writing to the messages table.
    Progress statuses (see PROGRESS_STATUSES) are transient indicators for UX;
    `metadata` carries the structured payload.

    `is_final` is only meaningful for status="success". A run_prompt call that
    answers multiple queued user messages emits one success per message; only
    the final one carries is_final=True, and only that one should release
    idle-gating in the scheduler (notify_idle). Older runtimes that don't
    emit is_final default to True, preserving one-reply-per-turn semantics.
    """

    status: Literal[
        "success", "error", "stopped", "safety_blocked",
        "queued", "container_starting", "running", "tool_use",
    ]
    result: str | None
    new_session_id: str | None = None
    error: str | None = None
    metadata: dict[str, object] | None = None
    is_final: bool = True

    def is_progress(self) -> bool:
        return self.status in PROGRESS_STATUSES


@dataclass(frozen=True)
class AgentBackendConfig:
    """Distinguishes different Agent backends.

    A single ContainerAgentExecutor uses this config to select the right
    image, entrypoint, and volume mounts for each backend.
    """

    name: str
    image: str
    entrypoint: list[str] | None = None
    extra_mounts: list[tuple[str, str, bool]] = field(default_factory=list)
    extra_env: dict[str, str] = field(default_factory=dict)
    skip_claude_session: bool = False


CLAUDE_CODE_BACKEND = AgentBackendConfig(
    name="claude",
    image="rolemesh-agent:latest",
    extra_env={"AGENT_BACKEND": "claude"},
)

def _pi_extra_env() -> dict[str, str]:
    """Build extra env for Pi backend — model selection only.

    API keys are NOT injected here; all LLM requests go through the
    credential proxy which injects real keys at the HTTP level.
    """
    import os

    # .env loading is handled at process entry by
    # ``rolemesh.bootstrap``; reading from os.environ here works
    # for shell exports, systemd EnvironmentFile, docker --env-file,
    # and the auto-loaded .env alike.
    env: dict[str, str] = {"AGENT_BACKEND": "pi"}
    model_id = os.environ.get("PI_MODEL_ID", "")
    if model_id:
        env["PI_MODEL_ID"] = model_id
    return env


PI_BACKEND = AgentBackendConfig(
    name="pi",
    image="rolemesh-agent:latest",
    extra_env=_pi_extra_env(),
    skip_claude_session=True,
)

# Map backend names to configs for dispatch.
BACKEND_CONFIGS: dict[str, AgentBackendConfig] = {
    "claude": CLAUDE_CODE_BACKEND,
    "claude-code": CLAUDE_CODE_BACKEND,  # legacy alias
    "pi": PI_BACKEND,
}
