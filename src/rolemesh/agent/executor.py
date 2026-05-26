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

# DB row provider → Pi PI_MODEL_ID format-string provider segment.
# Pi inherits the vendor convention from upstream where Bedrock is
# called "amazon-bedrock" (the boto3 service stem). The DB calls it
# just "bedrock" because that's what users see in the UI's provider
# picker. Map at the boundary.
_DB_TO_PI_PROVIDER: dict[str, str] = {
    "bedrock": "amazon-bedrock",
}


def pi_format_model_id(provider: str, model_id: str) -> str:
    """Format a DB (provider, model_id) pair into Pi's PI_MODEL_ID string.

    Pi expects ``<provider>/<model_id>``. Most DB providers are 1:1
    with Pi (``openai``/``anthropic``/``google``); only ``bedrock``
    gets renamed to ``amazon-bedrock``.
    """
    pi_provider = _DB_TO_PI_PROVIDER.get(provider, provider)
    return f"{pi_provider}/{model_id}"


def pi_env_for_model_id(model_id: str) -> dict[str, str]:
    """Build the env keys Pi backend needs for a given PI_MODEL_ID.

    Pure function — same input yields same output (modulo os.environ
    fallback for AWS_REGION). Used in two paths:

    1. Module-load default (``_pi_extra_env()`` below) — reads
       ``PI_MODEL_ID`` from the host's .env so spawns without a
       coworker (e.g. evaluation CLI) still get a working default.
    2. Per-spawn override (PR30 wiring) — the container executor
       resolves ``coworker.model_id`` against the ``models`` table
       and calls this with the resulting Pi-formatted string so the
       per-coworker model selection actually reaches the container.

    Returns just the model-related env (PI_MODEL_ID + optional
    Bedrock boto3 placeholders). The caller composes it with
    ``AGENT_BACKEND`` and other static keys.

    API keys are NOT injected here; all LLM requests go through the
    credential proxy which injects real keys at the HTTP level. For
    Bedrock we inject placeholder ``AWS_BEARER_TOKEN_BEDROCK`` so
    boto3 doesn't raise ``NoCredentialsError`` before sending; the
    proxy is the real credential gate. ``AWS_REGION`` makes boto3's
    model-ARN resolution line up with the upstream URL the proxy
    bound (single-source via ``BEDROCK_DEFAULT_REGION``).

    ``BEDROCK_BASE_URL`` is intentionally NOT set here — it lives
    in ``rolemesh.container.runner.build_container_spec`` because it
    depends on per-spawn proxy routing decisions.
    """
    import os

    from rolemesh.core.config import BEDROCK_DEFAULT_REGION

    env: dict[str, str] = {}
    if not model_id:
        return env
    env["PI_MODEL_ID"] = model_id

    if model_id.startswith("amazon-bedrock/"):
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
        env["AWS_REGION"] = (
            os.environ.get("AWS_REGION", "") or BEDROCK_DEFAULT_REGION
        )

    return env


def _pi_extra_env() -> dict[str, str]:
    """Build the static Pi-backend env from .env defaults.

    Used as the load-time default when no per-coworker model_id is
    resolved at spawn time (evaluation CLI, ad-hoc tooling). The
    container executor's per-spawn path (see container_executor.py)
    overrides PI_MODEL_ID with the coworker's choice when available.
    """
    import os

    env: dict[str, str] = {"AGENT_BACKEND": "pi"}
    env.update(pi_env_for_model_id(os.environ.get("PI_MODEL_ID", "")))
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
