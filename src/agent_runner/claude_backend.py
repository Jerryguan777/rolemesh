"""
Claude SDK backend — wraps claude_agent_sdk as an AgentBackend.

The hook surface is mediated through HookRegistry — this file is a thin
bridge between Claude SDK's hook callback shape and the backend-neutral
HookRegistry protocol. Transcript archiving now lives in
`hooks/handlers/transcript_archive.py` and is wired as an ordinary
PreCompact handler in main.py.

Fail-close policy for control hooks is enforced here: any exception
escaping HookRegistry.emit_pre_tool_use / emit_user_prompt_submit is
converted into a deny/block response that the agent observes as a
blocked call — never silently allowed.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ToolUseBlock, query

from rolemesh.ipc.protocol import AgentInitData, McpServerSpec

from .backend import (
    BackendEvent,
    ErrorEvent,
    ResultEvent,
    RunningEvent,
    SafetyBlockEvent,
    SessionInitEvent,
    StoppedEvent,
    ToolUseEvent,
    tool_input_preview,
)
from .hooks import (
    CompactionEvent,
    HookRegistry,
    StopEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserPromptEvent,
)
from .message_stream import MessageStream
from .tools.claude_adapter import create_rolemesh_mcp_server
from .tools.context import ToolContext


def _log(message: str) -> None:
    print(f"[claude-backend] {message}", file=sys.stderr, flush=True)


def _field(data: Any, key: str) -> Any:
    """Pluck a field from input_data, which may be a dict or an object."""
    if isinstance(data, dict):
        return data.get(key)
    return getattr(data, key, None)


def _tool_response_text(response: Any) -> tuple[str, bool]:
    """Normalize a Claude SDK PostToolUse tool_response into (text, is_error).

    Handles:
      - str: (response, False)
      - dict with {"content": [...], "isError": bool}: flattened + error flag
      - list: joined text blocks
    """
    if isinstance(response, str):
        return response, False
    if isinstance(response, dict):
        is_error = bool(response.get("isError") or response.get("is_error"))
        content = response.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(block, str):
                    parts.append(block)
            return "".join(parts), is_error
        if isinstance(content, str):
            return content, is_error
        return "", is_error
    if isinstance(response, list):
        parts2: list[str] = []
        for block in response:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts2.append(text)
            elif isinstance(block, str):
                parts2.append(block)
        return "".join(parts2), False
    return "", False


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _build_hook_callbacks(
    hooks: HookRegistry,
    emit_safety_block: Callable[[SafetyBlockEvent], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Translate HookRegistry into claude_agent_sdk hook callbacks.

    ``emit_safety_block`` is invoked whenever a control hook (currently
    UserPromptSubmit) blocks. Claude SDK's response to
    ``{"decision":"block"}`` is to short-circuit the turn WITHOUT
    yielding a ResultMessage — which means the backend's async-for
    loop produces nothing, and without this emission the entire turn
    stays invisible to the orchestrator. Pi backend surfaces its own
    block via a SafetyBlockEvent in _apply_user_prompt_hook; this
    callback gives the Claude path parity.

    Kept as an optional callable (rather than a hardcoded emit) so
    unit tests can drive _build_hook_callbacks without setting up a
    full ClaudeBackend instance. See tests/test_agent_runner/
    test_claude_safety_block.py.
    """

    async def pre_tool_use(
        input_data: Any, _tool_use_id: Any, _context: Any
    ) -> dict[str, Any]:
        tool_name = str(_field(input_data, "tool_name") or "")
        tool_input_raw = _field(input_data, "tool_input")
        tool_input = tool_input_raw if isinstance(tool_input_raw, dict) else {}
        try:
            verdict = await hooks.emit_pre_tool_use(
                ToolCallEvent(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tool_call_id=str(_tool_use_id or ""),
                )
            )
        except Exception as exc:  # noqa: BLE001 — fail-close by design
            _log(f"PreToolUse handler raised, failing closed: {exc}")
            return _deny(f"Hook system error: {exc}")
        if verdict is None:
            return {}
        if verdict.block:
            return _deny(verdict.reason or "Tool call blocked by hook")
        if verdict.modified_input is not None:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "updatedInput": verdict.modified_input,
                }
            }
        return {}

    async def post_tool_use(
        input_data: Any, _tool_use_id: Any, _context: Any
    ) -> dict[str, Any]:
        tool_name = str(_field(input_data, "tool_name") or "")
        tool_input_raw = _field(input_data, "tool_input")
        tool_input = tool_input_raw if isinstance(tool_input_raw, dict) else {}
        tool_response = _field(input_data, "tool_response")
        text, is_error = _tool_response_text(tool_response)
        event = ToolResultEvent(
            tool_name=tool_name,
            tool_input=tool_input,
            tool_result=text,
            is_error=is_error,
            tool_call_id=str(_tool_use_id or ""),
        )
        if is_error:
            await hooks.emit_post_tool_use_failure(event)
            return {}
        verdict = await hooks.emit_post_tool_use(event)
        if verdict and verdict.appended_context:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": verdict.appended_context,
                }
            }
        return {}

    async def user_prompt_submit(
        input_data: Any, _tool_use_id: Any, _context: Any
    ) -> dict[str, Any]:
        prompt = str(_field(input_data, "prompt") or "")
        try:
            verdict = await hooks.emit_user_prompt_submit(UserPromptEvent(prompt=prompt))
        except Exception as exc:  # noqa: BLE001 — fail-close by design
            _log(f"UserPromptSubmit handler raised, failing closed: {exc}")
            return {"decision": "block", "reason": f"Hook system error: {exc}"}
        if verdict is None:
            return {}
        if verdict.block:
            reason = verdict.reason or "Prompt blocked by hook"
            if emit_safety_block is not None:
                # Telemetry-quality: a failure here must not flip the
                # safety-block decision below into a safety-allow.
                try:
                    await emit_safety_block(
                        SafetyBlockEvent(stage="input_prompt", reason=reason)
                    )
                except Exception as exc:  # noqa: BLE001
                    _log(f"safety block emit failed: {exc}")
            return {"decision": "block", "reason": reason}
        if verdict.appended_context:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": verdict.appended_context,
                }
            }
        return {}

    async def pre_compact(
        input_data: Any, _tool_use_id: Any, _context: Any
    ) -> dict[str, Any]:
        transcript_path = _field(input_data, "transcript_path")
        session_id = _field(input_data, "session_id")
        await hooks.emit_pre_compact(
            CompactionEvent(
                transcript_path=transcript_path if isinstance(transcript_path, str) else None,
                session_id=session_id if isinstance(session_id, str) else None,
            )
        )
        return {}

    return {
        "PreToolUse": [HookMatcher(hooks=[pre_tool_use])],
        "PostToolUse": [HookMatcher(hooks=[post_tool_use])],
        "UserPromptSubmit": [HookMatcher(hooks=[user_prompt_submit])],
        "PreCompact": [HookMatcher(hooks=[pre_compact])],
    }


class ClaudeBackend:
    """AgentBackend implementation wrapping claude_agent_sdk."""

    def __init__(self) -> None:
        self._listener: Callable[[BackendEvent], Awaitable[None]] | None = None
        self._session_id: str | None = None
        self._last_assistant_uuid: str | None = None
        self._stream: MessageStream | None = None
        self._sdk_env: dict[str, str | None] = dict(os.environ)

        self._mcp_server: Any = None
        self._init: AgentInitData | None = None
        self._assistant_name: str | None = None
        # Running query task — abort() cancels this to force the async for
        # loop inside run_prompt to unwind immediately. Without it,
        # stream.end() only closes the input pipe; the Claude CLI subprocess
        # happily finishes the in-flight LLM response and ResultMessage
        # arrives after the UI is already idle.
        self._query_task: asyncio.Task[None] | None = None
        # Snapshot of _last_assistant_uuid at prompt start. Used to rewind
        # the resume-session-at anchor on abort so the next turn doesn't
        # chain through the aborted Q1's partial assistant entry.
        self._pre_prompt_assistant_uuid: str | None = None
        # Set True for the duration of abort() so handle_follow_up rejects
        # late-arriving follow-ups that would otherwise race into the
        # still-alive MessageStream queue before cancel propagates.
        self._aborting: bool = False

        self._hooks: HookRegistry = HookRegistry()
        self._sdk_hooks: dict[str, Any] = {}

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def subscribe(self, listener: Any) -> None:
        self._listener = listener

    async def _emit(self, event: BackendEvent) -> None:
        if self._listener:
            await self._listener(event)

    async def _emit_stop(self, reason: str) -> None:
        """Manual Stop hook emission — observational, exceptions swallowed."""
        try:
            await self._hooks.emit_stop(
                StopEvent(reason=reason, session_id=self._session_id)
            )
        except Exception as exc:  # noqa: BLE001 — defensive; emit_stop is already fail-safe
            _log(f"Stop hook emission failed: {exc}")

    async def start(
        self,
        init: AgentInitData,
        tool_ctx: ToolContext,
        mcp_servers: list[McpServerSpec] | None = None,
        hooks: HookRegistry | None = None,
    ) -> None:
        self._init = init
        self._session_id = init.session_id
        self._assistant_name = init.assistant_name
        # Conditionally register send_message: only scheduled-task
        # containers get it. See create_rolemesh_mcp_server docstring
        # for rationale (avoids Claude misusing it as the reply channel
        # in interactive turns).
        self._mcp_server = create_rolemesh_mcp_server(
            tool_ctx,
            register_send_message=init.is_scheduled_task,
        )
        self._hooks = hooks if hooks is not None else HookRegistry()
        self._sdk_hooks = _build_hook_callbacks(
            self._hooks,
            emit_safety_block=self._emit,
        )

    async def run_prompt(self, text: str) -> None:
        """Run a single query through the Claude SDK."""
        assert self._init is not None

        # Defensive per-turn signal: the SDK's SystemMessage(init) also
        # triggers RunningEvent below, but guaranteeing one here means the
        # UI status bar doesn't depend on SDK internals staying stable.
        await self._emit(RunningEvent())

        # Snapshot the current resume anchor so we can roll back on abort.
        # Do NOT reset _aborting here: abort() already clears it when
        # there's no active query (P2 fix), and the prior turn's finally
        # clears it when there was one. Overwriting False at this point
        # would clobber a True latched by an abort() that raced into the
        # window between run_prompt's first await (RunningEvent emit) and
        # the creation of the consumer task below — the P1 race.
        self._pre_prompt_assistant_uuid = self._last_assistant_uuid

        stream = MessageStream()
        stream.push(text)
        self._stream = stream

        init = self._init

        # Load global CLAUDE.md
        global_claude_md_path = Path("/workspace/global/CLAUDE.md")
        global_claude_md: str | None = None
        if init.permissions.get("data_scope") != "tenant" and global_claude_md_path.exists():
            global_claude_md = global_claude_md_path.read_text()

        # Discover extra directories
        extra_dirs: list[str] = []
        extra_base = Path("/workspace/extra")
        if extra_base.exists():
            for entry in extra_base.iterdir():
                if entry.is_dir():
                    extra_dirs.append(str(entry))

        # Build system prompt
        system_prompt: dict[str, Any] | None = None
        append_parts: list[str] = []
        if init.system_prompt:
            append_parts.append(init.system_prompt)
        if global_claude_md:
            append_parts.append(global_claude_md)
        if append_parts:
            system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": "\n\n".join(append_parts),
            }

        # Build extra_args for resume-session-at
        extra_args: dict[str, str] | None = None
        if self._last_assistant_uuid:
            extra_args = {"resume-session-at": self._last_assistant_uuid}

        # Build MCP servers dict
        mcp_servers_dict: dict[str, Any] = {"rolemesh": self._mcp_server}

        # Build allowed tools list
        allowed_tools = [
            "Bash", "Read", "Write", "Edit", "Glob", "Grep",
            "WebSearch", "WebFetch", "Task", "TaskOutput", "TaskStop",
            "TeamCreate", "TeamDelete", "SendMessage", "TodoWrite",
            "ToolSearch", "Skill", "NotebookEdit",
            "mcp__rolemesh__*",
        ]

        # Register external MCP servers
        if init.mcp_servers:
            for spec in init.mcp_servers:
                server_config: dict[str, Any] = {"type": spec.type, "url": spec.url}
                headers: dict[str, str] = {}
                if init.user_id:
                    headers["X-RoleMesh-User-Id"] = init.user_id
                if headers:
                    server_config["headers"] = headers
                mcp_servers_dict[spec.name] = server_config
                allowed_tools.append(f"mcp__{spec.name}__*")
                _log(f"External MCP server registered: {spec.name} ({spec.type}) -> {spec.url}")

        options = ClaudeAgentOptions(
            cwd="/workspace/group",
            add_dirs=extra_dirs if extra_dirs else None,
            resume=self._session_id,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            env=self._sdk_env,
            permission_mode="bypassPermissions",
            mcp_servers=mcp_servers_dict,
            hooks=self._sdk_hooks,
            setting_sources=["project", "user"],
        )
        if extra_args:
            options.extra_args = extra_args

        message_count = 0
        result_count = 0
        error_raised = False

        # Run the async-for over the SDK stream inside a dedicated task so
        # abort() can cancel it. A bare `async for ...: ...` can't be
        # interrupted from another coroutine — cancelling run_prompt's own
        # task would also tear down the outer bridge loop.
        async def _consume_query() -> None:
            nonlocal message_count, result_count
            # Guard against two races abort() can't fix with task.cancel() alone:
            #   1. Pre-task race: abort() arrives between run_prompt's first
            #      await (RunningEvent emit) and the task being created —
            #      _query_task is still None so cancel() skips, but _aborting
            #      is already True.
            #   2. In-loop lag: CancelledError only propagates at the next
            #      await inside the async-for. Between SDK yielding a message
            #      and the next await we could process and emit one more
            #      ResultEvent despite the cancel being in flight. Checking
            #      _aborting at the top of each iteration makes the loop bail
            #      at the next safe point even before cancel lands.
            if self._aborting:
                return
            async for message in query(prompt=stream, options=options):
                if self._aborting:
                    break
                message_count += 1
                cls_name = type(message).__name__

                if cls_name == "SystemMessage":
                    subtype = getattr(message, "subtype", "")
                    data = getattr(message, "data", {})
                    if subtype == "init":
                        sid = data.get("session_id") if isinstance(data, dict) else None
                        if sid:
                            self._session_id = sid
                            await self._emit(SessionInitEvent(session_id=sid))
                            await self._emit(RunningEvent())
                            _log(f"Session initialized: {sid}")
                    elif subtype == "task_notification":
                        _log(f"Task notification: {data}" if not isinstance(data, dict) else
                             f"Task notification: task={data.get('task_id')} status={data.get('status')}")

                elif cls_name == "AssistantMessage":
                    uuid = getattr(message, "uuid", None)
                    if uuid:
                        self._last_assistant_uuid = uuid
                    # Emit one ToolUseEvent per ToolUseBlock in this message.
                    # Multiple tools in a single AssistantMessage mean parallel
                    # tool calls — emitting one event per block keeps the UI's
                    # overwrite-style progress bar showing advancement.
                    content = getattr(message, "content", None)
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, ToolUseBlock):
                                tool_name = str(block.name or "")
                                tool_input = block.input if isinstance(block.input, dict) else {}
                                await self._emit(
                                    ToolUseEvent(
                                        tool=tool_name,
                                        input_preview=tool_input_preview(tool_name, tool_input),
                                    )
                                )

                elif cls_name == "ResultMessage":
                    result_count += 1
                    text_result = getattr(message, "result", None)
                    session_id_from_result = getattr(message, "session_id", None)
                    if session_id_from_result:
                        self._session_id = session_id_from_result
                    # Each ResultMessage answers one user message in the prompt
                    # stream. Mark these intermediate so the host streams them
                    # to the UI without releasing idle-gating; the NATS bridge
                    # publishes the batch-final marker after run_prompt returns.
                    await self._emit(ResultEvent(
                        text=text_result or None,
                        new_session_id=self._session_id,
                        is_final=False,
                    ))

        aborted = False
        try:
            self._query_task = asyncio.create_task(_consume_query())
            await self._query_task
        except asyncio.CancelledError:
            aborted = True
            _log("Query cancelled by abort — rewinding resume anchor")
            # Rewind: the aborted turn's last AssistantMessage uuid (if any
            # was set mid-stream) would otherwise make the next run_prompt
            # issue resume-session-at pointing into Q1's partial output.
            # Reset to the snapshot so the next turn continues from the
            # pre-Q1 state, just like Pi's leaf rewind on abort.
            self._last_assistant_uuid = self._pre_prompt_assistant_uuid
            # Don't re-raise: the outer agent_runner bridge loop is awaiting
            # run_prompt, and propagating CancelledError would tear it down
            # along with the container. We want the container to stay alive
            # for the next prompt.
        except Exception as exc:
            error_raised = True
            await self._emit(ErrorEvent(error=str(exc)))
            raise
        finally:
            self._query_task = None
            self._stream = None
            self._aborting = False
            # Silent-end guard: the SDK can finish the async-for without
            # raising AND without yielding any ResultMessage when an
            # upstream HTTP call (egress 403, 5xx, timeout) is swallowed
            # internally. Without this, run_prompt returns "successfully"
            # with no ResultEvent ever published — the orchestrator sees
            # Stop("completed") and the user sees nothing, silently.
            #
            # Trigger only when:
            #   * not aborted — abort() legitimately ends a turn with
            #     result_count==0, but emits its own StoppedEvent
            #     separately and is not a silent failure.
            #   * not error_raised — the except-Exception path above
            #     already published a precise ErrorEvent; doing it again
            #     would forward two error events for one failure.
            #   * result_count == 0 — the contract for a healthy SDK
            #     turn is "at least one ResultMessage per run_prompt
            #     call". If that didn't happen, treat it as failure
            #     even though no exception surfaced.
            #
            # See pi_backend.py:_handle_event for the Pi-side counterpart;
            # Pi can identify upstream errors precisely via
            # PromptTurnCompleteEvent.stop_reason="error", whereas the
            # Claude SDK gives us no comparable signal — hence this
            # coarser-grained but still load-bearing terminal invariant.
            if not aborted and not error_raised and result_count == 0:
                await self._emit(ErrorEvent(
                    error=(
                        "Claude SDK ended the query with no ResultMessage. "
                        "This typically means an upstream HTTP error "
                        "(egress 403, rate-limit, timeout) was swallowed "
                        "by the SDK without surfacing as an exception."
                    )
                ))
                error_raised = True
            if not aborted:
                await self._emit_stop("error" if error_raised else "completed")

        _log(f"Query done. Messages: {message_count}, results: {result_count}")

    async def handle_follow_up(self, text: str) -> None:
        """Pipe a follow-up message into the active query stream.

        Rejects follow-ups that arrive once abort() has started: otherwise
        the SDK could drain a pushed Q2 from MessageStream's queue before
        the cancel propagates, and Q2's reply would be generated with Q1's
        aborted context still in the LLM session.
        """
        if self._aborting:
            _log(f"Ignoring follow-up during abort ({len(text)} chars)")
            return
        if self._stream:
            _log(f"Piping follow-up message into active query ({len(text)} chars)")
            self._stream.push(text)

    async def abort(self) -> None:
        """Cancel the active query and emit StoppedEvent.

        stream.end() alone doesn't stop the Claude CLI subprocess from
        finishing the in-flight LLM call; cancelling the query task is the
        only reliable way to prevent a late ResultMessage from reaching the
        UI after the user already saw 'stopped'. Order matters: set the
        guard flag before the cancel hits so concurrently-arriving
        follow-ups can't slip into the stream in the gap.

        If no query is in flight (abort called between turns), _aborting
        is cleared at the end — otherwise it would stay latched forever
        since only run_prompt's finally clears it, and no run_prompt was
        triggered by this abort. Leaving it True would silently gag any
        future handle_follow_up calls in the same session.
        """
        had_active_query = self._query_task is not None and not self._query_task.done()
        self._aborting = True
        if had_active_query:
            _log("Aborting active query")
            assert self._query_task is not None  # narrowed by had_active_query
            self._query_task.cancel()
        if self._stream:
            self._stream.end()
        await self._emit(StoppedEvent())
        if not had_active_query:
            self._aborting = False
        # Stop hook emission — AFTER StoppedEvent so the UI exits the
        # 'stopping' state first; observability handlers run second. See
        # docs/backend-stop-contract.md item 6.
        await self._emit_stop("aborted")

    async def shutdown(self) -> None:
        pass
