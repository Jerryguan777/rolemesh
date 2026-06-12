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
import re
import signal
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rolemesh.channels.gateway import ChannelGateway
    from rolemesh.orchestration.approval_coordinator import ApprovalCoordinator
    from rolemesh.orchestration.approval_notify import ApprovalNotifier
    from rolemesh.safety.engine import SafetyEngine

from rolemesh.agent import (
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
    CONTAINER_IMAGE,
    CONTAINER_NETWORK_NAME,
    CREDENTIAL_PROXY_PORT,
    EGRESS_GATEWAY_IMAGE,
    GLOBAL_MAX_CONTAINERS,
    NATS_URL,
    POLL_INTERVAL,
    TIMEZONE,
)
from rolemesh.core.logger import get_logger
from rolemesh.core.orchestrator_state import (
    ConversationState,
    CoworkerState,
    OrchestratorState,
)
from rolemesh.db import (
    DEFAULT_TENANT,
    close_database,
    create_conversation,
    create_tenant,
    cleanup_running_delegations,
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
    list_coworker_mcp_configs,
    set_session,
    update_conversation_last_invocation,
    update_conversation_user_id,
    update_tenant_message_cursor,
)
from rolemesh.db import (
    store_message as db_store_message,
)
from rolemesh.ipc.nats_transport import NatsTransport
from rolemesh.ipc.task_handler import process_task_ipc
from rolemesh.orchestration.catalog import (
    handle_list_agents_request,
    render_agent_catalog,
)
from rolemesh.orchestration.delegation import handle_delegate_request
from rolemesh.orchestration.remote_control import (
    restore_remote_control,
)
from rolemesh.orchestration.router import format_messages, format_outbound
from rolemesh.orchestration.task_scheduler import start_scheduler_loop
from rolemesh.auth.credential_vault import (
    create_credential_vault_from_env,
    get_credential_vault,
    set_credential_vault,
)
from rolemesh.channels.admission import admit_telegram_1on1
from rolemesh.egress.credentials import CredentialResolver
from rolemesh.egress.token_identity import TokenAuthority
from rolemesh.security.credential_proxy import register_mcp_server, set_token_vault, start_credential_proxy

if TYPE_CHECKING:
    from rolemesh.container.runtime import ContainerRuntime
    from rolemesh.core.types import (
        ChannelBinding,
        Conversation,
        Coworker,
        NewMessage,
    )

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
    """Return the full Coworker dataclass carried on runtime CoworkerState.

    ``cw_state.config`` IS the Coworker loaded at state-load time, with
    every field intact. Returning it directly avoids the field-by-field
    reconstruction that silently dropped columns — the ``model_id`` drop
    was the Adam-on-gpt-4o-mini bug, and frontdesk v1.2 would likewise
    lose ``is_frontdesk`` / ``routing_description`` (and ``visibility``)
    were they reconstructed by hand here.
    """
    return cw_state.config


def _mcp_configs_from_state(coworker_id: str) -> list:
    """Look up MCP configs cached on ``_state`` for the executor.

    The executor uses this as its ``get_mcp_configs`` callable.
    Returns an empty list when the coworker isn't in state (the
    executor's ``_get_coworker`` branch already short-circuits in
    that case, so we never actually feed it to the spec builder).
    """
    cw = _state.coworkers.get(coworker_id)
    return list(cw.mcp_configs) if cw else []


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

# HITL approval coordinator (docs/12-hitl-approval-architecture.md §8). Created when the
# NATS subscriptions start; owns the orchestrator-side idle suspend/resume,
# expiry sweep, and restart recovery. None until startup so importing this
# module without full startup stays cheap.
_approval_coordinator: ApprovalCoordinator | None = None

# HITL approval human-facing delivery (docs §10 S4). Owns the Telegram/web card
# lifecycle (deliver on pending, deterministically edit on resolve) and the
# card-location cache the decision funnel authorises taps against. Created
# alongside the coordinator; None until startup.
_approval_notifier: ApprovalNotifier | None = None


async def _telegram_approval_decision(
    request_id: str, decision: str, telegram_user_id: str, chat_id: str
) -> str | None:
    """Funnel a Telegram ✅/❌ tap into the coordinator (docs §10 S4).

    IDOR guard: ``callback_data`` carries only ``request_id`` + verb. The
    authoritative ``(tenant_id, conversation_id, chat_id)`` come from the
    orchestrator's own card-location cache (not the client), and the tapping
    Telegram account must resolve — via ``user_channel_identities`` — to a
    RoleMesh user *in that tenant*. A guessed request_id, an unlinked sender,
    or a tap from a different chat is refused before any decision is recorded.
    Returns short toast text for the tap.
    """
    coordinator = _approval_coordinator
    notifier = _approval_notifier
    if coordinator is None or notifier is None:
        return "Approvals are not ready yet; please retry."
    ref = notifier.card_ref(request_id)
    if ref is None:
        return "This approval is no longer pending."
    if chat_id and ref.chat_id and chat_id != ref.chat_id:
        # Tap came from a chat other than the one the card lives in.
        return "Not authorized to decide this approval."
    from rolemesh.db.channel_identity import resolve_user_from_channel_sender

    approver_user_id = await resolve_user_from_channel_sender(
        ref.tenant_id, "telegram", telegram_user_id
    )
    if approver_user_id is None:
        return "Your Telegram account is not linked; cannot approve."
    won = await coordinator.decide(
        request_id,
        decision=decision,
        decided_by=approver_user_id,
        expected_tenant_id=ref.tenant_id,
        expected_conversation_id=ref.conversation_id,
    )
    if not won:
        return "This approval was already decided."
    if decision == "approve":
        # Reject/expire edit the card via the coordinator's notify_hard; the
        # winning approve closes the loop here (no LLM path edits it).
        await notifier.mark_outcome(request_id, "approved")
        return "Approved ✅"
    return "Rejected ❌"


async def _web_approval_decision(
    binding_id: str, chat_id: str, body: dict[str, object]
) -> None:
    """Funnel a WebUI approval-decision frame into the coordinator (docs §10 S4).

    ``binding_id``/``chat_id`` are authenticated (subject); the tenant is
    re-derived from the binding row and the conversation from (binding, chat),
    so the coordinator's guard binds the decision to the conversation the
    approver actually holds a ticket for. ``decided_by`` is the WebUI-stamped
    ticket user.
    """
    coordinator = _approval_coordinator
    notifier = _approval_notifier
    if coordinator is None or notifier is None:
        return
    request_id = body.get("request_id")
    decision = body.get("decision")
    if not isinstance(request_id, str) or decision not in ("approve", "reject"):
        return
    from rolemesh.db import (
        get_channel_binding_by_id_admin,
        get_conversation_by_binding_and_chat,
    )

    binding = await get_channel_binding_by_id_admin(binding_id)
    if binding is None:
        return
    conv = await get_conversation_by_binding_and_chat(
        binding_id, chat_id, tenant_id=binding.tenant_id
    )
    if conv is None:
        return
    decided_by = body.get("decided_by")
    if not isinstance(decided_by, str):
        decided_by = conv.user_id
    note = body.get("note")
    won = await coordinator.decide(
        request_id,
        decision=decision,
        decided_by=decided_by,
        note=note if isinstance(note, str) else None,
        expected_tenant_id=binding.tenant_id,
        expected_conversation_id=conv.id,
    )
    if won and decision == "approve":
        await notifier.mark_outcome(request_id, "approved")


async def _handle_agent_message_ipc(data: dict[str, object]) -> None:
    """Handle one ``agent.*.messages`` NATS publish from the send_message tool.

    Two delivery regimes share this subject:

    Interactive turns: the agent's reply already flows through the
    natural-output path (``agent.*.results`` → ``_on_output`` →
    ``send_stream_chunk`` / ``send_message`` on the channel gateway).
    Path β (this handler) was historically forwarding the same text a
    second time — every Claude AssistantMessage echoes the content it
    passed to ``send_message``, so users saw doubles. Commit a67d3e6
    removed that forward; interactive turns are still log-and-drop here.

    Scheduled-task turns: ``_run_task``'s ``_on_output`` only forwards
    when the agent produces a non-empty final ``result``. Agents
    typically just call ``send_message`` for "remind me at T" prompts
    and produce no separate result — so path α is empty for them.
    Without a forward here their message vanishes. The tool now stamps
    ``isScheduledTask`` on the payload so this handler can route only
    those into ``_send_via_coworker``; interactive turns still drop.

    Cross-chat targeting (one agent's send_message landing on another
    coworker's conversation) is still not supported — the tool hard-
    codes ``chatJid=ctx.chat_jid``, so the forward target is always
    the source coworker's own conversation. A future cross-chat feature
    would need a real target parameter AND a routing branch here.
    """
    if data.get("type") != "message":
        return
    chat_jid = data.get("chatJid")
    text = data.get("text")
    if not chat_jid or not text:
        return

    if not data.get("isScheduledTask"):
        # Interactive turn: natural-output path is the source of
        # truth (see a67d3e6). Drop the IPC echo to avoid doubles.
        logger.info(
            "Dropped send_message IPC (redundant with natural output path)",
            chat_jid=chat_jid,
            source_group=data.get("groupFolder", ""),
            text_preview=str(text)[:80],
        )
        return

    # Scheduled-task turn: forward to the channel gateway. Without
    # this, ``_run_task``'s empty-``result`` path leaves the user
    # with no delivery at all.
    source_group = str(data.get("groupFolder", ""))
    claimed_coworker_id = data.get("coworkerId")
    cw_state: CoworkerState | None = None
    if isinstance(claimed_coworker_id, str) and claimed_coworker_id:
        cw_state = _state.coworkers.get(claimed_coworker_id)
    if cw_state is None and source_group:
        for tenant in _state.tenants.values():
            cw_state = _state.get_coworker_by_folder(tenant.id, source_group)
            if cw_state is not None:
                break
    if cw_state is None:
        logger.warning(
            "Cannot route scheduled-task send_message IPC — coworker unresolved",
            chat_jid=chat_jid,
            source_group=source_group,
            claimed_coworker_id=claimed_coworker_id,
        )
        return
    logger.info(
        "Forwarding scheduled-task send_message IPC",
        chat_jid=chat_jid,
        coworker=cw_state.config.name,
        text_preview=str(text)[:80],
    )
    await _send_via_coworker(cw_state, str(chat_jid), str(text))


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
        require_approval blocks the turn identically to block.
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
        # R4 (docs/12-hitl-approval-architecture.md §11.4): the safety->approval bridge
        # is scoped to PRE_TOOL_CALL — the one stage with an awaiting container
        # and an approval surface. MODEL_OUTPUT runs orchestrator-side with no
        # container to block, so a ``require_approval`` verdict stays a HARD
        # block alias here (PRE_TOOL_CALL gets the HITL ticket instead).
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
    from rolemesh.db import get_all_tenants

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
        # Read the coworker's MCP bindings from the relation layer
        # (``coworker_mcp_servers`` JOIN ``mcp_servers``). 02b dropped
        # the inline JSONB column; ``list_coworker_mcp_configs`` is
        # now the single source of truth for "what does this coworker
        # have wired up".
        mcp_configs = await list_coworker_mcp_configs(
            cw.id, tenant_id=cw.tenant_id,
        )
        cw_state = CoworkerState.from_coworker(cw, mcp_configs=mcp_configs)

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

    # Register MCP servers with the credential proxy.
    for cw_state in _state.coworkers.values():
        for tool_cfg in cw_state.mcp_configs:
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


async def _auto_create_telegram_1on1_conversation(
    binding_id: str, chat_id: str, admitted_user_id: str
) -> tuple[CoworkerState, ConversationState] | None:
    """v6.1 §P1.5/§P1.6 — auto-create a Telegram 1:1 conversation
    for an *already admitted* sender.

    Counterpart of ``_auto_create_web_conversation``: the design's
    "already-linked -> create conv" implicit case. ``admission_user_id`` is the
    resolved RoleMesh user_id from ``admit_telegram_1on1`` — passed
    in so the row lands with ``user_id`` populated on first write
    instead of needing the lazy-backfill UPDATE later.

    Reset is one-shot (the §P1.3 cleanup wiped every legacy IM conv
    on the first migration); without auto-create here, every linked
    Telegram user's first message after relink would silently drop.
    """
    from rolemesh.db import (
        get_channel_binding_by_id_admin,
        get_conversation_by_binding_and_chat,
    )

    binding = await get_channel_binding_by_id_admin(binding_id)
    if binding is None or binding.channel_type != "telegram":
        return None
    cw = _state.coworkers.get(binding.coworker_id)
    if cw is None:
        logger.warning(
            "binding's coworker not in state; cannot route Telegram inbound",
            binding_id=binding_id,
            coworker_id=binding.coworker_id,
        )
        return None
    if binding.channel_type not in cw.channel_bindings:
        cw.channel_bindings[binding.channel_type] = binding

    # get-or-create: the row may already exist if a previous inbound
    # raced ahead, or if the channel chat reused a stale chat_id.
    conv = await get_conversation_by_binding_and_chat(
        binding_id, chat_id, tenant_id=cw.config.tenant_id
    )
    if conv is None:
        conv = await create_conversation(
            tenant_id=cw.config.tenant_id,
            coworker_id=cw.config.id,
            channel_binding_id=binding_id,
            channel_chat_id=chat_id,
            user_id=admitted_user_id,
        )
        logger.info(
            "Auto-created Telegram 1:1 conversation",
            coworker=cw.config.name,
            chat_id=chat_id,
            conversation_id=conv.id,
            user_id=admitted_user_id,
        )
    conv_state = ConversationState(conversation=conv)
    cw.conversations[conv.id] = conv_state
    return cw, conv_state


async def _auto_create_web_conversation(
    binding_id: str, chat_id: str
) -> tuple[CoworkerState, ConversationState] | None:
    """Auto-create a conversation for web channel (each browser tab gets a new chat_id)."""
    # First: check coworkers whose ``channel_bindings`` cache already
    # contains the binding (the startup-loaded path).
    for cw in _state.coworkers.values():
        for b in cw.channel_bindings.values():
            if b.id == binding_id and b.channel_type == "web":
                return await _land_web_conversation(cw, binding_id, chat_id)

    # Fallback: the binding row exists in DB but isn't in the in-memory
    # CoworkerState cache yet. Happens when the v1 webui creates the
    # binding via ``POST /api/v1/coworkers/{id}/conversations`` after
    # the orchestrator has booted. We hot-load from DB so the inbound
    # message doesn't get dropped — smoke caught this. The gateway
    # already hot-loads its own ``_bindings`` dict (see
    # ``WebNatsGateway._refresh_binding``); the missing piece is the
    # coworker-side cache, which this fallback fills.
    from rolemesh.db import get_channel_binding_by_id_admin

    binding = await get_channel_binding_by_id_admin(binding_id)
    if binding is None or binding.channel_type != "web":
        return None
    cw = _state.coworkers.get(binding.coworker_id)
    if cw is None:
        # Coworker not in state — would have been hot-loaded by the
        # ``web.coworker.restart`` subscriber on CREATE, but if that
        # event was missed we re-read here. Best-effort.
        logger.warning(
            "binding's coworker not in state; cannot route inbound",
            binding_id=binding_id,
            coworker_id=binding.coworker_id,
        )
        return None
    cw.channel_bindings[binding.channel_type] = binding
    return await _land_web_conversation(cw, binding_id, chat_id)


async def _land_web_conversation(
    cw: CoworkerState, binding_id: str, chat_id: str
) -> tuple[CoworkerState, ConversationState]:
    """Get-or-create + cache the conversation row for a web binding."""
    # ws.py may have already created the conversation before the
    # NATS message reaches the orchestrator. Check DB first to
    # avoid a UniqueViolationError on (binding_id, chat_id).
    conv = await get_conversation_by_binding_and_chat(
        binding_id, chat_id, tenant_id=cw.config.tenant_id
    )
    if conv is None:
        conv = await create_conversation(
            tenant_id=cw.config.tenant_id,
            coworker_id=cw.config.id,
            channel_binding_id=binding_id,
            channel_chat_id=chat_id,
            name=f"Web Chat {chat_id[:8]}",
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


async def _emit_child_chip_event_safe(
    *,
    parent_conv_id: str,
    child_conv_id: str,
    delegation_id: str,
    target_folder: str,
    target_name: str,
    phase: str,
    payload: dict[str, object],
) -> None:
    """Route a delegation child-chip progress event to the parent conv's web binding.

    Frontdesk v1.5: surfaces the target container's progress as a
    sub-chip rendered beneath the parent agent's status chip. Routed
    by the PARENT conversation's web binding (not the child's
    internal binding) because the WebUI only subscribes to web
    bindings — the internal binding has no WS listener.

    Silently no-op when:
      - the parent conv is unknown (not in state),
      - the parent conv's channel is not web (telegram/slack/internal),
      - the publish fails (NATS hiccup; chip events are best-effort).

    The audit path (delegations table, messages table) is the
    authoritative record; chip events are a UX layer on top.
    """
    found = _state.get_conversation(parent_conv_id)
    if not found:
        return
    cw_state, conv_state = found
    conv = conv_state.conversation
    channel_type = _get_channel_type_for_conv(cw_state, conv)
    binding = cw_state.channel_bindings.get(channel_type)
    gw = _gateways.get(channel_type) if binding else None
    if not (binding and isinstance(gw, WebNatsGateway)):
        return
    body: dict[str, object] = {
        "kind": "child_chip",
        "phase": phase,
        "child_conv_id": child_conv_id,
        "delegation_id": delegation_id,
        "target_folder": target_folder,
        "target_name": target_name,
        **payload,
    }
    try:
        await gw.send_status(binding.id, conv.channel_chat_id, body)
    except (OSError, RuntimeError):
        logger.debug(
            "child_chip emit failed",
            parent_conv_id=parent_conv_id,
            child_conv_id=child_conv_id,
            phase=phase,
            exc_info=True,
        )


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
) -> None:
    """Unified message handler for all channel gateways.

    All conversations are 1:1 (the product does not support group chat),
    so the agent always responds — there is no trigger gating.
    """
    # v6.1 §P1.5/§P1.6: For Telegram 1:1, gate admission BEFORE any
    # conversation lookup. Otherwise an admitted sender whose first
    # ever message arrives before a conv row exists (the common case
    # right after the §P1.3 reset wiped legacy IM convs) would
    # silently drop.
    #
    # ``binding`` lookup is admin_conn so this works even when the
    # binding's CoworkerState cache is empty (the cache populates
    # later via _auto_create_telegram_1on1_conversation).
    admitted_user_id: str | None = None
    from rolemesh.db import get_channel_binding_by_id_admin

    binding = await get_channel_binding_by_id_admin(binding_id)
    if binding is not None and binding.channel_type == "telegram":
        gateway = _gateways.get("telegram")
        if gateway is not None:
            admitted_user_id = await admit_telegram_1on1(
                tenant_id=binding.tenant_id,
                sender_channel_id=sender,
                gateway=gateway,
                binding_id=binding_id,
                chat_id=chat_id,
            )
            if admitted_user_id is None:
                return  # admission denied — guidance reply already sent

    # Find conversation
    result = _state.find_conversation_by_binding_and_chat(binding_id, chat_id)
    if not result:
        # Auto-create conversation for web channel (each browser tab = new chat_id)
        result = await _auto_create_web_conversation(binding_id, chat_id)
        if not result and admitted_user_id is not None:
            # v6.1 §P1.5/§P1.6: admitted Telegram 1:1 sender, no
            # existing conv → create one stamped with their user_id.
            result = await _auto_create_telegram_1on1_conversation(
                binding_id, chat_id, admitted_user_id
            )
        if not result:
            return

    _cw_state, conv_state = result
    conv = conv_state.conversation

    # v6.1 §P1.6 lazy backfill + §P1.4 identity-reassignment
    # correction (defense-in-depth complement to the same-transaction
    # NULL inside ``delete_channel_identity``):
    #
    # The original spec talked about backfilling NULL → user_id on
    # legacy convs. The stronger condition ``conv.user_id !=
    # admitted_user_id`` also catches the employee-handover case
    # where A unbound and B re-linked the same channel between
    # turns: even if a future bug in the unbind path forgot to NULL
    # the conv stamp, the admission layer here re-stamps it to the
    # currently-resolved user. The conv ``channel_binding_id`` is
    # not channel_type-mixed (binding ids are stable per channel),
    # so a cross-channel comparison can never trigger this branch.
    if admitted_user_id is not None and conv.user_id != admitted_user_id:
        await update_conversation_user_id(
            conv.id, admitted_user_id, tenant_id=conv.tenant_id
        )
        conv.user_id = admitted_user_id

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

    prompt = format_messages(missed_messages, TIMEZONE)

    previous_cursor = conv_state.last_agent_timestamp
    conv_state.last_agent_timestamp = missed_messages[-1].timestamp
    await update_conversation_last_invocation(
        conv.id, missed_messages[-1].timestamp, tenant_id=conv.tenant_id
    )

    logger.info("Processing messages", coworker=config.name, message_count=len(missed_messages))

    def _reset_idle_timer() -> None:
        # Idle-timer ownership moved onto the GroupQueue (§8): the approval
        # suspend path must cancel and later re-arm this exact timer from a NATS
        # handler that cannot reach a closure-local TimerHandle.
        # ``arm_idle_timer`` is a no-op while an approval is pending on this
        # conversation, so a status/tool event mid-approval can't un-suspend it.
        _queue.arm_idle_timer(conversation_id)

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
    _queue.cancel_idle_timer(conversation_id)

    if output == "error" or had_error:
        if output_sent_to_user:
            logger.warning(
                "Agent error after output was sent, skipping cursor rollback",
                coworker=config.name,
            )
            return True
        conv_state.last_agent_timestamp = previous_cursor
        await update_conversation_last_invocation(
            conv.id, previous_cursor, tenant_id=conv.tenant_id
        )
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
                await _handle_agent_message_ipc(json.loads(msg.data))
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

    # HITL approval (docs/12-hitl-approval-architecture.md §8). The container publishes
    # ``approval_request`` when it blocks a gated MCP tool call and
    # ``approval_cancel`` from its finally; the orchestrator suspends idle
    # reaping for the bounded wait, persists the request, relays decisions on
    # ``approval_decision``, and runs the expiry sweep + restart recovery. The
    # ApprovalCoordinator holds the race-prone state machine; this block is just
    # the NATS plumbing around it.
    for consumer_name in ("orch-approval-request", "orch-approval-cancel"):
        with contextlib.suppress(Exception):
            await transport.js.delete_consumer("agent-ipc", consumer_name)

    from rolemesh.orchestration.approval_coordinator import (
        ApprovalCoordinator,
        db_persistence,
    )

    def _resolve_tenant_for_coworker(coworker_id: str) -> str | None:
        cw = _state.coworkers.get(coworker_id)
        return cw.config.tenant_id if cw else None

    async def _publish_approval_decision(
        job_id: str, payload: dict[str, object]
    ) -> None:
        await transport.js.publish(
            f"agent.{job_id}.approval_decision", json.dumps(payload).encode(),
        )

    # Human-facing card delivery (§10 S4): resolve the request's conversation →
    # binding → chat and deliver/edit the ✅/❌ card on Telegram / web. Decoupled
    # from DB + channels by injected callables (admin-scoped: the orchestrator
    # has no tenant context and the request row carries the authoritative ids).
    from rolemesh.channels.telegram_gateway import TelegramGateway
    from rolemesh.channels.web_nats_gateway import WebNatsGateway
    from rolemesh.db import (
        get_channel_binding_by_id_admin,
        get_conversation_for_notification,
        get_conversations_for_coworker,
    )
    from rolemesh.orchestration.approval_notify import ApprovalNotifier

    async def _list_convs_for_coworker(
        coworker_id: str, tenant_id: str
    ) -> list[Any]:
        return await get_conversations_for_coworker(
            coworker_id, tenant_id=tenant_id
        )

    async def _tg_send_card(
        binding_id: str, chat_id: str, request_id: str, text: str
    ) -> int | None:
        tg = _gateways.get("telegram")
        if not isinstance(tg, TelegramGateway):
            return None
        return await tg.send_approval_card(
            binding_id, chat_id, request_id, text
        )

    async def _tg_edit_card(
        binding_id: str, chat_id: str, message_id: int, text: str
    ) -> None:
        tg = _gateways.get("telegram")
        if isinstance(tg, TelegramGateway):
            await tg.edit_approval_card(binding_id, chat_id, message_id, text)

    async def _web_publish_card(
        binding_id: str, chat_id: str, payload: dict[str, Any]
    ) -> None:
        web = _gateways.get("web")
        if isinstance(web, WebNatsGateway):
            await web.send_approval_event(binding_id, chat_id, payload)

    global _approval_notifier
    _approval_notifier = ApprovalNotifier(
        get_conversation=get_conversation_for_notification,
        get_binding=get_channel_binding_by_id_admin,
        list_conversations_for_coworker=_list_convs_for_coworker,
        send_telegram_card=_tg_send_card,
        edit_telegram_card=_tg_edit_card,
        publish_web_event=_web_publish_card,
    )

    global _approval_coordinator
    _approval_coordinator = ApprovalCoordinator(
        queue=_queue,
        persistence=db_persistence(),
        resolve_tenant=_resolve_tenant_for_coworker,
        publish_decision=_publish_approval_decision,
        notify_status=_approval_notifier.notify_status,
        notify_hard=_approval_notifier.notify_hard,
    )

    approval_request_sub = await transport.js.subscribe(
        "agent.*.approval_request", durable="orch-approval-request",
    )
    approval_cancel_sub = await transport.js.subscribe(
        "agent.*.approval_cancel", durable="orch-approval-cancel",
    )

    async def _handle_approval_requests() -> None:
        async for msg in approval_request_sub.messages:
            try:
                assert _approval_coordinator is not None
                await _approval_coordinator.on_approval_request(json.loads(msg.data))
            except Exception:
                logger.exception("Error processing approval_request")
            await msg.ack()

    async def _handle_approval_cancels() -> None:
        async for msg in approval_cancel_sub.messages:
            try:
                assert _approval_coordinator is not None
                await _approval_coordinator.on_approval_cancel(json.loads(msg.data))
            except Exception:
                logger.exception("Error processing approval_cancel")
            await msg.ack()

    tasks.append(asyncio.create_task(_handle_approval_requests()))
    tasks.append(asyncio.create_task(_handle_approval_cancels()))

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
                            await update_conversation_last_invocation(
                                conv.id, all_pending[-1].timestamp, tenant_id=conv.tenant_id
                            )

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

    # INV-3: name prefix alone is not safe — a foreign container the
    # user happens to name with "rolemesh-" could be killed. The image
    # whitelist is the positive identity signal that says "we launched
    # this one".
    await _runtime.cleanup_orphans(
        "rolemesh-",
        allowed_images=frozenset({CONTAINER_IMAGE, EGRESS_GATEWAY_IMAGE}),
    )


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
    # Idempotent bootstrap: ensure the gateway is running and register
    # its agent-network IP so ``runner.build_container_spec`` can pin it
    # as each agent container's DNS resolver. Lives in a shared module
    # so other entry points that spin up their own ``ContainerRuntime``
    # (eval CLI, ad-hoc admin scripts) get the same behaviour by
    # calling one function instead of duplicating this block — the
    # original inline code led to a real bug where eval-spawned
    # containers silently fell back to Docker's default DNS resolver
    # because no one called ``set_egress_gateway_dns_ip``.
    from rolemesh.egress.bootstrap import ensure_gateway_running_and_register_dns

    await ensure_gateway_running_and_register_dns(_runtime)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    """Entry point for the RoleMesh orchestrator."""
    global _transport, _queue, _runtime, _executor, _executors, _gateways

    await _ensure_container_system_running()
    await init_database()
    logger.info("Database initialized")
    # Frontdesk v1.2: sweep stale 'running' delegation audit rows left by a
    # prior crash, before state load and before any delegate subscription
    # accepts new work (handbook §6 Step 5.4).
    cleaned = await cleanup_running_delegations()
    if cleaned:
        logger.info(
            "delegations: cleaned up stale running rows on startup",
            count=cleaned,
        )
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

    def _render_catalog_for_executor(tenant_id: str, exclude_id: str) -> str:
        # Frontdesk v1.2: the executor injects this for is_frontdesk spawns.
        return render_agent_catalog(_state, tenant_id, exclude=exclude_id)

    # Token-identity: build the shared signing authority once. Under EC
    # this fail-closes if EGRESS_TOKEN_SECRET is unset (same secret the
    # gateway loads). With EC off there is no gateway to verify against,
    # so the authority stays None and spawns are token-free.
    _token_authority = TokenAuthority.from_env() if CONTAINER_NETWORK_NAME else None

    # Build one executor per backend.
    for cfg in (CLAUDE_CODE_BACKEND, PI_BACKEND):
        _executors[cfg.name] = ContainerAgentExecutor(
            cfg, _runtime, _transport, _get_coworker,
            get_mcp_configs=_mcp_configs_from_state,
            render_catalog=_render_catalog_for_executor,
            token_authority=_token_authority,
        )

    if AGENT_BACKEND_DEFAULT not in _executors:
        logger.warning("Unknown ROLEMESH_AGENT_BACKEND=%r, falling back to 'claude'", AGENT_BACKEND_DEFAULT)
    _executor = _executors.get(AGENT_BACKEND_DEFAULT, _executors["claude"])

    _queue = GroupQueue(transport=_transport, runtime=_runtime, orchestrator_state=_state)

    # Install the per-process CredentialVault so the resolver below
    # can decrypt rows from tenant_model_credentials. The webui process
    # already installs its own; orchestrator and webui share the same
    # CREDENTIAL_VAULT_KEY env var so the ciphertext written by one
    # decrypts in the other.
    set_credential_vault(create_credential_vault_from_env())
    _credential_resolver = CredentialResolver(get_credential_vault())

    # Host-side credential proxy: bound so the `register_mcp_server` /
    # `set_token_vault` wiring below has a sink to write to. Agents
    # reach the gateway container directly via Docker DNS, not this
    # host listener.
    proxy_runner = await start_credential_proxy(
        CREDENTIAL_PROXY_PORT,
        PROXY_BIND_HOST,
        credential_resolver=_credential_resolver,
    )

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
    _telegram_gw = TelegramGateway(on_message=_handle_incoming)
    _gateways = {
        "telegram": _telegram_gw,
        "slack": SlackGateway(on_message=_handle_incoming),
        "web": _web_gw,
    }

    # HITL approval decision intake (docs §10 S4). Register BEFORE bindings are
    # added: the Telegram bot instances capture the callback at construction
    # (inside add_binding), and the WebNatsGateway subscribes in start(). Both
    # funnels late-bind the coordinator/notifier globals, which are created in
    # _start_nats_ipc_subscriptions below — by the time any decision can arrive
    # (a card must already have been delivered) those globals are set.
    _telegram_gw.set_on_approval_decision(_telegram_approval_decision)
    _web_gw.set_on_approval_decision(_web_approval_decision)

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

    # Safety Framework background maintenance loop.
    from rolemesh.safety.maintenance import run_safety_maintenance_loop

    safety_maintenance_stop = asyncio.Event()
    safety_maintenance_task = asyncio.create_task(
        run_safety_maintenance_loop(stop_event=safety_maintenance_stop)
    )

    ipc_tasks = await _start_nats_ipc_subscriptions(_transport, ipc_deps)

    # EC-2: serve the egress gateway's snapshot RPCs. The gateway asks
    # for a rule snapshot at startup and the identity map on demand;
    # without these responders the gateway fails closed (blocks every
    # request) and agents on the internal bridge lose egress.
    from rolemesh.egress.mcp_cache import subscribe_mcp_changes
    from rolemesh.egress.orch_glue import fetch_all_egress_rules, start_responders

    egress_responder_subs = await start_responders(
        _transport.nc,
        state=_state,
        rules_fetcher=fetch_all_egress_rules,
    )

    # Mirror MCP registry deltas into THIS process. The webui process
    # is the one that publishes ``egress.mcp.changed`` (admin REST
    # edits a coworker's MCP bindings), but our in-process
    # ``_mcp_registry`` is also the source the snapshot responder
    # serves to the gateway. Without this subscription, an admin tools
    # edit would land on the gateway via the broadcast yet leave the
    # orchestrator's view stale; the next gateway restart would then
    # re-fetch a snapshot that's missing the edit.
    mcp_sub = await subscribe_mcp_changes(_transport.nc)
    egress_responder_subs.append(mcp_sub)

    # Frontdesk v1.2: core NATS responder for the ``list_agents`` tool.
    # Request-reply (not JetStream) — the calling frontdesk's turn blocks
    # synchronously on the RPC; reuses the existing connection.
    async def _list_agents_cb(msg: object) -> None:
        await handle_list_agents_request(msg, state=_state)

    list_agents_sub = await _transport.nc.subscribe(
        "agent.*.list_agents.request", cb=_list_agents_cb,
    )
    egress_responder_subs.append(list_agents_sub)

    # Frontdesk v1.2: core NATS responder for the ``delegate_to_agent``
    # tool. Request-reply (not JetStream) because the calling frontdesk's
    # turn is blocked synchronously on the RPC — the business deadline is
    # enforced inside the handler. Registered AFTER
    # ``cleanup_running_delegations()`` + ``_load_state()`` so in-flight
    # stale audit rows from a prior crash are sealed off before we start
    # producing new ones (handbook §6 Step 5.4).
    def _get_executor_for_backend(backend: str) -> object:
        return _executors.get(backend) if _executors else None

    async def _delegate_cb(msg: object) -> None:
        assert _queue is not None
        await handle_delegate_request(
            msg, state=_state, queue=_queue,
            get_executor=_get_executor_for_backend,
            emit_chip_event=_emit_child_chip_event_safe,
        )

    delegate_sub = await _transport.nc.subscribe(
        "agent.*.delegate.request", cb=_delegate_cb,
    )
    egress_responder_subs.append(delegate_sub)

    # Token vault RPC: gateway's RemoteTokenVault forwards each
    # user-mode MCP request here so we can decrypt + refresh tokens
    # using THIS process's TokenVault (which holds the DB conn and
    # the IdP refresh path). Only wire the responder when the vault
    # is configured — without OIDC there are no tokens to serve and
    # the gateway's RemoteTokenVault will time out to None, which
    # already maps to "skip Bearer injection" upstream.
    if _vault is not None:
        from rolemesh.egress.orch_glue import start_token_responder

        token_sub = await start_token_responder(_transport.nc, vault=_vault)
        egress_responder_subs.append(token_sub)

    # Credential RPC: the gateway's RemoteCredentialResolver forwards
    # every (tenant_id, provider) lookup here so we can decrypt rows
    # using THIS process's CredentialResolver (which holds the DB
    # conn and the Fernet vault). Without this responder the gateway's
    # RPC times out and the agent's LLM call surfaces as 502.
    from rolemesh.egress.orch_glue import start_credential_responder

    cred_sub = await start_credential_responder(
        _transport.nc, resolver=_credential_resolver,
    )
    egress_responder_subs.append(cred_sub)

    # v1.1 §7: hot-reload pipeline for coworker config changes from
    # the WebUI. The /api/v1 PATCH publishes ``web.coworker.restart``
    # on the JS ``web-ipc`` stream; this subscriber re-reads the row
    # so the next request uses the new config without an orchestrator
    # restart. The stream is created by the WebUI lifespan; we ensure
    # it here too in case the orchestrator boots before the WebUI.
    from nats.js.api import StreamConfig as _WebStreamConfig

    from rolemesh.db import get_coworker as _db_get_coworker
    from rolemesh.db import list_skills_for_coworker as _db_list_skills
    from rolemesh.orchestration.coworker_hot_reload import (
        subscribe_coworker_mcp_changed,
        subscribe_coworker_restart,
        subscribe_coworker_skills_changed,
    )

    try:
        await _transport.js.add_stream(
            _WebStreamConfig(name="web-ipc", subjects=["web.>"], max_age=3600.0)
        )
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await _transport.js.update_stream(
                _WebStreamConfig(name="web-ipc", subjects=["web.>"], max_age=3600.0)
            )

    async def _fetch_cw(coworker_id: str, tenant_id: str) -> Coworker | None:
        return await _db_get_coworker(coworker_id, tenant_id=tenant_id)

    async def _fetch_mcp_configs(coworker_id: str, tenant_id: str):
        return await list_coworker_mcp_configs(
            coworker_id, tenant_id=tenant_id,
        )

    async def _fetch_skills(coworker_id: str, tenant_id: str):
        # Projection-eligible only — matches the spawn-time projector
        # filter. The orchestrator cache feeds container spawn, so
        # mismatched enabled flags would inflate the tmpfs mount.
        return await _db_list_skills(
            coworker_id,
            tenant_id=tenant_id,
            enabled_only=True,
            with_files=True,
        )

    coworker_restart_sub = await subscribe_coworker_restart(
        _transport.js,
        state=_state,
        fetch_coworker=_fetch_cw,
        fetch_mcp_configs=_fetch_mcp_configs,
    )
    egress_responder_subs.append(coworker_restart_sub)

    # web.coworker.mcp_changed — sibling subscriber that handles the
    # narrower "junction row touched" event (bind / unbind / patch
    # enabled_tools). Keeps ``CoworkerState.mcp_configs`` honest
    # without a full coworker row refetch.
    coworker_mcp_sub = await subscribe_coworker_mcp_changed(
        _transport.js,
        state=_state,
        fetch_mcp_configs=_fetch_mcp_configs,
    )
    egress_responder_subs.append(coworker_mcp_sub)

    # web.coworker.skills_changed — sibling of mcp_changed for the
    # per-tenant skills catalog (v1.1 03b). Catalog edits and
    # coworker_skills mutations both publish; subscriber refreshes
    # ``CoworkerState.skills`` so the next container spawn sees the
    # new projection.
    coworker_skills_sub = await subscribe_coworker_skills_changed(
        _transport.js,
        state=_state,
        fetch_skills=_fetch_skills,
    )
    egress_responder_subs.append(coworker_skills_sub)

    # chore A — orchestrator-side ``web.run.cancel.*`` subscriber.
    # WebUI publishes the event from POST /api/v1/runs/{id}/cancel
    # (and from the WS request.cancel frame). The subscriber stops
    # the container (if any) and writes ``runs.status='cancelled'``
    # via the lifecycle helper. Re-uses the existing ``web-ipc``
    # JetStream stream registered above.
    from rolemesh.orchestration.run_cancel_subscriber import (
        subscribe_run_cancel,
    )

    assert _runtime is not None, (
        "ContainerRuntime must be initialised before "
        "subscribe_run_cancel — _runtime is wired in "
        "_init_container_runtime earlier in startup."
    )
    run_cancel_sub = await subscribe_run_cancel(
        _transport.js,
        runtime=_runtime,
        fetch_active_container=_queue.get_active_container_name,
    )
    egress_responder_subs.append(run_cancel_sub)

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

    # HITL restart recovery (R2): re-adopt + re-suspend any approvals left
    # pending by a previous orchestrator instance before the message loop starts
    # reaping. Runs after the queue's callbacks are wired so re-armed idle timers
    # resolve against a fully-configured queue.
    if _approval_coordinator is not None:
        await _approval_coordinator.recover_pending()

    await _message_loop(shutdown_event)

    for t in ipc_tasks:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t

    for sub in egress_responder_subs:
        with contextlib.suppress(Exception):
            await sub.unsubscribe()  # type: ignore[union-attr]

    safety_maintenance_stop.set()
    safety_maintenance_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await safety_maintenance_task
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
    """Concrete IpcDeps backed by OrchestratorState."""

    async def send_message(self, jid: str, text: str) -> None:
        await _send_via_coworker(None, jid, text)

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


async def _persist_web_assistant_message(
    conv: Conversation, sender_name: str, text: str
) -> None:
    """Persist an assistant message for a web conversation.

    Web is the only channel whose chat history lives in our own
    ``messages`` table — Telegram/Slack rely on the third-party
    service to retain history. The interactive web path persists via
    ``_process_conversation_messages`` above; this helper covers the
    IPC-driven paths (scheduled tasks today, future cross-chat sends)
    that bypass that loop. Without it, a scheduled-task reply to a
    web conversation goes to NATS only — invisible on page reload,
    and invisible to any WS that wasn't already connected at fire
    time (``DeliverPolicy.NEW`` doesn't replay).
    """
    await db_store_message(
        tenant_id=conv.tenant_id,
        conversation_id=conv.id,
        msg_id=str(uuid.uuid4()),
        sender=sender_name,
        sender_name=sender_name,
        content=text,
        timestamp=datetime.now(UTC).isoformat(),
        is_from_me=True,
        is_bot_message=True,
    )


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
                        if channel_type == "web":
                            await _persist_web_assistant_message(
                                conv.conversation, cw_state.config.name, text
                            )
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
                        if channel_type == "web":
                            await _persist_web_assistant_message(
                                conv.conversation, cw.config.name, text
                            )
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
