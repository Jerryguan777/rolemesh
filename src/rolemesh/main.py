"""Main orchestrator -- state management, message loop, agent invocation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from rolemesh.agent import CLAUDE_CODE_BACKEND, AgentInput, AgentOutput, ContainerAgentExecutor
from rolemesh.container.runner import (
    AvailableGroup,
    write_groups_snapshot,
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
    IDLE_TIMEOUT,
    NATS_URL,
    POLL_INTERVAL,
    TIMEZONE,
    TRIGGER_PATTERN,
)
from rolemesh.core.group_folder import resolve_group_folder_path
from rolemesh.core.logger import get_logger
from rolemesh.db.pg import (
    close_database,
    get_all_chats,
    get_all_registered_groups,
    get_all_sessions,
    get_all_tasks,
    get_messages_since,
    get_new_messages,
    get_router_state,
    init_database,
    set_registered_group,
    set_router_state,
    set_session,
    store_chat_metadata,
    store_message,
)
from rolemesh.ipc.nats_transport import NatsTransport
from rolemesh.ipc.task_handler import process_task_ipc
from rolemesh.orchestration.remote_control import (
    restore_remote_control,
    start_remote_control,
    stop_remote_control,
)
from rolemesh.orchestration.router import find_channel, format_messages, format_outbound
from rolemesh.orchestration.task_scheduler import start_scheduler_loop
from rolemesh.security.credential_proxy import start_credential_proxy
from rolemesh.security.sender_allowlist import (
    is_sender_allowed,
    is_trigger_allowed,
    load_sender_allowlist,
    should_drop_message,
)

if TYPE_CHECKING:
    from rolemesh.container.runtime import ContainerRuntime
    from rolemesh.core.types import Channel, NewMessage, RegisteredGroup

logger = get_logger()

# Re-export for backwards compatibility during refactor
__all__ = ["main", "main_sync"]

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_last_timestamp: str = ""
_sessions: dict[str, str] = {}
_registered_groups: dict[str, RegisteredGroup] = {}
_last_agent_timestamp: dict[str, str] = {}
_message_loop_running: bool = False

_channels: list[Channel] = []
_queue: GroupQueue = GroupQueue()
_transport: NatsTransport | None = None
_runtime: ContainerRuntime | None = None
_executor: ContainerAgentExecutor | None = None
_bg_tasks: set[asyncio.Task[None]] = set()


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


async def _load_state() -> None:
    """Load persisted state from the database."""
    global _last_timestamp, _sessions, _registered_groups, _last_agent_timestamp

    _last_timestamp = await get_router_state("last_timestamp") or ""
    agent_ts = await get_router_state("last_agent_timestamp")
    try:
        _last_agent_timestamp = json.loads(agent_ts) if agent_ts else {}
    except (json.JSONDecodeError, TypeError):
        logger.warning("Corrupted last_agent_timestamp in DB, resetting")
        _last_agent_timestamp = {}
    _sessions = await get_all_sessions()
    _registered_groups = await get_all_registered_groups()
    logger.info("State loaded", group_count=len(_registered_groups))


async def _save_state() -> None:
    """Persist state to the database."""
    await set_router_state("last_timestamp", _last_timestamp)
    await set_router_state("last_agent_timestamp", json.dumps(_last_agent_timestamp))


# ---------------------------------------------------------------------------
# Group management
# ---------------------------------------------------------------------------


async def _register_group(jid: str, group: RegisteredGroup) -> None:
    """Register a new group and persist it."""
    try:
        group_dir = resolve_group_folder_path(group.folder)
    except ValueError:
        logger.warning("Rejecting group registration with invalid folder", jid=jid, folder=group.folder)
        return

    _registered_groups[jid] = group
    await set_registered_group(jid, group)

    # Create group folder
    (group_dir / "logs").mkdir(parents=True, exist_ok=True)
    logger.info("Group registered", jid=jid, name=group.name, folder=group.folder)


async def get_available_groups() -> list[AvailableGroup]:
    """Get available groups list for the agent."""
    chats = await get_all_chats()
    registered_jids = set(_registered_groups.keys())

    return [
        AvailableGroup(
            jid=c.jid,
            name=c.name,
            last_activity=c.last_message_time,
            is_registered=c.jid in registered_jids,
        )
        for c in chats
        if c.jid != "__group_sync__" and c.is_group
    ]


def _set_registered_groups(groups: dict[str, RegisteredGroup]) -> None:
    """Set registered groups (for testing)."""
    global _registered_groups
    _registered_groups = groups


# ---------------------------------------------------------------------------
# NATS subscription handlers (replace file-based IPC watcher)
# ---------------------------------------------------------------------------


async def _start_nats_ipc_subscriptions(transport: NatsTransport, deps: _IpcDepsImpl) -> list[asyncio.Task[None]]:
    """Subscribe to NATS subjects for agent IPC messages and tasks."""
    tasks: list[asyncio.Task[None]] = []

    messages_sub = await transport.js.subscribe("agent.*.messages", durable="orch-messages")

    async def _handle_messages() -> None:
        async for msg in messages_sub.messages:
            try:
                data = json.loads(msg.data)
                if data.get("type") == "message" and data.get("chatJid") and data.get("text"):
                    chat_jid = data["chatJid"]
                    source_group = data.get("groupFolder", "")

                    registered_groups = deps.registered_groups()
                    folder_is_main: dict[str, bool] = {}
                    for group in registered_groups.values():
                        if group.is_main:
                            folder_is_main[group.folder] = True
                    is_main = folder_is_main.get(source_group, False)

                    target_group = registered_groups.get(chat_jid)
                    if is_main or (target_group is not None and target_group.folder == source_group):
                        await deps.send_message(chat_jid, data["text"])
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

                registered_groups = deps.registered_groups()
                folder_is_main: dict[str, bool] = {}
                for group in registered_groups.values():
                    if group.is_main:
                        folder_is_main[group.folder] = True
                is_main = folder_is_main.get(source_group, False)

                await process_task_ipc(data, source_group, is_main, deps)
                await msg.ack()
            except Exception:
                logger.exception("Error processing NATS IPC task")
                await msg.ack()

    tasks.append(asyncio.create_task(_handle_tasks()))

    logger.info("NATS IPC subscriptions started")
    return tasks


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------


async def _process_group_messages(chat_jid: str) -> bool:
    """Process all pending messages for a group."""
    global _last_agent_timestamp

    group = _registered_groups.get(chat_jid)
    if group is None:
        return True

    channel = find_channel(_channels, chat_jid)
    if channel is None:
        logger.warning("No channel owns JID, skipping messages", chat_jid=chat_jid)
        return True

    is_main_group = group.is_main

    since_timestamp = _last_agent_timestamp.get(chat_jid, "")
    missed_messages = await get_messages_since(chat_jid, since_timestamp, ASSISTANT_NAME)

    if not missed_messages:
        return True

    if not is_main_group and group.requires_trigger is not False:
        allowlist_cfg = load_sender_allowlist()
        has_trigger = any(
            TRIGGER_PATTERN.search(m.content.strip())
            and (m.is_from_me or is_trigger_allowed(chat_jid, m.sender, allowlist_cfg))
            for m in missed_messages
        )
        if not has_trigger:
            return True

    prompt = format_messages(missed_messages, TIMEZONE)

    previous_cursor = _last_agent_timestamp.get(chat_jid, "")
    _last_agent_timestamp[chat_jid] = missed_messages[-1].timestamp
    await _save_state()

    logger.info("Processing messages", group=group.name, message_count=len(missed_messages))

    idle_handle: asyncio.TimerHandle | None = None

    def _reset_idle_timer() -> None:
        nonlocal idle_handle
        if idle_handle is not None:
            idle_handle.cancel()
        loop = asyncio.get_running_loop()
        idle_handle = loop.call_later(
            IDLE_TIMEOUT / 1000.0,
            lambda: _queue.close_stdin(chat_jid),
        )

    if hasattr(channel, "set_typing"):
        try:
            await channel.set_typing(chat_jid, True)
        except (OSError, RuntimeError, TypeError, ValueError):
            logger.warning("Failed to set typing indicator", chat_jid=chat_jid)

    had_error = False
    output_sent_to_user = False

    async def _on_output(result: AgentOutput) -> None:
        nonlocal had_error, output_sent_to_user
        if result.result:
            raw = result.result
            import re

            text = re.sub(r"<internal>[\s\S]*?</internal>", "", raw).strip()
            logger.info("Agent output", group=group.name, chars=len(raw))
            if text:
                await channel.send_message(chat_jid, text)
                output_sent_to_user = True
            _reset_idle_timer()
        if result.status == "success":
            _queue.notify_idle(chat_jid)
        if result.status == "error":
            had_error = True

    output = await _run_agent(group, prompt, chat_jid, _on_output)

    if hasattr(channel, "set_typing"):
        with contextlib.suppress(OSError, RuntimeError, TypeError, ValueError):
            await channel.set_typing(chat_jid, False)
    if idle_handle is not None:
        idle_handle.cancel()

    if output == "error" or had_error:
        if output_sent_to_user:
            logger.warning(
                "Agent error after output was sent, skipping cursor rollback to prevent duplicates",
                group=group.name,
            )
            return True
        _last_agent_timestamp[chat_jid] = previous_cursor
        await _save_state()
        logger.warning("Agent error, rolled back message cursor for retry", group=group.name)
        return False

    return True


async def _run_agent(
    group: RegisteredGroup,
    prompt: str,
    chat_jid: str,
    on_output: Callable[[AgentOutput], Awaitable[None]] | None = None,
) -> str:
    """Run agent in a container. Returns 'success' or 'error'."""
    is_main = group.is_main
    session_id = _sessions.get(group.folder)

    if _transport is not None:
        tasks = await get_all_tasks()
        await write_tasks_snapshot(
            _transport,
            group.folder,
            is_main,
            [
                {
                    "id": t.id,
                    "groupFolder": t.group_folder,
                    "prompt": t.prompt,
                    "schedule_type": t.schedule_type,
                    "schedule_value": t.schedule_value,
                    "status": t.status,
                    "next_run": t.next_run,
                }
                for t in tasks
            ],
        )

        available_groups = await get_available_groups()
        await write_groups_snapshot(
            _transport,
            group.folder,
            is_main,
            available_groups,
            set(_registered_groups.keys()),
        )

    if _executor is None:
        logger.error("Agent executor not initialized")
        return "error"

    wrapped_on_output = None
    if on_output is not None:
        original_on_output = on_output

        async def _wrapped(output: AgentOutput) -> None:
            if output.new_session_id:
                _sessions[group.folder] = output.new_session_id
                await set_session(group.folder, output.new_session_id)
            await original_on_output(output)

        wrapped_on_output = _wrapped

    try:
        output = await _executor.execute(
            AgentInput(
                prompt=prompt,
                session_id=session_id,
                group_folder=group.folder,
                chat_jid=chat_jid,
                is_main=is_main,
                assistant_name=ASSISTANT_NAME,
            ),
            lambda handle, container_name, job_id: _queue.register_process(
                chat_jid, handle, container_name, group.folder, job_id
            ),
            wrapped_on_output,
        )

        if output.new_session_id:
            _sessions[group.folder] = output.new_session_id
            await set_session(group.folder, output.new_session_id)

        if output.status == "error":
            logger.error("Container agent error", group=group.name, error=output.error)
            return "error"

        return "success"
    except (OSError, RuntimeError, TypeError, ValueError):
        logger.exception("Agent error", group=group.name)
        return "error"


# ---------------------------------------------------------------------------
# Message loop
# ---------------------------------------------------------------------------


async def _message_loop(shutdown_event: asyncio.Event) -> None:
    """Main polling loop that detects new messages and dispatches them."""
    global _last_timestamp, _message_loop_running

    if _message_loop_running:
        logger.debug("Message loop already running, skipping duplicate start")
        return
    _message_loop_running = True

    logger.info("RoleMesh running", trigger=f"@{ASSISTANT_NAME}")

    while not shutdown_event.is_set():
        try:
            jids = list(_registered_groups.keys())
            messages, new_timestamp = await get_new_messages(jids, _last_timestamp, ASSISTANT_NAME)

            if messages:
                logger.info("New messages", count=len(messages))

                _last_timestamp = new_timestamp
                await _save_state()

                messages_by_group: dict[str, list[NewMessage]] = {}
                for msg in messages:
                    messages_by_group.setdefault(msg.chat_jid, []).append(msg)

                for chat_jid, group_messages in messages_by_group.items():
                    group = _registered_groups.get(chat_jid)
                    if group is None:
                        continue

                    channel = find_channel(_channels, chat_jid)
                    if channel is None:
                        logger.warning("No channel owns JID, skipping messages", chat_jid=chat_jid)
                        continue

                    is_main_group = group.is_main
                    needs_trigger = not is_main_group and group.requires_trigger is not False

                    if needs_trigger:
                        allowlist_cfg = load_sender_allowlist()
                        has_trigger = any(
                            TRIGGER_PATTERN.search(m.content.strip())
                            and (m.is_from_me or is_trigger_allowed(chat_jid, m.sender, allowlist_cfg))
                            for m in group_messages
                        )
                        if not has_trigger:
                            continue

                    all_pending = await get_messages_since(
                        chat_jid,
                        _last_agent_timestamp.get(chat_jid, ""),
                        ASSISTANT_NAME,
                    )
                    messages_to_send = all_pending if all_pending else group_messages
                    formatted = format_messages(messages_to_send, TIMEZONE)

                    if _queue.send_message(chat_jid, formatted):
                        logger.debug(
                            "Piped messages to active container", chat_jid=chat_jid, count=len(messages_to_send)
                        )
                        _last_agent_timestamp[chat_jid] = messages_to_send[-1].timestamp
                        await _save_state()
                        if hasattr(channel, "set_typing"):
                            try:
                                await channel.set_typing(chat_jid, True)
                            except (OSError, RuntimeError, TypeError, ValueError):
                                logger.warning("Failed to set typing indicator", chat_jid=chat_jid)
                    else:
                        _queue.enqueue_message_check(chat_jid)
        except (OSError, RuntimeError, TypeError, ValueError):
            logger.exception("Error in message loop")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=POLL_INTERVAL)
            break
        except TimeoutError:
            pass


async def _recover_pending_messages() -> None:
    """Startup recovery: check for unprocessed messages in registered groups."""
    for chat_jid, group in _registered_groups.items():
        since_timestamp = _last_agent_timestamp.get(chat_jid, "")
        pending = await get_messages_since(chat_jid, since_timestamp, ASSISTANT_NAME)
        if pending:
            logger.info("Recovery: found unprocessed messages", group=group.name, pending_count=len(pending))
            _queue.enqueue_message_check(chat_jid)


async def _ensure_container_system_running() -> None:
    """Ensure the container runtime is running and clean up orphans."""
    global _runtime
    _runtime = get_runtime()
    await _runtime.ensure_available()
    await _runtime.cleanup_orphans("rolemesh-")


# ---------------------------------------------------------------------------
# Remote control handler
# ---------------------------------------------------------------------------


async def _handle_remote_control(command: str, chat_jid: str, msg: NewMessage) -> None:
    """Handle /remote-control and /remote-control-end commands."""
    group = _registered_groups.get(chat_jid)
    if not group or not group.is_main:
        logger.warning("Remote control rejected: not main group", chat_jid=chat_jid, sender=msg.sender)
        return

    channel = find_channel(_channels, chat_jid)
    if channel is None:
        return

    if command == "/remote-control":
        import os

        result = await start_remote_control(msg.sender, chat_jid, os.getcwd())
        if result.get("ok"):
            await channel.send_message(chat_jid, str(result["url"]))
        else:
            await channel.send_message(chat_jid, f"Remote Control failed: {result.get('error', 'unknown')}")
    else:
        result = stop_remote_control()
        if result.get("ok"):
            await channel.send_message(chat_jid, "Remote Control session ended.")
        else:
            await channel.send_message(chat_jid, str(result.get("error", "Unknown error")))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    """Entry point for the RoleMesh orchestrator."""
    global _transport, _queue, _runtime, _executor

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
    _executor = ContainerAgentExecutor(
        CLAUDE_CODE_BACKEND,
        _runtime,
        _transport,
        lambda: _registered_groups,
    )

    _queue = GroupQueue(transport=_transport, runtime=_runtime)

    proxy_runner = await start_credential_proxy(CREDENTIAL_PROXY_PORT, PROXY_BIND_HOST)

    shutdown_event = asyncio.Event()

    def _signal_handler(sig_name: str) -> None:
        logger.info("Shutdown signal received", signal=sig_name)
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler, sig.name)

    import rolemesh.channels  # noqa: F401
    from rolemesh.channels.registry import (
        ChannelOpts,
        get_channel_factory,
        get_registered_channel_names,
    )

    def _on_message(chat_jid: str, msg: NewMessage) -> None:
        trimmed = msg.content.strip()
        if trimmed in ("/remote-control", "/remote-control-end"):
            asyncio.ensure_future(_handle_remote_control(trimmed, chat_jid, msg)).add_done_callback(
                lambda fut: (
                    logger.error("Remote control command error", chat_jid=chat_jid, error=str(fut.exception()))
                    if fut.exception()
                    else None
                )
            )
            return

        if not msg.is_from_me and not msg.is_bot_message and chat_jid in _registered_groups:
            cfg = load_sender_allowlist()
            if should_drop_message(chat_jid, cfg) and not is_sender_allowed(chat_jid, msg.sender, cfg):
                if cfg.log_denied:
                    logger.debug("sender-allowlist: dropping message (drop mode)", chat_jid=chat_jid, sender=msg.sender)
                return

        asyncio.ensure_future(store_message(msg)).add_done_callback(
            lambda fut: logger.error("store_message error", error=str(fut.exception())) if fut.exception() else None
        )

    channel_opts = ChannelOpts(
        on_message=_on_message,
        on_chat_metadata=store_chat_metadata,
        registered_groups=lambda: _registered_groups,
    )

    for channel_name in get_registered_channel_names():
        factory = get_channel_factory(channel_name)
        if factory is None:
            continue
        channel = factory(channel_opts)
        if channel is None:
            logger.warning(
                "Channel installed but credentials missing -- skipping. Check .env or re-run the channel skill.",
                channel=channel_name,
            )
            continue
        _channels.append(channel)
        await channel.connect()

    if not _channels:
        logger.critical("No channels connected")
        sys.exit(1)

    start_scheduler_loop(_SchedulerDepsImpl())

    ipc_deps = _IpcDepsImpl()
    ipc_tasks = await _start_nats_ipc_subscriptions(_transport, ipc_deps)

    _queue.set_process_messages_fn(_process_group_messages)
    await _recover_pending_messages()

    await _message_loop(shutdown_event)

    for t in ipc_tasks:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t

    await proxy_runner.cleanup()
    await _queue.shutdown(10000)
    for ch in _channels:
        await ch.disconnect()
    await _transport.close()
    await _runtime.close()
    await close_database()


# ---------------------------------------------------------------------------
# Dependency implementations
# ---------------------------------------------------------------------------


class _SchedulerDepsImpl:
    """Concrete SchedulerDependencies backed by module-level state."""

    def registered_groups(self) -> dict[str, RegisteredGroup]:
        return _registered_groups

    def get_sessions(self) -> dict[str, str]:
        return _sessions

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
        channel = find_channel(_channels, jid)
        if channel is None:
            logger.warning("No channel owns JID, cannot send message", jid=jid)
            return
        text = format_outbound(raw_text)
        if text:
            await channel.send_message(jid, text)


class _IpcDepsImpl:
    """Concrete IpcDeps backed by module-level state."""

    async def send_message(self, jid: str, text: str) -> None:
        channel = find_channel(_channels, jid)
        if channel is None:
            raise RuntimeError(f"No channel for JID: {jid}")
        await channel.send_message(jid, text)

    def registered_groups(self) -> dict[str, RegisteredGroup]:
        return _registered_groups

    async def register_group(self, jid: str, group: RegisteredGroup) -> None:
        await _register_group(jid, group)

    async def sync_groups(self, force: bool) -> None:
        coros = []
        for ch in _channels:
            if hasattr(ch, "sync_groups"):
                coros.append(ch.sync_groups(force))
        if coros:
            await asyncio.gather(*coros)

    async def get_available_groups(self) -> list[AvailableGroup]:
        return await get_available_groups()

    def write_groups_snapshot(
        self,
        group_folder: str,
        is_main: bool,
        available_groups: list[AvailableGroup],
        registered_jids: set[str],
    ) -> None:
        if _transport is not None:
            t = asyncio.ensure_future(
                write_groups_snapshot(_transport, group_folder, is_main, available_groups, registered_jids)
            )
            _bg_tasks.add(t)
            t.add_done_callback(_bg_tasks.discard)

    async def on_tasks_changed(self) -> None:
        if _transport is None:
            return
        tasks = await get_all_tasks()
        task_rows: list[dict[str, object]] = [
            {
                "id": t.id,
                "groupFolder": t.group_folder,
                "prompt": t.prompt,
                "schedule_type": t.schedule_type,
                "schedule_value": t.schedule_value,
                "status": t.status,
                "next_run": t.next_run,
            }
            for t in tasks
        ]

        async def _update_snapshots() -> None:
            assert _transport is not None
            for group in _registered_groups.values():
                await write_tasks_snapshot(_transport, group.folder, group.is_main, task_rows)

        t = asyncio.ensure_future(_update_snapshots())
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main_sync() -> None:
    """Synchronous wrapper for CLI entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
