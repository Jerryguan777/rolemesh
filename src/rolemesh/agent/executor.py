"""Agent execution data types and backend configs.

Defines AgentInput, AgentOutput, and AgentBackendConfig.
The concrete executor (ContainerAgentExecutor) lives in container_executor.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from rolemesh.core.logger import get_logger

logger = get_logger()


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
    """Build extra env for Pi backend — model selection + boto3
    placeholders.

    API keys are NOT injected here; all LLM requests go through the
    credential proxy which injects real keys at the HTTP level. For
    Bedrock specifically, we inject a placeholder
    ``AWS_BEARER_TOKEN_BEDROCK`` (so boto3 doesn't raise
    ``NoCredentialsError`` before it even sends) and an
    ``AWS_REGION`` so boto3's model-ARN resolution lines up with
    the upstream URL the credential proxy bound (single-source via
    ``BEDROCK_DEFAULT_REGION``).

    ``BEDROCK_BASE_URL`` is intentionally NOT set here — it lives
    in ``rolemesh.container.runner.build_container_spec`` alongside
    ``ANTHROPIC_BASE_URL`` / ``OPENAI_BASE_URL`` because it depends
    on per-spawn ``proxy_base`` (egress-gateway under EC-2,
    host.docker.internal under rollback). Computing it here would
    bake module-load-time hosting into a per-spawn decision.
    """
    import os

    from rolemesh.core.config import BEDROCK_DEFAULT_REGION

    # .env loading is handled at process entry by
    # ``rolemesh.bootstrap``; reading from os.environ here works
    # for shell exports, systemd EnvironmentFile, docker --env-file,
    # and the auto-loaded .env alike.
    env: dict[str, str] = {"AGENT_BACKEND": "pi"}
    model_id = os.environ.get("PI_MODEL_ID", "")
    if model_id:
        env["PI_MODEL_ID"] = model_id

    # Bedrock wiring — only meaningful when the host has the bearer
    # token configured AND the model id targets Bedrock. We still
    # inject the placeholder unconditionally on the Bedrock path so
    # boto3 client init doesn't raise; the proxy is the real
    # credential gate.
    if model_id.startswith("amazon-bedrock/"):
        # Diagnostic guard: if the host doesn't actually have the
        # bearer token set, the credential proxy will not register a
        # ``bedrock`` provider entry and every container request will
        # surface as a 404 from the proxy, with no obvious link back
        # to "you forgot to set AWS_BEARER_TOKEN_BEDROCK in .env".
        # Warn at container-spec build time so the misconfiguration
        # is visible in the orchestrator log instead of as an
        # opaque mid-turn error.
        if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
            logger.warning(
                "Pi backend uses a Bedrock model id but host has no "
                "AWS_BEARER_TOKEN_BEDROCK set; the credential proxy "
                "will not register a bedrock provider, and every "
                "tool call will return 404 from the proxy. Set "
                "AWS_BEARER_TOKEN_BEDROCK in .env to fix.",
                model_id=model_id,
            )

        env["AWS_BEARER_TOKEN_BEDROCK"] = "placeholder-proxy-replaces-this"
        # Region picks the model's region context inside boto3 (model
        # ARNs are region-scoped). Single source of truth in
        # ``rolemesh.core.config.BEDROCK_DEFAULT_REGION``; the
        # credential proxy uses the same fallback so endpoint URL
        # and model ARN resolution stay in the same region.
        env["AWS_REGION"] = os.environ.get("AWS_REGION", "") or BEDROCK_DEFAULT_REGION

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
    "pi": PI_BACKEND,
}
