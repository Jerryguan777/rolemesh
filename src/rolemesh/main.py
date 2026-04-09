"""Main orchestrator -- state management, message loop, agent invocation.

Multi-tenant architecture: OrchestratorState replaces module-level globals.
Routing: binding_id -> conversation -> coworker.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import signal
import sys
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rolemesh.channels.gateway import ChannelGateway

from rolemesh.agent import CLAUDE_CODE_BACKEND, AgentInput, AgentOutput, ContainerAgentExecutor
from rolemesh.auth.permissions import AgentPermissions
from rolemesh.channels.slack_gateway import SlackGateway
from rolemesh.channels.telegram_gateway import TelegramGateway
from rolemesh.channels.web_nats_gateway import WebNatsGateway
from rolemesh.container.runner import (
    write_tasks_snapshot,
)
from rolemesh.container.runtime import (
    PROXY_BIND_HOST,
    ContainerHandle,
    get_runtime,
)
from rolemesh.container.scheduler import GroupQueue
from rolemesh.core.config import (
    ASSISTANT_NAME,
    CREDENTIAL_PROXY_PORT,
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
# Track texts sent via IPC send_message to deduplicate against results stream
_ipc_sent_texts: set[str] = set()
_bg_tasks: set[asyncio.Task[None]] = set()


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
    _ipc_sent_texts.clear()  # Reset dedup set for each processing cycle
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
            lambda: _queue.close_stdin(conversation_id),
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
        if result.result:
            raw = result.result
            text = re.sub(r"<internal>[\s\S]*?</internal>", "", raw).strip()
            logger.info("Agent output", coworker=config.name, chars=len(raw))
            if text and binding:
                # Skip if this exact text was already sent via IPC send_message
                if text in _ipc_sent_texts:
                    _ipc_sent_texts.discard(text)
                    logger.debug("Skipping duplicate result (already sent via IPC)", coworker=config.name)
                elif isinstance(gw, WebNatsGateway):
                    await gw.send_stream_chunk(binding.id, conv.channel_chat_id, text)
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
                    )
                elif gw:
                    await gw.send_message(binding.id, conv.channel_chat_id, text)
                output_sent_to_user = True
            _reset_idle_timer()
        if result.status == "success":
            # Send stream done immediately for web channel (don't wait for
            # _run_agent to return — the container stays alive until idle timeout)
            if binding and isinstance(gw, WebNatsGateway):
                with contextlib.suppress(OSError, RuntimeError, TypeError, ValueError):
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

    if _executor is None:
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
        output = await _executor.execute(
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
            lambda handle, container_name, job_id: _queue.register_process(
                conv.id, handle, container_name, config.folder, job_id
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
                data = json.loads(msg.data)
                if data.get("type") == "message" and data.get("chatJid") and data.get("text"):
                    chat_jid = data["chatJid"]
                    source_group = data.get("groupFolder", "")
                    source_coworker_id = data.get("coworkerId", "")

                    # Find source coworker (by ID, or fallback to folder lookup)
                    source_cw = _state.coworkers.get(source_coworker_id) if source_coworker_id else None
                    if source_cw is None and source_group:
                        for tenant in _state.tenants.values():
                            source_cw = _state.get_coworker_by_folder(tenant.id, source_group)
                            if source_cw:
                                break

                    # Authorization: all agents can only message their own conversations
                    authorized = False
                    if source_cw:
                        for conv in source_cw.conversations.values():
                            if conv.conversation.channel_chat_id == chat_jid:
                                authorized = True
                                break

                    if authorized:
                        # Track text sent via IPC so _on_output can deduplicate
                        _ipc_sent_texts.add(data["text"])
                        await _send_via_coworker(source_cw, chat_jid, data["text"])
                        logger.info("NATS IPC message sent", chat_jid=chat_jid, source_group=source_group)
                    else:
                        logger.warning(
                            "Unauthorized IPC message attempt blocked",
                            chat_jid=chat_jid,
                            source_group=source_group,
                        )
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
                source_tenant_id = data.get("tenantId", DEFAULT_TENANT)
                source_coworker_id = data.get("coworkerId", "")

                # Determine permissions from coworker state (fallback to folder lookup)
                source_cw = _state.coworkers.get(source_coworker_id) if source_coworker_id else None
                if source_cw is None and source_group:
                    for tenant in _state.tenants.values():
                        source_cw = _state.get_coworker_by_folder(tenant.id, source_group)
                        if source_cw:
                            source_coworker_id = source_cw.config.id
                            break
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
    """Ensure the container runtime is running and clean up orphans."""
    global _runtime
    _runtime = get_runtime()
    await _runtime.ensure_available()
    await _runtime.cleanup_orphans("rolemesh-")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    """Entry point for the RoleMesh orchestrator."""
    global _transport, _queue, _runtime, _executor, _gateways

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

    _executor = ContainerAgentExecutor(
        CLAUDE_CODE_BACKEND,
        _runtime,
        _transport,
        _get_coworker,
    )

    _queue = GroupQueue(transport=_transport, runtime=_runtime, orchestrator_state=_state)

    proxy_runner = await start_credential_proxy(CREDENTIAL_PROXY_PORT, PROXY_BIND_HOST)

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
    ipc_tasks = await _start_nats_ipc_subscriptions(_transport, ipc_deps)

    _queue.set_process_messages_fn(_process_conversation_messages)
    await _recover_pending_messages()

    await _message_loop(shutdown_event)

    for t in ipc_tasks:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t

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
        proc: ContainerHandle,
        container_name: str,
        group_folder: str,
        job_id: str | None = None,
    ) -> None:
        _queue.register_process(group_jid, proc, container_name, group_folder, job_id)

    @property
    def transport(self) -> NatsTransport | None:
        return _transport

    @property
    def executor(self) -> ContainerAgentExecutor | None:
        return _executor

    async def send_message(self, jid: str, raw_text: str) -> None:
        text = format_outbound(raw_text)
        if text:
            await _send_via_coworker(None, jid, text)


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
