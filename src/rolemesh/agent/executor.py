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
    # Only meaningful for status="error". False marks a deterministic
    # configuration error the container classified at the source
    # (pi.ai.types.NonRetryableConfigError): the orchestrator fails the
    # message once, surfaces the error to the user, and skips the
    # retry/backoff ladder. Default True — an unmarked error (including
    # every event from older containers) keeps the existing retry path.
    retryable: bool = True

    def is_progress(self) -> bool:
        return self.status in PROGRESS_STATUSES


@dataclass(frozen=True)
class AgentBackendConfig:
    """Distinguishes different Agent backends.

    A single ContainerAgentExecutor uses this config to select the right
    image, entrypoint, and volume mounts for each backend.
    """

    name: str
    # None means "use the deployment-layer CONTAINER_IMAGE env var"
    # (the normal case — operators set the image via Helm values or
    # compose env, and it must also match the orphan-cleanup image
    # whitelist built from CONTAINER_IMAGE in main.py). Only set an
    # explicit image here if a backend genuinely needs a different one.
    image: str | None = None
    entrypoint: list[str] | None = None
    extra_mounts: list[tuple[str, str, bool]] = field(default_factory=list)
    extra_env: dict[str, str] = field(default_factory=dict)
    skip_claude_session: bool = False


CLAUDE_CODE_BACKEND = AgentBackendConfig(
    name="claude",
    extra_env={"AGENT_BACKEND": "claude"},
)

# DB row provider → Pi PI_MODEL_ID prefix. Pi inherits the upstream
# vendor name "amazon-bedrock" (boto3 service stem); the DB stores
# just "bedrock" because that's what the UI's provider picker shows.
# Translate at the boundary.
_DB_TO_PI_PROVIDER: dict[str, str] = {
    "bedrock": "amazon-bedrock",
}


def _pi_extra_env() -> dict[str, str]:
    """Build the static Pi-backend env from .env defaults.

    Load-time default for spawns without a coworker (evaluation CLI,
    ad-hoc tooling). Per-spawn coworker model_id selection is applied
    in ``container.runner.build_container_spec`` via the
    ``pi_model_id_override`` arg, which recomputes the same model-
    related keys this function sets here. ``BEDROCK_BASE_URL`` is
    deliberately set in ``build_container_spec`` instead — it
    depends on the per-spawn EC/rollback proxy routing decision.
    """
    import os

    from rolemesh.core.config import BEDROCK_DEFAULT_REGION

    env: dict[str, str] = {"AGENT_BACKEND": "pi"}
    model_id = os.environ.get("PI_MODEL_ID", "")
    if not model_id:
        return env
    env["PI_MODEL_ID"] = model_id
    if model_id.startswith("amazon-bedrock/"):
        if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
            # Loud at module load so an operator who sets a Bedrock
            # PI_MODEL_ID but forgets the host token sees the
            # misconfiguration up front rather than as opaque 404s
            # from the credential proxy at first tool call.
            logger.warning(
                "Pi backend uses a Bedrock model id but host has no "
                "AWS_BEARER_TOKEN_BEDROCK set; tool calls will 404 "
                "from the credential proxy. Set AWS_BEARER_TOKEN_BEDROCK "
                "in .env to fix.",
                model_id=model_id,
            )
        env["AWS_BEARER_TOKEN_BEDROCK"] = "placeholder-proxy-replaces-this"
        env["AWS_REGION"] = (
            os.environ.get("AWS_REGION", "") or BEDROCK_DEFAULT_REGION
        )
    return env


PI_BACKEND = AgentBackendConfig(
    name="pi",
    extra_env=_pi_extra_env(),
    skip_claude_session=True,
)

# Map backend names to configs for dispatch.
BACKEND_CONFIGS: dict[str, AgentBackendConfig] = {
    "claude": CLAUDE_CODE_BACKEND,
    "pi": PI_BACKEND,
}
