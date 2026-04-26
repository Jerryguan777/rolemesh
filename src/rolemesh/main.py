"""Main orchestrator -- state management, message loop, agent invocation.

Multi-tenant architecture: OrchestratorState replaces module-level globals.
Routing: binding_id -> conversation -> coworker.
"""
# ruff: noqa: I001
# Intentional import order: rolemesh.bootstrap MUST run first to
# populate os.environ from .env before other rolemesh imports capture
# module-level values. Disable ruff's import sorter for this file
# only; semantics > stylistic ordering here.

from __future__ import annotations

# Side-effect import: runs load_env() so ``.env`` lands in os.environ
# BEFORE core/config + peers capture module-level values at import
# time. Must stay at the very top of rolemesh imports. See
# ``rolemesh.bootstrap`` for why and how this is structured.
import rolemesh.bootstrap  # noqa: F401

import asyncio
import contextlib
import json
import os
import re
import signal
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rolemesh.channels.gateway import ChannelGateway
    from rolemesh.safety.engine import SafetyEngine

from rolemesh.agent import (
    BACKEND_CONFIGS,
    CLAUDE_CODE_BACKEND,
    PI_BACKEND,
    AgentInput,
    AgentOutput,
    ContainerAgentExecutor,
)
from rolemesh.auth.permissions import AgentPermissions
from rolemesh.channels.slack_gateway import SlackGateway
from rolemesh.channels.telegram_gateway import TelegramGateway
from rolemesh.channels.web_nats_gateway import WebNatsGateway
from rolemesh.container.runner import (
    write_tasks_snapshot,
)
from rolemesh.container.runtime import (
    PROXY_BIND_HOST,
    get_runtime,
)
from rolemesh.container.scheduler import GroupQueue
from rolemesh.core.config import (
    AGENT_BACKEND_DEFAULT,
    ASSISTANT_NAME,
    CONTAINER_EGRESS_NETWORK_NAME,
    CONTAINER_NETWORK_NAME,
    CREDENTIAL_PROXY_PORT,
    EGRESS_GATEWAY_CONTAINER_NAME,
    GLOBAL_MAX_CONTAINERS,
    IDLE_TIMEOUT,
    NATS_URL,
    POLL_INTERVAL,
    TIMEZONE,
)
from rolemesh.core.logger import get_logger
from rolemesh.core.orchestrator_state import (
    ConversationState,
    CoworkerConfig,
    CoworkerState,
    OrchestratorState,
)
from rolemesh.core.types import ChannelBinding, Conversation, Coworker
from rolemesh.db.pg import (
    DEFAULT_TENANT,
    close_database,
    create_conversation,
    create_tenant,
    get_all_channel_bindings,
    get_all_conversations,
    get_all_coworkers,
    get_all_sessions,
    get_all_tasks,
    get_conversation_by_binding_and_chat,
    get_messages_since,
    get_new_messages_for_conversations,
    get_tenant_by_slug,
    init_database,
    set_session,
    update_conversation_last_invocation,
    update_tenant_message_cursor,
)
from rolemesh.db.pg import (
    get_conversations_for_coworker as pg_get_conversations_for_coworker,
)
from rolemesh.db.pg import (
    store_message as db_store_message,
)
from rolemesh.ipc.nats_transport import NatsTransport
from rolemesh.ipc.task_handler import process_task_ipc
from rolemesh.orchestration.remote_control import (
    restore_remote_control,
)
from rolemesh.orchestration.router import format_messages, format_outbound
from rolemesh.orchestration.task_scheduler import start_scheduler_loop
from rolemesh.security.credential_proxy import register_mcp_server, set_token_vault, start_credential_proxy
from rolemesh.security.sender_allowlist import (
    is_sender_allowed,
    is_trigger_allowed,
    load_sender_allowlist,
    should_drop_message,
)

if TYPE_CHECKING:
    from rolemesh.container.runtime import ContainerRuntime
    from rolemesh.core.types import NewMessage

logger = get_logger()

__all__ = ["main", "main_sync"]

# ---------------------------------------------------------------------------
# Module-level runtime objects (not state — those are in OrchestratorState)
# ---------------------------------------------------------------------------

_state: OrchestratorState = OrchestratorState(global_limit=GLOBAL_MAX_CONTAINERS)
_message_loop_running: bool = False

_gateways: dict[str, ChannelGateway] = {}
_queue: GroupQueue = GroupQueue()


@dataclass(frozen=True)
class _UsageFields:
    """Container for the six message-token columns.

    All fields are Optional because the metadata may be absent (legacy
    container, error path that didn't carry usage). Treat absent fields
    as NULL DB rows — distinct from "backend reported zero tokens".
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    model_id: str | None = None


def _extract_usage(metadata: dict[str, object] | None) -> _UsageFields:
    """Pull the wire-format ``usage`` payload into the DB-row shape.

    Parses the same wire format ``UsageSnapshot.from_metadata`` produces,
    but yields a ``_UsageFields`` record purpose-shaped for the DB write
    path: every field is Optional and None maps to a NULL column.
    UsageSnapshot defaults int tokens to 0, which would conflate
    "unknown" with "literally zero" in sum-of-tokens analytics.

    Trust boundary: this is the orchestrator's gate against malformed
    metadata coming off NATS. Wrong shape (string instead of dict,
    missing keys, garbage in optional fields) yields all-None
    _UsageFields rather than raising — a single rogue container must
    not be able to crash the message-storage path for sibling
    conversations.

    Note that ``cost_source`` from the wire is intentionally dropped
    here — there is no DB column for it. If/when one is added,
    extend ``_UsageFields`` and read it through.
    """
    if not isinstance(metadata, dict):
        return _UsageFields()
    usage = metadata.get("usage")
    if not isinstance(usage, dict):
        return _UsageFields()

    def _int(key: str) -> int | None:
        val = usage.get(key)
        return int(val) if isinstance(val, (int, float)) else None

    cost_raw = usage.get("cost_usd")
    cost = float(cost_raw) if isinstance(cost_raw, (int, float)) else None
    model_raw = usage.get("model_id")
    model = model_raw if isinstance(model_raw, str) else None
    return _UsageFields(
        input_tokens=_int("input_tokens"),
        output_tokens=_int("output_tokens"),
        cache_read_tokens=_int("cache_read_tokens"),
        cache_write_tokens=_int("cache_write_tokens"),
        cost_usd=cost,
        model_id=model,
    )


def _coworker_from_state(cw_state: CoworkerState) -> Coworker:
    """Build a full Coworker dataclass from runtime CoworkerState."""
    c = cw_state.config
    return Coworker(
        id=c.id,
        tenant_id=c.tenant_id,
        name=c.name,
        folder=c.folder,
        agent_backend=c.agent_backend,
        system_prompt=c.system_prompt,
        tools=c.tools,
        skills=c.skills,
        max_concurrent=c.max_concurrent,
    )


_transport: NatsTransport | None = None
_runtime: ContainerRuntime | None = None
_executor: ContainerAgentExecutor | None = None
_executors: dict[str, ContainerAgentExecutor] = {}
_bg_tasks: set[asyncio.Task[None]] = set()

# SafetyEngine is shared by the NATS safety_events subscriber (ingests
# container-produced audit events) and the orchestrator-side MODEL_OUTPUT
# pipeline (see _on_output below). Instantiated when the safety
# subscriber starts; kept None until then so tests that import this
# module without full startup don't pay for the default DbAuditSink.
_safety_engine: SafetyEngine | None = None

# V2 P0.3: orchestrator-side slow-check RPC. Thread pool caps concurrent
# sync check invocations so a heavy ML model can't starve the event
# loop. RPC server is started alongside the subscribers; both are kept
# at module scope so shutdown can tear them down cleanly.
_safety_rpc_server: object | None = None
_safety_thread_pool: object | None = None


def _handle_agent_message_ipc(data: dict[str, object]) -> None:
    """Handle one ``agent.*.messages`` NATS publish from the send_message tool.

    Current behaviour: log the attempt and drop. The send_message tool
    publishes to this subject when the agent calls it, but the tool
    hard-codes ``chatJid=ctx.chat_jid`` (``rolemesh_tools.py:169``)
    — meaning it can only ever target the agent's current conversation.
    That conversation's natural output (``agent.*.results`` → ``_on_output``)
    already delivers the final reply, so forwarding this IPC to the
    channel gateway a SECOND time resulted in duplicate user-visible
    replies. Previously a race-prone string-match dedup (``_ipc_sent_texts``)
    tried to suppress the duplicate; removing the forward here removes
    the duplicate at the source.

    If the send_message tool signature gains a target chat_jid parameter
    in the future (for cross-chat notifications / scheduled-task inbox /
    agent-to-agent messaging), this function grows a branch: forward to
    ``_send_via_coworker`` only when ``data["chatJid"] !=`` the source
    coworker's currently-processed conversation. Until then the handler
    is strictly log-and-drop.
    """
    if data.get("type") != "message":
        return
    if not data.get("chatJid") or not data.get("text"):
        return
    logger.info(
        "Dropped send_message IPC (redundant with natural output path)",
        chat_jid=data["chatJid"],
        source_group=data.get("groupFolder", ""),
        text_preview=str(data["text"])[:80],
    )


@dataclass(frozen=True)
class ModelOutputSafetyResult:
    """Outcome of ``_apply_model_output_safety``.

    Two terminal shapes:
      - ``text`` set, ``block`` is None: forward ``text`` to the user as
        an ordinary assistant reply. May be the original input (allow /
        warn) or a redacted copy (redact).
      - ``block`` set, ``text`` is None: forward through the dedicated
        safety-block channel instead of the assistant channel.
    """

    text: str | None = None
    block: tuple[str, str | None] | None = None  # (reason, rule_id)


async def _apply_model_output_safety(
    *,
    safety_engine: SafetyEngine | None,
    tenant_id: str,
    coworker_id: str,
    user_id: str,
    conversation_id: str,
    text: str,
) -> ModelOutputSafetyResult:
    """Run the MODEL_OUTPUT pipeline and return the decision.

    Split out of ``_on_output`` so it is reachable from unit tests
    without standing up the full conversation / gateway closure. The
    pipeline itself lives in ``rolemesh.safety.pipeline_core``; this
    wrapper is purely the "where does the text go on block / exception"
    policy that the orchestrator hot path applies.

    Return contract:

      - No engine / no text / no rules → text unchanged, no block.
      - Rule load failure → log, fail-open: text unchanged, no block.
      - Pipeline internal exception → fail-close: emit a generic block.
      - Block / require_approval verdict → emit a block with reason.
        require_approval is downgraded to block for the user-facing
        reply on MODEL_OUTPUT; the approval-request creation path is
        a separate concern.
      - Redact verdict → text substituted from
        ``verdict.modified_payload["text"]``.
      - Warn verdict → text unchanged (warn is audit-only at this
        stage since the reply is the final output).
      - Allow → text unchanged.
    """
    if not text or safety_engine is None:
        return ModelOutputSafetyResult(text=text)
    try:
        rules = await safety_engine.load_rules_for_coworker(
            tenant_id, coworker_id
        )
    except Exception:
        logger.exception(
            "safety: MODEL_OUTPUT rule load failed — skipping pipeline"
        )
        return ModelOutputSafetyResult(text=text)
    if not rules:
        return ModelOutputSafetyResult(text=text)

    from rolemesh.safety.types import SafetyContext, Stage

    ctx = SafetyContext(
        stage=Stage.MODEL_OUTPUT,
        tenant_id=tenant_id,
        coworker_id=coworker_id,
        user_id=user_id,
        job_id="",
        conversation_id=conversation_id,
        payload={"text": text},
    )
    try:
        verdict = await safety_engine.run_orchestrator_pipeline(ctx, rules)
    except Exception:
        logger.exception(
            "safety: MODEL_OUTPUT pipeline raised — failing closed"
        )
        return ModelOutputSafetyResult(
            block=("[Response blocked by safety policy]", None)
        )
    if verdict.action in ("block", "require_approval"):
        reason = verdict.reason or "[Response blocked by safety policy]"
        # Verdict at pipeline level doesn't carry rule_ids — the audit
        # path persists per-rule records to safety_decisions separately.
        # UI shows stage=model_output which is enough context.
        return ModelOutputSafetyResult(block=(reason, None))
    if verdict.action == "redact":
        modified = verdict.modified_payload or {}
        cleaned = (
            modified.get("text") if isinstance(modified, dict) else None
        )
        if isinstance(cleaned, str):
            return ModelOutputSafetyResult(text=cleaned)
        logger.warning(
            "safety: MODEL_OUTPUT redact without 'text' in modified_payload "
            "— falling back to original text",
            coworker_id=coworker_id,
        )
        return ModelOutputSafetyResult(text=text)
    return ModelOutputSafetyResult(text=text)


# ---------------------------------------------------------------------------
# State loading
# ---------------------------------------------------------------------------


async def _load_state() -> None:
    """Load all multi-tenant state from the database into OrchestratorState."""
    global _state
    _state = OrchestratorState(global_limit=GLOBAL_MAX_CONTAINERS)

    # Ensure default tenant exists
    default_tenant = await get_tenant_by_slug("default")
    if default_tenant is None:
        default_tenant = await create_tenant(name="Default Tenant", slug="default")
        logger.info("Created default tenant", tenant_id=default_tenant.id)

    # Load ALL tenants
    from rolemesh.db.pg import get_all_tenants

    for t in await get_all_tenants():
        _state.tenants[t.id] = t

    # Load all coworkers
    all_coworkers = await get_all_coworkers()
    all_bindings = await get_all_channel_bindings()
    all_conversations = await get_all_conversations()
    all_sessions = await get_all_sessions()

    # Index bindings and conversations
    bindings_by_coworker: dict[str, list[ChannelBinding]] = {}
    for b in all_bindings:
        bindings_by_coworker.setdefault(b.coworker_id, []).append(b)

    convs_by_coworker: dict[str, list[Conversation]] = {}
    for c in all_conversations:
        convs_by_coworker.setdefault(c.coworker_id, []).append(c)

    for cw in all_coworkers:
        config = CoworkerConfig(
            id=cw.id,
            tenant_id=cw.tenant_id,
            name=cw.name,
            folder=cw.folder,
            system_prompt=cw.system_prompt,
            trigger_pattern=CoworkerConfig.build_trigger_pattern(cw.name),
            agent_backend=cw.agent_backend,
            container_image=None,
            max_concurrent=cw.max_concurrent,
            tools=cw.tools,
            skills=cw.skills,
            agent_role=cw.agent_role,
            permissions=cw.permissions,
        )

        cw_state = CoworkerState(config=config)

        # Load channel bindings
        for b in bindings_by_coworker.get(cw.id, []):
            cw_state.channel_bindings[b.channel_type] = b

        # Load conversations (keyed by conversation ID, not chat_id,
        # because the same chat_id may appear under different bindings)
        for conv in convs_by_coworker.get(cw.id, []):
            session_id = all_sessions.get(conv.id)
            cw_state.conversations[conv.id] = ConversationState(
                conversation=conv,
                session_id=session_id,
                last_agent_timestamp=conv.last_agent_invocation or "",
            )

        _state.coworkers[cw.id] = cw_state

    # Register MCP servers with the credential proxy
    for cw in all_coworkers:
        for tool_cfg in cw.tools:
            parsed = urlparse(tool_cfg.url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            register_mcp_server(tool_cfg.name, origin, tool_cfg.headers, tool_cfg.auth_mode)

    logger.info(
        "State loaded",
        tenant_count=len(_state.tenants),
        coworker_count=len(_state.coworkers),
    )


# ---------------------------------------------------------------------------
# Message handling callback (from gateways)
# ---------------------------------------------------------------------------


async def _auto_create_web_conversation(
    binding_id: str, chat_id: str
) -> tuple[CoworkerState, ConversationState] | None:
    """Auto-create a conversation for web channel (each browser tab gets a new chat_id)."""
    # Find the coworker that owns this binding
    for cw in _state.coworkers.values():
        for b in cw.channel_bindings.values():
            if b.id == binding_id and b.channel_type == "web":
                # ws.py may have already created the conversation before the
                # NATS message reaches the orchestrator. Check DB first to
                # avoid a UniqueViolationError on (binding_id, chat_id).
                conv = await get_conversation_by_binding_and_chat(binding_id, chat_id)
                if conv is None:
                    conv = await create_conversation(
                        tenant_id=cw.config.tenant_id,
                        coworker_id=cw.config.id,
                        channel_binding_id=binding_id,
                        channel_chat_id=chat_id,
                        name=f"Web Chat {chat_id[:8]}",
                        requires_trigger=False,
                        user_id=None,
                    )
                conv_state = ConversationState(conversation=conv)
                cw.conversations[conv.id] = conv_state
                logger.info(
                    "Auto-created web conversation",
                    coworker=cw.config.name,
                    chat_id=chat_id,
                    conversation_id=conv.id,
                )
                return cw, conv_state
    return None


async def _emit_status_for_conversation(conversation_id: str, payload: dict[str, object]) -> None:
    """Route a progress-status payload to the web gateway for this conversation.

    Non-web channels silently ignore status events — progress reporting is a
    WebUI-only feature.
    """
    found = _state.get_conversation(conversation_id)
    if not found:
        return
    cw_state, conv_state = found
    conv = conv_state.conversation
    channel_type = _get_channel_type_for_conv(cw_state, conv)
    binding = cw_state.channel_bindings.get(channel_type)
    gw = _gateways.get(channel_type) if binding else None
    if binding and isinstance(gw, WebNatsGateway):
        try:
            await gw.send_status(binding.id, conv.channel_chat_id, payload)
        except (OSError, RuntimeError):
            logger.debug("send_status failed", conversation_id=conversation_id, exc_info=True)


async def _emit_queued_status(conversation_id: str) -> None:
    await _emit_status_for_conversation(conversation_id, {"status": "queued"})


async def _emit_container_starting_status(conversation_id: str) -> None:
    await _emit_status_for_conversation(conversation_id, {"status": "container_starting"})


async def _handle_web_stop(binding_id: str, chat_id: str) -> None:
    """User clicked Stop in the WebUI. Interrupt current turn, keep container alive.

    Resolves the web binding+chat to a conversation, then asks the scheduler
    to send an interrupt signal to the active agent container.
    """
    # The gateway already logged "Web stop received" at info level for ops
    # visibility. Keep the internal routing at debug level.
    logger.debug("handle_web_stop", binding_id=binding_id, chat_id=chat_id)
    result = _state.find_conversation_by_binding_and_chat(binding_id, chat_id)
    if result is None:
        logger.warning("Stop received for unknown binding/chat", binding_id=binding_id, chat_id=chat_id)
        return
    _, conv_state = result
    conv = conv_state.conversation
    _queue.interrupt_current_turn(conv.id)


async def _handle_incoming(
    binding_id: str,
    chat_id: str,
    sender: str,
    sender_name: str,
    text: str,
    timestamp: str,
    msg_id: str,
    is_group: bool,
) -> None:
    """Unified message handler for all channel gateways."""
    # Find conversation
    result = _state.find_conversation_by_binding_and_chat(binding_id, chat_id)
    if not result:
        # Auto-create conversation for web channel (each browser tab = new chat_id)
        result = await _auto_create_web_conversation(binding_id, chat_id)
        if not result:
            return

    cw_state, conv_state = result
    conv = conv_state.conversation

    # In groups with multiple bots, each bot receives all messages.
    # Only store the message if it's relevant to THIS coworker:
    # - conversation doesn't require trigger (DM or admin), OR
    # - message content matches this coworker's trigger pattern
    if is_group and conv.requires_trigger and not cw_state.config.trigger_pattern.search(text.strip()):
        return  # Not for this coworker — skip silently

    # Sender allowlist check
    cfg = load_sender_allowlist()
    if should_drop_message(chat_id, cfg) and not is_sender_allowed(chat_id, sender, cfg):
        if cfg.log_denied:
            logger.debug("sender-allowlist: dropping message (drop mode)", chat_id=chat_id, sender=sender)
        return

    # Store message
    await db_store_message(
        tenant_id=conv.tenant_id,
        conversation_id=conv.id,
        msg_id=msg_id,
        sender=sender,
        sender_name=sender_name,
        content=text,
        timestamp=timestamp,
    )

    # Immediately enqueue processing for this conversation
    # (don't wait for the message loop to discover it via polling)
    _queue.enqueue_message_check(
        conv.id,
        tenant_id=conv.tenant_id,
        coworker_id=conv.coworker_id,
    )


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------


async def _process_conversation_messages(conversation_id: str) -> bool:
    """Process all pending messages for a conversation (identified by conversation_id)."""
    found = _state.get_conversation(conversation_id)
    if not found:
        return True

    cw_state, conv_state = found
    conv = conv_state.conversation
    config = cw_state.config

    since_timestamp = conv_state.last_agent_timestamp
    missed_messages = await get_messages_since(
        conv.tenant_id, conv.id, since_timestamp, config.name, chat_jid=conv.channel_chat_id
    )

    if not missed_messages:
        return True

    if config.agent_role != "super_agent" and conv.requires_trigger:
        allowlist_cfg = load_sender_allowlist()
        has_trigger = any(
            config.trigger_pattern.search(m.content.strip())
            and (m.is_from_me or is_trigger_allowed(conv.channel_chat_id, m.sender, allowlist_cfg))
            for m in missed_messages
        )
        if not has_trigger:
            return True

    prompt = format_messages(missed_messages, TIMEZONE)

    previous_cursor = conv_state.last_agent_timestamp
    conv_state.last_agent_timestamp = missed_messages[-1].timestamp
    await update_conversation_last_invocation(conv.id, missed_messages[-1].timestamp)

    logger.info("Processing messages", coworker=config.name, message_count=len(missed_messages))

    idle_handle: asyncio.TimerHandle | None = None

    def _reset_idle_timer() -> None:
        nonlocal idle_handle
        if idle_handle is not None:
            idle_handle.cancel()
        loop = asyncio.get_running_loop()
        idle_handle = loop.call_later(
            IDLE_TIMEOUT / 1000.0,
            lambda: _queue.request_shutdown(conversation_id),
        )

    # Set typing
    channel_type = _get_channel_type_for_conv(cw_state, conv)
    binding = cw_state.channel_bindings.get(channel_type)
    gw = _gateways.get(channel_type) if binding else None
    if binding and gw:
        with contextlib.suppress(OSError, RuntimeError, TypeError, ValueError):
            await gw.set_typing(binding.id, conv.channel_chat_id, True)

    had_error = False
    output_sent_to_user = False

    async def _on_output(result: AgentOutput) -> None:
        nonlocal had_error, output_sent_to_user
        # Progress events (running / tool_use / queued / container_starting)
        # are transient UX indicators — route to web gateway as status and
        # early-return. Don't touch idle timer or notify_idle.
        if result.is_progress():
            if binding and isinstance(gw, WebNatsGateway):
                payload: dict[str, object] = {"status": result.status}
                if result.metadata:
                    payload.update(result.metadata)
                try:
                    await gw.send_status(binding.id, conv.channel_chat_id, payload)
                except (OSError, RuntimeError):
                    logger.debug("send_status failed", conversation_id=conv.id, exc_info=True)
            return

        # Safety-block events go on a dedicated channel so the UI can
        # distinguish them from ordinary assistant replies. Crucially we
        # do NOT write to the messages table — blocks are already
        # audited in safety_decisions, and storing them as is_from_me
        # messages would pollute conversation history and confuse LLM
        # context reconstruction. send_stream_done still fires so the
        # client exits the 'streaming' state.
        if result.status == "safety_blocked":
            reason = result.result or "Blocked by safety policy"
            meta = result.metadata or {}
            stage = str(meta.get("stage", "unknown"))
            rule_id = meta.get("rule_id")
            logger.info(
                "Safety-block event",
                coworker=config.name,
                stage=stage,
                rule_id=rule_id,
                chars=len(reason),
            )
            if binding and isinstance(gw, WebNatsGateway):
                with contextlib.suppress(OSError, RuntimeError, TypeError, ValueError):
                    await gw.send_safety_block(
                        binding.id,
                        conv.channel_chat_id,
                        reason=reason,
                        stage=stage,
                        rule_id=str(rule_id) if isinstance(rule_id, str) else None,
                    )
                with contextlib.suppress(OSError, RuntimeError, TypeError, ValueError):
                    await gw.send_stream_done(binding.id, conv.channel_chat_id)
            output_sent_to_user = True
            _reset_idle_timer()
            if result.is_final:
                _queue.notify_idle(conversation_id)
            return
        if result.result:
            raw = result.result
            text = re.sub(r"<internal>[\s\S]*?</internal>", "", raw).strip()
            logger.info("Agent output", coworker=config.name, chars=len(raw))

            # V2 P0.1: orchestrator-side MODEL_OUTPUT safety pipeline.
            # Runs on the user-visible text (internal tags already stripped).
            safety_result = await _apply_model_output_safety(
                safety_engine=_safety_engine,
                tenant_id=config.tenant_id,
                coworker_id=config.id,
                user_id=conv.user_id or "",
                conversation_id=conv.id,
                text=text,
            )

            if safety_result.block is not None:
                reason, rule_id = safety_result.block
                logger.info(
                    "Safety-block (model_output)",
                    coworker=config.name,
                    rule_id=rule_id,
                    chars=len(reason),
                )
                if binding and isinstance(gw, WebNatsGateway):
                    with contextlib.suppress(OSError, RuntimeError, TypeError, ValueError):
                        await gw.send_safety_block(
                            binding.id,
                            conv.channel_chat_id,
                            reason=reason,
                            stage="model_output",
                            rule_id=rule_id,
                        )
                output_sent_to_user = True
                _reset_idle_timer()
            elif safety_result.text and binding:
                text = safety_result.text
                if isinstance(gw, WebNatsGateway):
                    await gw.send_stream_chunk(binding.id, conv.channel_chat_id, text)
                    # Pull token usage off the wire metadata for persistence.
                    # The container side (agent_runner.main on_event) puts
                    # the snapshot under metadata["usage"] using the
                    # UsageSnapshot.to_metadata wire format. Unknown / older
                    # containers leave it absent and all six DB columns
                    # stay NULL. Cost arrives as float; asyncpg coerces to
                    # NUMERIC(10,6) without us needing a Decimal.
                    usage = _extract_usage(result.metadata)
                    # Store assistant response for web history
                    await db_store_message(
                        tenant_id=conv.tenant_id,
                        conversation_id=conv.id,
                        msg_id=str(uuid.uuid4()),
                        sender=config.name,
                        sender_name=config.name,
                        content=text,
                        timestamp=datetime.now(UTC).isoformat(),
                        is_from_me=True,
                        is_bot_message=True,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cache_read_tokens=usage.cache_read_tokens,
                        cache_write_tokens=usage.cache_write_tokens,
                        cost_usd=usage.cost_usd,
                        model_id=usage.model_id,
                    )
                elif gw:
                    await gw.send_message(binding.id, conv.channel_chat_id, text)
                output_sent_to_user = True
                _reset_idle_timer()
            else:
                _reset_idle_timer()
        if result.status == "success":
            # Send stream done immediately for web channel (don't wait for
            # _run_agent to return — the container stays alive until idle timeout).
            # send_stream_done fires per success so a followed-up batch can
            # finalize each reply bubble separately.
            if binding and isinstance(gw, WebNatsGateway):
                with contextlib.suppress(OSError, RuntimeError, TypeError, ValueError):
                    await gw.send_stream_done(binding.id, conv.channel_chat_id)
            # Only release idle-gating once the whole batch is done. When a
            # user queued a follow-up during the active turn, the container
            # emits one success per answered message; all but the last carry
            # is_final=False. Calling notify_idle mid-batch would mark the
            # container idle while it is still processing queued messages and
            # can race with scheduled-task preemption.
            if result.is_final:
                _queue.notify_idle(conversation_id)
        if result.status == "stopped":
            # User-initiated stop. Forward a status frame so the UI exits
            # the transitional 'stopping' state, then emit done to close
            # the stream. Container stays alive for follow-up prompts.
            if binding and isinstance(gw, WebNatsGateway):
                with contextlib.suppress(OSError, RuntimeError, TypeError, ValueError):
                    await gw.send_status(
                        binding.id,
                        conv.channel_chat_id,
                        {"status": "stopped"},
                    )
                    await gw.send_stream_done(binding.id, conv.channel_chat_id)
            _queue.notify_idle(conversation_id)
        if result.status == "error":
            had_error = True

    output = await _run_agent(cw_state, conv_state, prompt, _on_output)

    # Stop typing
    if binding and gw:
        with contextlib.suppress(OSError, RuntimeError, TypeError, ValueError):
            await gw.set_typing(binding.id, conv.channel_chat_id, False)
    if idle_handle is not None:
        idle_handle.cancel()

    if output == "error" or had_error:
        if output_sent_to_user:
            logger.warning(
                "Agent error after output was sent, skipping cursor rollback",
                coworker=config.name,
            )
            return True
        conv_state.last_agent_timestamp = previous_cursor
        await update_conversation_last_invocation(conv.id, previous_cursor)
        logger.warning("Agent error, rolled back message cursor for retry", coworker=config.name)
        return False

    return True


def _get_channel_type_for_chat(chat_id: str) -> str:
    """Infer channel type from chat ID format (fallback heuristic)."""
    if chat_id.startswith("C") or chat_id.startswith("D"):
        return "slack"
    return "telegram"


def _get_channel_type_for_conv(cw_state: CoworkerState, conv: Conversation) -> str:
    """Determine channel type for a conversation by looking up its binding."""
    for channel_type, binding in cw_state.channel_bindings.items():
        if binding.id == conv.channel_binding_id:
            return channel_type
    return _get_channel_type_for_chat(conv.channel_chat_id)


async def _run_agent(
    cw_state: CoworkerState,
    conv_state: ConversationState,
    prompt: str,
    on_output: Callable[[AgentOutput], Awaitable[None]] | None = None,
) -> str:
    """Run agent in a container. Returns 'success' or 'error'."""
    config = cw_state.config
    conv = conv_state.conversation
    permissions = config.permissions
    session_id = conv_state.session_id

    if _transport is not None:
        tasks = await get_all_tasks(config.tenant_id)
        await write_tasks_snapshot(
            _transport,
            config.tenant_id,
            config.folder,
            permissions,
            [
                {
                    "id": t.id,
                    "coworkerFolder": config.folder,
                    "prompt": t.prompt,
                    "schedule_type": t.schedule_type,
                    "schedule_value": t.schedule_value,
                    "status": t.status,
                    "next_run": t.next_run,
                }
                for t in tasks
            ],
        )

    # Select executor based on coworker's backend setting
    executor = _executors.get(config.agent_backend) if _executors else None
    if executor is None and _executors:
        logger.warning(
            "Unknown agent_backend=%r for coworker %s, using default",
            config.agent_backend, config.name,
        )
        executor = _executor
    if executor is None:
        logger.error("Agent executor not initialized")
        return "error"

    wrapped_on_output = None
    if on_output is not None:
        original_on_output = on_output

        async def _wrapped(output: AgentOutput) -> None:
            if output.new_session_id:
                conv_state.session_id = output.new_session_id
                await set_session(conv.id, conv.tenant_id, conv.coworker_id, output.new_session_id)
            await original_on_output(output)

        wrapped_on_output = _wrapped

    try:
        output = await executor.execute(
            AgentInput(
                prompt=prompt,
                session_id=session_id,
                group_folder=config.folder,
                chat_jid=conv.channel_chat_id,
                permissions=permissions.to_dict(),
                user_id=conv.user_id or "",
                assistant_name=config.name,
                system_prompt=config.system_prompt,
                tenant_id=config.tenant_id,
                coworker_id=config.id,
                conversation_id=conv.id,
            ),
            lambda container_name, job_id: _queue.register_process(
                conv.id, container_name, config.folder, job_id
            ),
            wrapped_on_output,
        )

        if output.new_session_id:
            conv_state.session_id = output.new_session_id
            await set_session(conv.id, conv.tenant_id, conv.coworker_id, output.new_session_id)

        if output.status == "error":
            logger.error("Container agent error", coworker=config.name, error=output.error)
            return "error"

        return "success"
    except (OSError, RuntimeError, TypeError, ValueError):
        logger.exception("Agent error", coworker=config.name)
        return "error"


# ---------------------------------------------------------------------------
# NATS IPC subscriptions
# ---------------------------------------------------------------------------


async def _start_nats_ipc_subscriptions(transport: NatsTransport, deps: _IpcDepsImpl) -> list[asyncio.Task[None]]:
    """Subscribe to NATS subjects for agent IPC messages and tasks."""
    tasks: list[asyncio.Task[None]] = []

    # Clean up stale durable consumers and purge old messages on startup
    for consumer_name in ("orch-messages", "orch-tasks"):
        with contextlib.suppress(Exception):
            await transport.js.delete_consumer("agent-ipc", consumer_name)
    with contextlib.suppress(Exception):
        await transport.js.purge_stream("agent-ipc")

    messages_sub = await transport.js.subscribe("agent.*.messages", durable="orch-messages")

    async def _handle_messages() -> None:
        async for msg in messages_sub.messages:
            try:
                _handle_agent_message_ipc(json.loads(msg.data))
                await msg.ack()
            except Exception:
                logger.exception("Error processing NATS IPC message")
                await msg.ack()

    tasks.append(asyncio.create_task(_handle_messages()))

    tasks_sub = await transport.js.subscribe("agent.*.tasks", durable="orch-tasks")

    async def _handle_tasks() -> None:
        async for msg in tasks_sub.messages:
            try:
                data = json.loads(msg.data)
                source_group = data.get("groupFolder", data.get("createdBy", ""))
                claimed_coworker_id = data.get("coworkerId", "")

                # Determine the AUTHORITATIVE (tenant_id, coworker_id) from
                # the orchestrator's in-memory state, NOT from the NATS
                # payload. Claimed tenantId in the message body is a hint
                # only; if the coworker resolves to a different tenant, we
                # override with the server-side truth.
                source_cw = (
                    _state.coworkers.get(claimed_coworker_id)
                    if claimed_coworker_id
                    else None
                )
                if source_cw is None and source_group:
                    for tenant in _state.tenants.values():
                        source_cw = _state.get_coworker_by_folder(tenant.id, source_group)
                        if source_cw:
                            break
                if source_cw is not None:
                    source_tenant_id = source_cw.config.tenant_id
                    source_coworker_id = source_cw.config.id
                else:
                    source_tenant_id = data.get("tenantId", DEFAULT_TENANT)
                    source_coworker_id = claimed_coworker_id
                permissions = source_cw.config.permissions if source_cw else AgentPermissions()

                await process_task_ipc(
                    data,
                    source_group,
                    permissions,
                    deps,
                    tenant_id=source_tenant_id,
                    coworker_id=source_coworker_id,
                )
                await msg.ack()
            except Exception:
                logger.exception("Error processing NATS IPC task")
                await msg.ack()

    tasks.append(asyncio.create_task(_handle_tasks()))

    # Safety Framework event ingestion. Container-side SafetyHookHandler
    # publishes audit events to agent.*.safety_events; the subscriber
    # performs a trusted-tenant lookup (same pattern as _handle_tasks
    # above) and forwards validated payloads to SafetyEngine which
    # writes to safety_decisions. Without this subscription the safety
    # decisions published by containers are silently lost — a gap that
    # existed in V1's initial shipment.
    for consumer_name in ("orch-safety-events",):
        with contextlib.suppress(Exception):
            await transport.js.delete_consumer("agent-ipc", consumer_name)
    safety_events_sub = await transport.js.subscribe(
        "agent.*.safety_events", durable="orch-safety-events"
    )

    from rolemesh.safety.engine import SafetyEngine
    from rolemesh.safety.subscriber import SafetyEventsSubscriber

    class _StateCoworkerLookup:
        """Adapter: claimed coworker_id -> (tenant_id, id) from _state."""

        def __call__(
            self, claimed_coworker_id: str
        ) -> _TrustedCoworkerRec | None:
            cw = _state.coworkers.get(claimed_coworker_id)
            if cw is None:
                return None
            return _TrustedCoworkerRec(
                tenant_id=cw.config.tenant_id, id=cw.config.id
            )

    global _safety_engine
    _safety_engine = SafetyEngine()
    safety_subscriber = SafetyEventsSubscriber(
        engine=_safety_engine,
        coworker_lookup=_StateCoworkerLookup(),
    )

    async def _handle_safety_events() -> None:
        async for msg in safety_events_sub.messages:
            try:
                await safety_subscriber.on_message_bytes(msg.data)
            except Exception:
                # Subscriber must not poison its own loop. Individual
                # malformed/suspicious messages already surface via
                # structured warnings inside on_message_bytes.
                logger.exception("Error processing safety_events message")
            await msg.ack()

    tasks.append(asyncio.create_task(_handle_safety_events()))

    # V2 P0.3: Slow-check RPC server (agent.*.safety.detect). Uses core
    # NATS request-reply rather than JetStream — slow checks are
    # synchronous from the container's perspective, so a missed reply
    # should surface as a timeout + fail-open on the caller, not be
    # persisted and re-delivered. A shared ThreadPoolExecutor absorbs
    # sync ML libraries so they can't stall the orchestrator loop.
    from concurrent.futures import ThreadPoolExecutor
    from os import cpu_count

    from rolemesh.safety.registry import get_orchestrator_registry
    from rolemesh.safety.rpc_server import SafetyRpcServer

    max_workers = max(4, int((cpu_count() or 4) * 1.5))

    global _safety_thread_pool, _safety_rpc_server
    _safety_thread_pool = ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="safety-rpc"
    )
    _safety_rpc_server = SafetyRpcServer(
        nats_client=transport.nc,
        registry=get_orchestrator_registry(),
        thread_pool=_safety_thread_pool,
        coworker_lookup=_StateCoworkerLookup(),
    )
    await _safety_rpc_server.start()
    logger.info(
        "safety RPC server started",
        component="safety",
        max_workers=max_workers,
    )

    logger.info("NATS IPC subscriptions started")
    return tasks


# ---------------------------------------------------------------------------
# Message loop
# ---------------------------------------------------------------------------


async def _message_loop(shutdown_event: asyncio.Event) -> None:
    """Main polling loop that detects new messages and dispatches them."""
    global _message_loop_running

    if _message_loop_running:
        return
    _message_loop_running = True

    logger.info("RoleMesh running (multi-tenant)")

    # Per-tenant message cursors
    last_timestamps: dict[str, str] = {}
    for t in _state.tenants.values():
        last_timestamps[t.id] = t.last_message_cursor or ""

    while not shutdown_event.is_set():
        try:
            # Collect conversations grouped by tenant
            convs_by_tenant: dict[str, list[str]] = {}
            conv_lookup: dict[str, tuple[CoworkerState, ConversationState]] = {}
            for cw in _state.coworkers.values():
                for cs in cw.conversations.values():
                    tid = cs.conversation.tenant_id
                    convs_by_tenant.setdefault(tid, []).append(cs.conversation.id)
                    conv_lookup[cs.conversation.id] = (cw, cs)

            # Query each tenant's messages
            results: list[tuple[str, NewMessage]] = []
            for tid, conv_ids in convs_by_tenant.items():
                last_ts = last_timestamps.get(tid, "")
                tenant_results = await get_new_messages_for_conversations(tid, conv_ids, last_ts, ASSISTANT_NAME)
                if tenant_results:
                    results.extend(tenant_results)
                    new_ts = max(msg.timestamp for _, msg in tenant_results)
                    if new_ts > last_ts:
                        last_timestamps[tid] = new_ts
                        await update_tenant_message_cursor(tid, new_ts)

            if results:
                logger.info("New messages", count=len(results))

                # Group by conversation
                by_conv: dict[str, list[tuple[CoworkerState, ConversationState]]] = {}
                for conv_id, _msg in results:
                    if conv_id not in by_conv:
                        by_conv[conv_id] = []
                    if conv_id in conv_lookup:
                        by_conv[conv_id] = [conv_lookup[conv_id]]

                for conv_id, entries in by_conv.items():
                    if not entries:
                        continue
                    cw_state, conv_state = entries[0]
                    config = cw_state.config
                    conv = conv_state.conversation
                    chat_id = conv.channel_chat_id

                    needs_trigger = config.agent_role != "super_agent" and conv.requires_trigger

                    if needs_trigger:
                        conv_messages = [msg for cid, msg in results if cid == conv_id]
                        allowlist_cfg = load_sender_allowlist()
                        has_trigger = any(
                            config.trigger_pattern.search(m.content.strip())
                            and (m.is_from_me or is_trigger_allowed(chat_id, m.sender, allowlist_cfg))
                            for m in conv_messages
                        )
                        if not has_trigger:
                            continue

                    # Try piping to active container first
                    all_pending = await get_messages_since(
                        conv.tenant_id,
                        conv.id,
                        conv_state.last_agent_timestamp,
                        config.name,
                        chat_jid=chat_id,
                    )
                    if all_pending:
                        formatted = format_messages(all_pending, TIMEZONE)
                        if _queue.send_message(conv_id, formatted):
                            logger.debug("Piped messages to active container", conv_id=conv_id)
                            conv_state.last_agent_timestamp = all_pending[-1].timestamp
                            await update_conversation_last_invocation(conv.id, all_pending[-1].timestamp)

                            ch_type = _get_channel_type_for_conv(cw_state, conv)
                            binding = cw_state.channel_bindings.get(ch_type)
                            if binding:
                                gw = _gateways.get(ch_type)
                                if gw:
                                    with contextlib.suppress(OSError, RuntimeError, TypeError, ValueError):
                                        await gw.set_typing(binding.id, chat_id, True)
                        else:
                            _queue.enqueue_message_check(
                                conv_id,
                                tenant_id=config.tenant_id,
                                coworker_id=config.id,
                            )
        except (OSError, RuntimeError, TypeError, ValueError):
            logger.exception("Error in message loop")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=POLL_INTERVAL)
            break
        except TimeoutError:
            pass


async def _recover_pending_messages() -> None:
    """Startup recovery: check for unprocessed messages."""
    for cw in _state.coworkers.values():
        for conv_state in cw.conversations.values():
            conv = conv_state.conversation
            since = conv_state.last_agent_timestamp
            pending = await get_messages_since(
                conv.tenant_id, conv.id, since, cw.config.name, chat_jid=conv.channel_chat_id
            )
            if pending:
                logger.info(
                    "Recovery: found unprocessed messages",
                    coworker=cw.config.name,
                    chat_id=conv.channel_chat_id,
                    pending_count=len(pending),
                )
                _queue.enqueue_message_check(
                    conv.id,
                    tenant_id=cw.config.tenant_id,
                    coworker_id=cw.config.id,
                )


async def _ensure_container_system_running() -> None:
    """Prepare the container runtime and bridge networks.

    Gateway launch used to live here too, but the gateway's first
    startup step is a NATS request-reply for the rule snapshot — that
    only gets a responder once ``start_responders`` runs later in
    ``main()``. Launching the gateway this early produced a chicken-
    and-egg: gateway crash-loops on ``NoRespondersError`` while the
    orchestrator is still blocked on its readiness probe. Gateway
    launch is now ``_launch_egress_gateway_once_ready`` and runs after
    the responders are registered.

    Order here is just:
      1. get_runtime() + ensure_available() — dockerd version gate
      2. ensure_agent_network() — creates the agent bridge with
         ``Internal=true``; physically removes the default route.
      3. ensure_egress_network() — creates the outbound bridge.
      4. cleanup_orphans() — safe to run before the gateway exists;
         operates only on containers we labeled.

    Fail-closed throughout: any step raising makes the orchestrator
    refuse to enter the ready state.
    """
    global _runtime
    _runtime = get_runtime()
    await _runtime.ensure_available()
    if hasattr(_runtime, "ensure_agent_network"):
        await _runtime.ensure_agent_network(CONTAINER_NETWORK_NAME)
    # egress-net only exists to carry the gateway's outbound; if EC is
    # turned off there's no gateway to attach, so skip the bridge too.
    if CONTAINER_NETWORK_NAME and hasattr(_runtime, "ensure_egress_network"):
        await _runtime.ensure_egress_network(CONTAINER_EGRESS_NETWORK_NAME)

    await _runtime.cleanup_orphans("rolemesh-")


async def _launch_egress_gateway_once_ready() -> None:
    """Start the egress-gateway container and wait for it to be ready.

    Must be called AFTER ``start_responders`` has registered the NATS
    rule-snapshot / identity-snapshot responders. The gateway's first
    action on boot is a request-reply for the snapshot; without a
    responder it fails-closed and Docker restart-loops the container.

    Gated on:
      * ``CONTAINER_NETWORK_NAME`` non-empty — operators disable EC
        with ``CONTAINER_NETWORK_NAME=""`` (rollback mode).
      * ``hasattr(_runtime, ...)`` — k8s runtime will grow its own
        gateway pod primitive and should not use this path.
    """
    if (
        CONTAINER_NETWORK_NAME
        and hasattr(_runtime, "ensure_egress_network")
        and hasattr(_runtime, "verify_egress_gateway_reachable")
    ):
        from rolemesh.container.runner import set_egress_gateway_dns_ip
        from rolemesh.egress.launcher import launch_egress_gateway, wait_for_gateway_ready

        # Access the aiodocker client directly via the Docker runtime's
        # adapter surface. For the k8s backend we'll wrap this differently.
        docker_client = _runtime._ensure_client()  # type: ignore[attr-defined]
        await launch_egress_gateway(
            docker_client,
            agent_network=CONTAINER_NETWORK_NAME,
            egress_network=CONTAINER_EGRESS_NETWORK_NAME,
        )
        await wait_for_gateway_ready(
            docker_client,
            agent_network=CONTAINER_NETWORK_NAME,
            gateway_service_name=EGRESS_GATEWAY_CONTAINER_NAME,
            reverse_proxy_port=CREDENTIAL_PROXY_PORT,
        )

        # Discover the gateway's bridge IP on the agent network and
        # register it so runner.build_container_spec can pin it as
        # each agent container's DNS resolver. Without this step the
        # authoritative DNS resolver built in EC-2 never sees agent
        # traffic — queries go through Docker's embedded resolver
        # (127.0.0.11) which forwards to the host. See EC-2 code
        # review P1 finding.
        gateway_container = docker_client.containers.container(
            EGRESS_GATEWAY_CONTAINER_NAME
        )
        gateway_info = await gateway_container.show()
        gateway_ip = (
            gateway_info.get("NetworkSettings", {})
            .get("Networks", {})
            .get(CONTAINER_NETWORK_NAME, {})
            .get("IPAddress", "")
        )
        if gateway_ip:
            set_egress_gateway_dns_ip(gateway_ip)
        else:
            # Gateway is reachable by name (we just verified /healthz)
            # but doesn't expose an IPAddress on agent-net in inspect
            # output. Shouldn't happen in practice with our topology;
            # log as an error because it means agent DNS will silently
            # fall back to the embedded resolver.
            logger.error(
                "Gateway healthy but its agent-net IP is missing from "
                "inspect output — agents will fall back to Docker DNS "
                "and the authoritative resolver will not see their queries",
                gateway=EGRESS_GATEWAY_CONTAINER_NAME,
                agent_network=CONTAINER_NETWORK_NAME,
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    """Entry point for the RoleMesh orchestrator."""
    global _transport, _queue, _runtime, _executor, _executors, _gateways

    await _ensure_container_system_running()
    await init_database()
    logger.info("Database initialized")
    await _load_state()
    restore_remote_control()

    _transport = NatsTransport(NATS_URL)
    try:
        await _transport.connect()
    except ConnectionError:
        logger.critical(
            "Failed to connect to NATS -- is it running?",
            url=NATS_URL,
            hint="docker compose -f docker-compose.dev.yml up -d",
        )
        sys.exit(1)

    assert _runtime is not None

    def _get_coworker(coworker_id: str) -> Coworker | None:
        cw = _state.coworkers.get(coworker_id)
        return _coworker_from_state(cw) if cw else None

    # Build one executor per unique config, then add aliases.
    _unique_configs = [CLAUDE_CODE_BACKEND, PI_BACKEND]
    for cfg in _unique_configs:
        _executors[cfg.name] = ContainerAgentExecutor(cfg, _runtime, _transport, _get_coworker)
    # Add aliases so legacy DB values (e.g. "claude-code") resolve correctly.
    for alias, cfg in BACKEND_CONFIGS.items():
        if alias not in _executors:
            _executors[alias] = _executors[cfg.name]

    if AGENT_BACKEND_DEFAULT not in _executors:
        logger.warning("Unknown ROLEMESH_AGENT_BACKEND=%r, falling back to 'claude'", AGENT_BACKEND_DEFAULT)
    _executor = _executors.get(AGENT_BACKEND_DEFAULT, _executors["claude"])

    _queue = GroupQueue(transport=_transport, runtime=_runtime, orchestrator_state=_state)

    # Host-side credential proxy: kept running for backward compatibility
    # with operator tooling and so the `register_mcp_server` /
    # `set_token_vault` wiring below has a sink to write to. Agents no
    # longer reach this listener (the EC-1 agent bridge is Internal=true
    # and agents resolve ``egress-gateway`` via Docker DNS instead), but
    # the in-process dicts are still authoritative state that EC-2 will
    # propagate into the gateway container. Leaving the host-side
    # listener bound is the simplest compatibility shim during the PR-1
    # → PR-2 transition; EC-2 removes this line entirely.
    proxy_runner = await start_credential_proxy(CREDENTIAL_PROXY_PORT, PROXY_BIND_HOST)

    # Gateway reachability is already enforced in
    # _ensure_container_system_running() via wait_for_gateway_ready. No
    # second probe is needed at this point — keeping one here would
    # double the startup latency for no diagnostic gain.

    # Initialize TokenVault for per-user MCP token forwarding (OIDC mode only).
    # Note: this only sets the vault in THIS process. The WebUI runs in a
    # separate process and must initialize its own vault from env (see
    # src/webui/main.py lifespan).
    from rolemesh.auth.token_vault import create_vault_from_env

    _vault = await create_vault_from_env()
    if _vault is not None:
        set_token_vault(_vault)
        logger.info("TokenVault initialized for credential proxy")

    shutdown_event = asyncio.Event()

    def _signal_handler(sig_name: str) -> None:
        logger.info("Shutdown signal received", signal=sig_name)
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler, sig.name)

    # Initialize gateways and add bindings
    _web_gw = WebNatsGateway(on_message=_handle_incoming, transport=_transport)
    _gateways = {
        "telegram": TelegramGateway(on_message=_handle_incoming),
        "slack": SlackGateway(on_message=_handle_incoming),
        "web": _web_gw,
    }

    # Add channel bindings to gateways
    for cw in _state.coworkers.values():
        for channel_type, binding in cw.channel_bindings.items():
            gw = _gateways.get(channel_type)
            if gw:
                try:
                    await gw.add_binding(binding)
                except Exception:
                    logger.exception("Failed to add binding", binding_id=binding.id, channel_type=channel_type)

    await _web_gw.start()

    start_scheduler_loop(_SchedulerDepsImpl())

    ipc_deps = _IpcDepsImpl()

    # Approval engine: wired up unconditionally so all three IPC
    # routes (proposal, auto_intercept, decision from REST) go through
    # a single coherent state machine. The orchestrator code that sends
    # notifications hands the engine a ChannelSender adapter that
    # resolves conversation_id → binding_id+chat_id via the gateway
    # fan-out.
    from rolemesh.approval.engine import ApprovalEngine
    from rolemesh.approval.notification import NotificationTargetResolver
    from rolemesh.db.pg import get_conversation as _pg_get_conv

    async def _convs_for_user_and_cw(user_id: str, coworker_id: str) -> list[str]:
        # Find conversations this user can talk to this coworker in.
        # A simple heuristic: conversations whose channel_binding_id
        # belongs to this coworker AND whose user_id matches (set for
        # web conversations) are candidates, sorted by
        # last_agent_invocation. Falling back to all conversations for
        # the coworker when user_id match is absent (e.g. Telegram
        # group conversations have no single user).
        all_for_cw = await pg_get_conversations_for_coworker(coworker_id)
        ranked = [
            c.id
            for c in all_for_cw
            if c.user_id == user_id or c.user_id is None
        ]
        return ranked

    class _OrchestratorChannelSender:
        async def send_to_conversation(
            self, conversation_id: str, text: str
        ) -> None:
            conv = await _pg_get_conv(conversation_id)
            if conv is None:
                logger.warning(
                    "approval notification: conversation not found",
                    conversation_id=conversation_id,
                )
                return
            cw = _state.coworkers.get(conv.coworker_id)
            await _send_via_coworker(cw, conv.channel_chat_id, text)

    resolver = NotificationTargetResolver(
        get_conversations_for_user_and_coworker=_convs_for_user_and_cw,
        get_conversation=_pg_get_conv,
        webui_base_url=os.environ.get("WEBUI_BASE_URL") or None,
    )
    approval_engine = ApprovalEngine(
        publisher=_transport.js,
        channel_sender=_OrchestratorChannelSender(),
        resolver=resolver,
    )
    ipc_deps.set_approval_engine(approval_engine)

    # V2 P1.1: thread the approval engine through to SafetyEngine so
    # require_approval verdicts actually produce human-in-the-loop
    # decision surfaces instead of just landing in the audit table.
    # The safety engine was instantiated earlier in start_subscribers
    # (without the approval dep because that module isn't constructed
    # yet at that point); patching the attribute post-hoc keeps
    # ordering minimal and avoids re-threading construction.
    if _safety_engine is not None:
        _safety_engine._approval_handler = approval_engine  # type: ignore[attr-defined]

    from rolemesh.approval.executor import ApprovalWorker
    from rolemesh.approval.expiry import run_approval_maintenance_loop

    approval_worker = ApprovalWorker(
        js=_transport.js,
        channel_sender=_OrchestratorChannelSender(),
    )
    await approval_worker.start()

    # V2 P1.1: 24-hour TTL on safety_decisions.approval_context.
    # Runs alongside approval maintenance — separate loops because the
    # two touch different tables and should fail independently.
    from rolemesh.safety.maintenance import run_safety_maintenance_loop

    safety_maintenance_stop = asyncio.Event()
    safety_maintenance_task = asyncio.create_task(
        run_safety_maintenance_loop(stop_event=safety_maintenance_stop)
    )

    approval_maintenance_stop = asyncio.Event()
    approval_maintenance_task = asyncio.create_task(
        run_approval_maintenance_loop(
            approval_engine, stop_event=approval_maintenance_stop
        )
    )

    # Cancel-for-job cascade: agent containers publish on StoppedEvent
    # (see docs/backend-stop-contract.md §8). We fan those out through
    # the engine so each pending approval for the aborted job gets
    # status=cancelled + an audit row.
    async def _on_cancel_for_job(msg: Any) -> None:
        try:
            jid = msg.subject.rsplit(".", 1)[-1]
        except Exception:  # noqa: BLE001 — defensive, subject format is fixed
            await msg.ack()
            return
        try:
            await approval_engine.cancel_for_job(jid)
        except Exception as exc:  # noqa: BLE001 — never let handler death leak
            logger.warning(
                "approval cancel_for_job handler failed",
                job_id=jid,
                error=str(exc),
            )
        with contextlib.suppress(Exception):
            await msg.ack()

    cancel_sub = await _transport.js.subscribe(
        "approval.cancel_for_job.*",
        durable="orch-approval-cancel",
        cb=_on_cancel_for_job,
        manual_ack=True,
    )

    ipc_tasks = await _start_nats_ipc_subscriptions(_transport, ipc_deps)

    # EC-2: serve the egress gateway's snapshot RPCs. The gateway asks
    # for a rule snapshot at startup and the identity map on demand;
    # without these responders the gateway fails closed (blocks every
    # request) and agents on the internal bridge lose egress.
    from rolemesh.egress.orch_glue import fetch_all_egress_rules, start_responders

    egress_responder_subs = await start_responders(
        _transport.nc,
        state=_state,
        rules_fetcher=fetch_all_egress_rules,
    )

    # Launch the egress gateway now that the snapshot responders are
    # registered. Moved here from _ensure_container_system_running()
    # because otherwise the gateway NATS-requests the snapshot before
    # anyone is listening and crash-loops the container.
    await _launch_egress_gateway_once_ready()

    _queue.set_process_messages_fn(_process_conversation_messages)
    _queue.set_on_queued(_emit_queued_status)
    _queue.set_on_container_starting(_emit_container_starting_status)
    _web_gw.set_on_stop(_handle_web_stop)
    await _recover_pending_messages()

    await _message_loop(shutdown_event)

    for t in ipc_tasks:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t

    for sub in egress_responder_subs:
        with contextlib.suppress(Exception):
            await sub.unsubscribe()  # type: ignore[union-attr]

    approval_maintenance_stop.set()
    safety_maintenance_stop.set()
    with contextlib.suppress(Exception):
        await cancel_sub.unsubscribe()
    approval_maintenance_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await approval_maintenance_task
    safety_maintenance_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await safety_maintenance_task
    await approval_worker.stop()
    # V2 P0.3: shut down the safety RPC server and its thread pool so
    # nats-py can tear down the subscription cleanly and in-flight
    # sync checks do not block process exit.
    if _safety_rpc_server is not None:
        with contextlib.suppress(Exception):
            await _safety_rpc_server.stop()  # type: ignore[attr-defined]
    if _safety_thread_pool is not None:
        with contextlib.suppress(Exception):
            _safety_thread_pool.shutdown(wait=False)  # type: ignore[attr-defined]
    await proxy_runner.cleanup()
    await _queue.shutdown(10000)
    for gw in _gateways.values():
        await gw.shutdown()
    await _transport.close()
    await _runtime.close()
    await close_database()


# ---------------------------------------------------------------------------
# Dependency implementations
# ---------------------------------------------------------------------------


class _SchedulerDepsImpl:
    """Concrete SchedulerDependencies backed by OrchestratorState."""

    @property
    def orchestrator_state(self) -> OrchestratorState:
        return _state

    def get_coworker(self, coworker_id: str) -> Coworker | None:
        cw = _state.coworkers.get(coworker_id)
        return _coworker_from_state(cw) if cw else None

    def get_session(self, conversation_id: str) -> str | None:
        for cw in _state.coworkers.values():
            for conv in cw.conversations.values():
                if conv.conversation.id == conversation_id:
                    return conv.session_id
        return None

    @property
    def queue(self) -> GroupQueue:
        return _queue

    def on_process(
        self,
        group_jid: str,
        container_name: str,
        group_folder: str,
        job_id: str | None = None,
    ) -> None:
        _queue.register_process(group_jid, container_name, group_folder, job_id)

    @property
    def transport(self) -> NatsTransport | None:
        return _transport

    @property
    def executor(self) -> ContainerAgentExecutor | None:
        return _executor

    def get_executor(self, backend_name: str) -> ContainerAgentExecutor | None:
        return _executors.get(backend_name)

    async def send_message(self, jid: str, raw_text: str, coworker_id: str = "") -> None:
        text = format_outbound(raw_text)
        if text:
            cw_state = _state.coworkers.get(coworker_id) if coworker_id else None
            await _send_via_coworker(cw_state, jid, text)


@dataclass(frozen=True)
class _TrustedCoworkerRec:
    """Minimal trusted view of a coworker exposed to the safety
    subscriber. Kept tight so SafetyEventsSubscriber has a small,
    stable contract to depend on (see rolemesh.safety.subscriber's
    TrustedCoworker Protocol).
    """

    tenant_id: str
    id: str


class _IpcDepsImpl:
    """Concrete IpcDeps backed by OrchestratorState.

    The approval engine is attached lazily via set_approval_engine() so
    deployments without ApprovalEngine fall through to no-op handlers —
    keeps the approval module zero-impact when it is not wired up.
    """

    def __init__(self) -> None:
        # ApprovalEngine type is imported lazily in main() to avoid the
        # module-level import cycle (approval.engine imports db.pg which
        # imports main-adjacent types).
        self._approval_engine: object | None = None

    def set_approval_engine(self, engine: object | None) -> None:
        self._approval_engine = engine

    async def send_message(self, jid: str, text: str) -> None:
        await _send_via_coworker(None, jid, text)

    async def on_proposal(
        self, data: dict[str, object], *, tenant_id: str, coworker_id: str
    ) -> None:
        if self._approval_engine is None:
            logger.warning(
                "submit_proposal received but approval engine is not wired"
            )
            return
        await self._approval_engine.handle_proposal(  # type: ignore[attr-defined]
            data, tenant_id=tenant_id, coworker_id=coworker_id
        )

    async def on_auto_intercept(
        self, data: dict[str, object], *, tenant_id: str, coworker_id: str
    ) -> None:
        if self._approval_engine is None:
            logger.warning(
                "auto_approval_request received but approval engine is not wired"
            )
            return
        await self._approval_engine.handle_auto_intercept(  # type: ignore[attr-defined]
            data, tenant_id=tenant_id, coworker_id=coworker_id
        )

    async def on_tasks_changed(self) -> None:
        if _transport is None:
            return
        for cw in _state.coworkers.values():
            tasks = await get_all_tasks(cw.config.tenant_id)
            task_rows: list[dict[str, object]] = [
                {
                    "id": t.id,
                    "coworkerFolder": cw.config.folder,
                    "prompt": t.prompt,
                    "schedule_type": t.schedule_type,
                    "schedule_value": t.schedule_value,
                    "status": t.status,
                    "next_run": t.next_run,
                }
                for t in tasks
            ]

            async def _update(
                folder: str, perms: AgentPermissions, tid: str, rows: list[dict[str, object]]
            ) -> None:
                assert _transport is not None
                await write_tasks_snapshot(_transport, tid, folder, perms, rows)

            cw_perms = cw.config.permissions
            t = asyncio.ensure_future(_update(cw.config.folder, cw_perms, cw.config.tenant_id, task_rows))
            _bg_tasks.add(t)
            t.add_done_callback(_bg_tasks.discard)


async def _send_via_coworker(cw_state: CoworkerState | None, chat_id: str, text: str) -> None:
    """Send a message using a specific coworker's binding."""
    if cw_state:
        for conv in cw_state.conversations.values():
            if conv.conversation.channel_chat_id == chat_id:
                channel_type = _get_channel_type_for_conv(cw_state, conv.conversation)
                binding = cw_state.channel_bindings.get(channel_type)
                if binding:
                    gw = _gateways.get(channel_type)
                    if gw:
                        await gw.send_message(binding.id, chat_id, text)
                return
    # Fallback: scan all coworkers (for backward compat)
    for cw in _state.coworkers.values():
        for conv in cw.conversations.values():
            if conv.conversation.channel_chat_id == chat_id:
                channel_type = _get_channel_type_for_conv(cw, conv.conversation)
                binding = cw.channel_bindings.get(channel_type)
                if binding:
                    gw = _gateways.get(channel_type)
                    if gw:
                        await gw.send_message(binding.id, chat_id, text)
                return
    logger.warning("No channel for chat_id", chat_id=chat_id)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main_sync() -> None:
    """Synchronous wrapper for CLI entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
