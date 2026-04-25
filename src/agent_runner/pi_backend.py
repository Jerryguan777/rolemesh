"""
Pi backend — wraps pi.coding_agent as an AgentBackend.

Bridges Pi's extension system into the unified HookRegistry.

Bridge design:

  - PreToolUse: mapped to Pi's `tool_call` extension event. Pi's
    ToolCallEventResult only carries {block, reason} — it does NOT
    support modified_input. When a HookHandler returns modified_input,
    we log a warning and drop the modification; the agent sees the
    original input. Claude backend does honor modified_input — this
    asymmetry is documented in the spec. Applications that need
    guaranteed modification must enforce it via block + reason.

  - PostToolUse / PostToolUseFailure: mapped to Pi's `tool_result`
    event. Success vs failure is branched on ToolResultEvent.is_error.
    appended_context is attached by wrapping the original content list
    with an extra TextContent block.

  - PreCompact: mapped to Pi's `session_before_compact` event. Only
    observational here — we do NOT return {cancel: True}.

  - UserPromptSubmit: NOT routed through a Pi extension event. Pi does
    not invoke `emit_before_agent_start` or `emit_input` internally, so
    there is no reliable extension hook point for this. Instead we emit
    hooks.emit_user_prompt_submit() directly from run_prompt() and
    handle_follow_up() right before the text reaches session.prompt().
    Block semantics: when a handler blocks, we refuse to call
    session.prompt() and surface the block reason to the orchestrator
    via ResultEvent so the user sees why their prompt was denied.
    appended_context is prefixed to the prompt before dispatch (Pi has
    no first-class concept of system-context injection mid-session).

  - Stop: manually emitted at run_prompt end and abort end — matching
    the Claude backend's policy and docs/backend-stop-contract.md items
    6 and 7.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pi.agent.types import (
    PromptTurnCompleteEvent,
    ToolExecutionStartEvent,
)
from pi.ai.types import TextContent
from pi.coding_agent.core.agent_session import AgentSession, AgentSessionEvent
from pi.coding_agent.core.extensions.loader import create_extension_runtime
from pi.coding_agent.core.extensions.runner import ExtensionRunner
from pi.coding_agent.core.extensions.types import Extension
from pi.coding_agent.core.resource_loader import DefaultResourceLoader, DefaultResourceLoaderOptions
from pi.coding_agent.core.sdk import CreateAgentSessionOptions, create_agent_session
from pi.coding_agent.core.session_manager import SessionManager
from pi.mcp import McpServerConnection, load_mcp_tools
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
    UserPromptEvent,
)
from .hooks import (
    ToolCallEvent as HookToolCallEvent,
)
from .hooks import (
    ToolResultEvent as HookToolResultEvent,
)
from .tools.context import ToolContext
from .tools.pi_adapter import create_rolemesh_tools

_log_pylog = logging.getLogger(__name__)


def _log(message: str) -> None:
    print(f"[pi-backend] {message}", file=sys.stderr, flush=True)


def _extract_text(message: Any) -> str:
    """Extract text content from a Pi AssistantMessage."""
    if not hasattr(message, "content"):
        return ""
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextContent) or hasattr(block, "text"):
            parts.append(block.text)
    return "".join(parts)


def _task_done_callback(task: asyncio.Task[None]) -> None:
    """Log unhandled exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log(f"Background task error: {exc}")


def _build_bridge_extension(hooks: HookRegistry) -> Extension:
    """Build an inline Extension that routes Pi events to HookRegistry.

    Handlers are attached to the three Pi events we care about:
      - tool_call      -> hooks.emit_pre_tool_use
      - tool_result    -> hooks.emit_post_tool_use / on failure path
      - session_before_compact -> hooks.emit_pre_compact
    """

    extension = Extension(
        path="<rolemesh-hook-bridge>",
        resolved_path="<rolemesh-hook-bridge>",
        handlers={},
        tools={},
        message_renderers={},
        commands={},
        flags={},
        shortcuts={},
    )

    async def handle_tool_call(event: Any, _ctx: Any) -> dict[str, Any] | None:
        tool_name = str(getattr(event, "tool_name", "") or "")
        tool_input = getattr(event, "input", None)
        if not isinstance(tool_input, dict):
            tool_input = {}
        tool_call_id = str(getattr(event, "tool_call_id", "") or "")
        try:
            verdict = await hooks.emit_pre_tool_use(
                HookToolCallEvent(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tool_call_id=tool_call_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 — fail-close by design
            _log(f"PreToolUse handler raised, failing closed: {exc}")
            return {"block": True, "reason": f"Hook system error: {exc}"}
        if verdict is None:
            return None
        if verdict.block:
            return {
                "block": True,
                "reason": verdict.reason or "Tool call blocked by hook",
            }
        if verdict.modified_input is not None:
            # Pi's ToolCallEventResult has no input-modification slot.
            # Log once and continue without modification rather than
            # silently pretending it worked.
            _log_pylog.warning(
                "PreToolUse modified_input not supported on Pi backend; "
                "dropping modification for tool=%s",
                tool_name,
            )
        return None

    async def handle_tool_result(event: Any, _ctx: Any) -> dict[str, Any] | None:
        tool_name = str(getattr(event, "tool_name", "") or "")
        tool_input = getattr(event, "input", None)
        if not isinstance(tool_input, dict):
            tool_input = {}
        tool_call_id = str(getattr(event, "tool_call_id", "") or "")
        content = getattr(event, "content", None)
        if not isinstance(content, list):
            content = []
        is_error = bool(getattr(event, "is_error", False))

        # Flatten content to a readable string for the handler. Pi's
        # _ExtensionWrappedTool.execute builds the error path's content
        # as raw dicts `[{"type": "text", "text": str(err)}]` rather than
        # TextContent objects, so we must handle both object shape (with
        # .text attr) and dict shape (with "text" key).
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
            else:
                text = getattr(block, "text", None)
            if isinstance(text, str):
                text_parts.append(text)
        result_text = "".join(text_parts)

        hook_event = HookToolResultEvent(
            tool_name=tool_name,
            tool_input=tool_input,
            tool_result=result_text,
            is_error=is_error,
            tool_call_id=tool_call_id,
        )
        if is_error:
            await hooks.emit_post_tool_use_failure(hook_event)
            return None
        verdict = await hooks.emit_post_tool_use(hook_event)
        if verdict and verdict.appended_context:
            new_content = list(content)
            new_content.append(TextContent(text=verdict.appended_context))
            return {"content": new_content}
        return None

    async def handle_before_compact(event: Any, _ctx: Any) -> dict[str, Any] | None:
        preparation = (
            event.get("preparation")
            if isinstance(event, dict)
            else getattr(event, "preparation", None)
        )
        messages: list[Any] = []
        if preparation is not None:
            maybe_msgs = getattr(preparation, "messages_to_summarize", None)
            if isinstance(maybe_msgs, list):
                messages = list(maybe_msgs)
        await hooks.emit_pre_compact(CompactionEvent(messages=messages))
        return None

    extension.handlers["tool_call"] = [handle_tool_call]
    extension.handlers["tool_result"] = [handle_tool_result]
    extension.handlers["session_before_compact"] = [handle_before_compact]
    return extension


class PiBackend:
    """AgentBackend implementation wrapping Pi's AgentSession."""

    def __init__(self) -> None:
        self._listener: Callable[[BackendEvent], Awaitable[None]] | None = None
        self._session: AgentSession | None = None
        self._session_file: str | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._mcp_connections: list[McpServerConnection] = []
        self._bg_tasks: set[asyncio.Task[None]] = set()
        # Set True for the duration of abort() so handle_follow_up rejects
        # late-arriving follow-ups that would otherwise land on Pi's
        # follow_up_queue — mirrors the guard Claude's backend added in
        # 143fd03. Cleared at the end of abort().
        self._aborting: bool = False

        # Latched by abort() at its first synchronous step; consulted by
        # run_prompt's finally to know whether abort() has already
        # claimed this run's Stop emission. Necessary because Pi aborts
        # are cooperative: session.prompt() returns NORMALLY after
        # session.abort() releases its internal signal, so run_prompt's
        # finally cannot tell from an exception that the turn was
        # aborted. Using _aborting alone would race — abort()'s finally
        # clears _aborting BEFORE run_prompt's session.prompt has
        # resumed, and run_prompt would emit a duplicate Stop(completed).
        # Reset at the start of each run_prompt so the next run starts
        # clean.
        self._stop_emitted_by_abort: bool = False

        self._hooks: HookRegistry = HookRegistry()

    @property
    def session_id(self) -> str | None:
        return self._session_file

    def subscribe(self, listener: Any) -> None:
        self._listener = listener

    async def _emit(self, event: BackendEvent) -> None:
        if self._listener:
            await self._listener(event)

    async def _emit_stop(self, reason: str) -> None:
        try:
            await self._hooks.emit_stop(
                StopEvent(reason=reason, session_id=self._session_file)
            )
        except Exception as exc:  # noqa: BLE001 — defensive; emit_stop already fail-safe
            _log(f"Stop hook emission failed: {exc}")

    async def start(
        self,
        init: AgentInitData,
        tool_ctx: ToolContext,
        mcp_servers: list[McpServerSpec] | None = None,
        hooks: HookRegistry | None = None,
    ) -> None:
        cwd = "/workspace/group"
        self._hooks = hooks if hooks is not None else HookRegistry()

        # Register LLM providers (Anthropic, OpenAI, Google, etc.)
        # Must be called before any LLM streaming; the registry starts empty.
        from pi.ai.providers.register_builtins import register_built_in_api_providers
        register_built_in_api_providers()

        # Map credential proxy env vars to Pi's expected names.
        # Container has CLAUDE_CODE_OAUTH_TOKEN (Claude Code convention);
        # Pi reads ANTHROPIC_OAUTH_TOKEN or ANTHROPIC_API_KEY.
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_OAUTH_TOKEN"):
            oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            if oauth_token:
                os.environ["ANTHROPIC_OAUTH_TOKEN"] = oauth_token

        # ANTHROPIC_BASE_URL points to credential proxy (legacy path /).
        # For the /proxy/anthropic path, Pi's Anthropic SDK reads the env var.
        # OPENAI_BASE_URL points to /proxy/openai — OpenAI SDK reads it.

        # Session file path
        sessions_dir = Path("/workspace/sessions")
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_file = str(sessions_dir / f"{init.conversation_id}.jsonl")
        self._session_file = session_file

        # Create or open session manager
        if init.session_id and Path(init.session_id).exists():
            _log(f"Opening existing session: {init.session_id}")
            session_manager = SessionManager.open(init.session_id, cwd)
            self._session_file = init.session_id
        elif Path(session_file).exists():
            _log(f"Opening existing session file: {session_file}")
            session_manager = SessionManager.open(session_file, cwd)
        else:
            _log(f"Creating new session: {session_file}")
            session_manager = SessionManager.create(cwd)
            session_manager.set_session_file(session_file)

        # Build system prompt from coworker config + global CLAUDE.md
        custom_system_prompt: str | None = None
        append_system_prompt: str | None = None

        if init.system_prompt:
            append_system_prompt = init.system_prompt

        global_claude_md = Path("/workspace/global/CLAUDE.md")
        if init.permissions.get("data_scope") != "tenant" and global_claude_md.exists():
            md_content = global_claude_md.read_text()
            if append_system_prompt:
                append_system_prompt += "\n\n" + md_content
            else:
                append_system_prompt = md_content

        # Build resource loader with system prompt injection
        resource_loader = DefaultResourceLoader(
            DefaultResourceLoaderOptions(
                cwd=cwd,
                agent_dir=str(Path.home() / ".pi" / "agent"),
                system_prompt=custom_system_prompt,
                append_system_prompt=append_system_prompt,
            )
        )
        await resource_loader.reload()

        # Build custom tools (RoleMesh IPC tools + external MCP tools).
        # send_message is restricted to scheduled-task containers — see
        # claude_adapter.create_rolemesh_mcp_server for rationale.
        custom_tools = create_rolemesh_tools(
            tool_ctx,
            register_send_message=init.is_scheduled_task,
        )

        if mcp_servers:
            mcp_tools, self._mcp_connections = await load_mcp_tools(
                mcp_servers, user_id=init.user_id,
            )
            custom_tools.extend(mcp_tools)
            if mcp_tools:
                _log(f"Loaded {len(mcp_tools)} external MCP tools from {len(self._mcp_connections)} servers")

        # Resolve model from PI_MODEL_ID env var.
        # Format: "model-id" (searches all providers) or "provider/model-id".
        model = None
        model_id = os.environ.get("PI_MODEL_ID")
        if model_id:
            from pi.ai.models import get_model, get_providers

            if "/" in model_id:
                provider, mid = model_id.split("/", 1)
                model = get_model(provider, mid)
            else:
                for provider in get_providers():
                    model = get_model(provider, model_id)
                    if model:
                        break

            if model:
                # Override model.base_url to route through credential proxy.
                # Pi models have hardcoded base_urls (e.g. "https://api.openai.com/v1")
                # which bypass the proxy. We replace them with the proxy URL from env vars.
                _PROXY_ENV_MAP = {
                    "openai": "OPENAI_BASE_URL",
                    "anthropic": "ANTHROPIC_BASE_URL",
                }
                proxy_env = _PROXY_ENV_MAP.get(model.provider)
                if proxy_env:
                    proxy_url = os.environ.get(proxy_env)
                    if proxy_url:
                        model.base_url = proxy_url
                        _log(f"Routing {model.provider} through proxy: {proxy_url}")
                _log(f"Using model: {model.provider}/{model.id}")
            else:
                _log(f"Warning: model '{model_id}' not found in any provider")

        # Extension runner injection point. Ref is adopted by
        # create_agent_session() so tools get wrapped with lazy
        # extension interception that reads this ref at execute time.
        extension_runner_ref: dict[str, Any] = {}

        # Create agent session
        result = await create_agent_session(
            CreateAgentSessionOptions(
                cwd=cwd,
                model=model,
                session_manager=session_manager,
                resource_loader=resource_loader,
                custom_tools=custom_tools,
                extension_runner_ref=extension_runner_ref,
            )
        )
        self._session = result.session

        # Install the bridge extension. The runner is bound AFTER session
        # creation so the sdk wired our ref through first.
        bridge_ext = _build_bridge_extension(self._hooks)
        runtime = create_extension_runtime()
        runner = ExtensionRunner(
            extensions=[bridge_ext],
            runtime=runtime,
            cwd=cwd,
        )
        extension_runner_ref["current"] = runner

        # Subscribe to session events
        self._unsubscribe = self._session.subscribe(self._handle_event)

        await self._emit(SessionInitEvent(session_id=self._session_file or ""))
        await self._emit(RunningEvent())
        _log(f"Pi session started (session_id={self._session.session_id})")

    def _schedule_emit(self, event: BackendEvent) -> None:
        """Schedule an async _emit call from a synchronous context."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._emit(event))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        task.add_done_callback(_task_done_callback)

    def _handle_event(self, event: AgentSessionEvent) -> None:
        """Synchronous event handler — translates Pi session events into
        BackendEvents on NATS.

        PromptTurnCompleteEvent fires once per answered user prompt — including
        follow-ups queued during an active turn — and carries the final
        assistant message for that prompt. Each one becomes a ResultEvent with
        is_final=False so the host streams every reply to the user but does
        not release idle-gating until the whole batch settles. The is_final
        marker is published by the NATS bridge after run_prompt returns.

        Error path: when the upstream LLM call fails (egress 403, 5xx,
        timeout, max_turns hit, etc.), pi.agent_loop fires a
        PromptTurnCompleteEvent whose message has stop_reason="error" and
        an error_message but no text content (see
        src/pi/agent/proxy.py:413-418 plus agent_loop.py:188-192 plus the
        "stream ended without a done event" fallback at agent_loop.py:328-337).
        Without translating that into an ErrorEvent here, run_prompt's
        finally would emit Stop("completed") with no ResultEvent and the
        orchestrator would silently report a successful turn that
        produced no reply.

        Aborted path: stop_reason="aborted" can also reach this handler
        when abort() races with a partial response. abort() emits its
        own StoppedEvent and resets state, so this handler stays out of
        the way — emitting a competing ErrorEvent here would fight the
        abort flow.

        Stop-hook reason caveat: emitting ErrorEvent here does NOT change
        the eventual _emit_stop("completed") in run_prompt's finally,
        because session.prompt() returns normally even on the error path
        (Pi turns HTTP failures into in-stream events, not exceptions).
        Today this asymmetry has zero observable effect — no StopHandler
        is registered against StopEvent.reason anywhere in the repo, and
        the orchestrator-facing truth comes from ErrorEvent →
        ContainerOutput(status="error"), which IS emitted correctly.
        Revisit if/when a real Stop observer is wired up.
        """
        if isinstance(event, PromptTurnCompleteEvent):
            msg = getattr(event, "message", None)
            stop_reason = getattr(msg, "stop_reason", None)
            if stop_reason == "error":
                # Surface BOTH the partial reply (if any) and the error.
                # proxy.py accumulates streamed TextContent into the same
                # AssistantMessage that ends up here on failure — losing
                # that text would silently swallow the model's actual
                # output up to the point of failure (common pattern: LLM
                # streams half a paragraph, then upstream times out).
                # Order matters: ResultEvent first so the user sees the
                # reply, then ErrorEvent so the orchestrator records
                # status="error" for the turn.
                partial_text = _extract_text(msg) if msg is not None else ""
                if partial_text:
                    self._schedule_emit(
                        ResultEvent(
                            text=partial_text,
                            new_session_id=self._session_file,
                            is_final=False,
                        )
                    )
                err = (
                    getattr(msg, "error_message", None)
                    or "LLM stream ended in error with no further detail"
                )
                self._schedule_emit(ErrorEvent(error=err))
                return
            if stop_reason == "aborted":
                return
            text = _extract_text(msg) if msg is not None else ""
            if text:
                self._schedule_emit(
                    ResultEvent(
                        text=text,
                        new_session_id=self._session_file,
                        is_final=False,
                    )
                )
        elif isinstance(event, ToolExecutionStartEvent):
            self._schedule_emit(
                ToolUseEvent(
                    tool=event.tool_name,
                    input_preview=tool_input_preview(event.tool_name, event.args),
                )
            )

    async def _apply_user_prompt_hook(self, text: str) -> str | None:
        """Run UserPromptSubmit hook. Returns the (possibly modified) text,
        or None if the prompt was blocked.

        Block semantics on Pi: since Pi does not have a first-class
        'reject incoming prompt' signal, we refuse to hand the text to
        session.prompt() and instead emit a SafetyBlockEvent that
        surfaces the block reason to the orchestrator on a dedicated
        channel — distinct from ResultEvent so blocks don't end up in
        the conversation messages table posing as assistant replies.
        """
        try:
            verdict = await self._hooks.emit_user_prompt_submit(
                UserPromptEvent(prompt=text)
            )
        except Exception as exc:  # noqa: BLE001 — fail-close by design
            _log(f"UserPromptSubmit handler raised, failing closed: {exc}")
            await self._emit(
                SafetyBlockEvent(
                    stage="input_prompt",
                    reason=f"Hook system error: {exc}",
                )
            )
            return None
        if verdict is None:
            return text
        if verdict.block:
            reason = verdict.reason or "Prompt blocked by hook"
            await self._emit(
                SafetyBlockEvent(stage="input_prompt", reason=reason)
            )
            return None
        if verdict.appended_context:
            return f"{verdict.appended_context}\n\n{text}"
        return text

    async def run_prompt(self, text: str) -> None:
        assert self._session is not None
        # Emit RunningEvent per-turn so warm-container follow-ups also get a
        # progress signal. Pi's AgentSession is created once in start() and
        # reused across prompts — without this, turns 2..N would have no
        # running event and the UI status bar would stay empty.
        await self._emit(RunningEvent())

        # Reset the "abort claimed this run's Stop" latch at the start of
        # each run. Any abort() that fires during this run will set it
        # True before yielding to the event loop, so run_prompt's finally
        # sees it and skips its own Stop emission.
        self._stop_emitted_by_abort = False

        prompt_text = await self._apply_user_prompt_hook(text)
        if prompt_text is None:
            # Blocked: drain bg tasks then emit Stop(completed) — from the
            # outer bridge's point of view run_prompt did complete normally,
            # it just produced no assistant reply.
            if self._bg_tasks:
                await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)
            await self._emit_stop("completed")
            return

        error_raised = False
        try:
            # AgentSession.prompt() awaits Agent.prompt() which awaits _run_loop(),
            # so this call is fully blocking until every queued follow-up is
            # answered. Per-prompt ResultEvents flow from _handle_event via
            # PromptTurnCompleteEvent; the bridge emits the batch-final marker.
            await self._session.prompt(prompt_text)
        except Exception as exc:
            error_raised = True
            await self._emit(ErrorEvent(error=str(exc)))
            raise
        finally:
            # Drain any ResultEvent/ToolUseEvent publishes scheduled synchronously
            # from _handle_event so they hit the wire before the bridge's
            # batch-final marker (or before the exception propagates up). Without
            # this, the final marker can race ahead of the last per-prompt
            # ResultEvent and the host sees notify_idle before the reply text;
            # on error, already-scheduled replies would be left as orphaned
            # tasks with no publish guarantee.
            if self._bg_tasks:
                await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)
            # Stop hook: skip if abort() already emitted Stop(aborted) for
            # this run. This is NOT equivalent to checking _aborting —
            # abort()'s finally clears _aborting BEFORE run_prompt
            # resumes past session.prompt(), so _aborting would be False
            # here even when the turn was aborted. See §4.1.8.
            if not self._stop_emitted_by_abort:
                await self._emit_stop("error" if error_raised else "completed")

    async def handle_follow_up(self, text: str) -> None:
        assert self._session is not None
        # Reject follow-ups once abort() has started: otherwise Pi's
        # _queue_follow_up lands the message on _follow_up_queue, which
        # AgentSession.abort()'s rewind now clears — but if the push races
        # in AFTER the clear, the queue grows a ghost entry that resurrects
        # on the NEXT turn's get_follow_up_messages() poll.
        if self._aborting:
            _log(f"Ignoring follow-up during abort ({len(text)} chars)")
            return
        await self._emit(RunningEvent())

        prompt_text = await self._apply_user_prompt_hook(text)
        if prompt_text is None:
            return

        try:
            if self._session.is_streaming:
                await self._session.prompt(prompt_text, streaming_behavior="followUp")
            else:
                await self._session.prompt(prompt_text)
        except Exception as exc:
            _log(f"Follow-up error: {exc}")

    async def abort(self) -> None:
        """Abort the current turn and emit StoppedEvent for UI confirmation.

        session.abort() waits for the agent to become idle, then returns.
        We emit StoppedEvent after it settles so the UI can transition out
        of the 'stopping' state. _aborting gates handle_follow_up for the
        duration so concurrently-arriving follow-ups can't sneak onto the
        queue between abort starting and session.abort()'s internal rewind.
        """
        # Latch the "abort claims this run's Stop" flag BEFORE any await.
        # run_prompt's finally must see this True when session.prompt
        # returns, otherwise it will duplicate the Stop emission (we'd
        # end up with ["aborted", "completed"] for a single user turn).
        # Setting it synchronously — before any await — ensures no
        # schedule point lets run_prompt observe it as False.
        self._aborting = True
        self._stop_emitted_by_abort = True
        try:
            if self._session is not None:
                await self._session.abort()
            await self._emit(StoppedEvent())
        finally:
            self._aborting = False
        # Stop hook emission — AFTER StoppedEvent so the UI exits the
        # 'stopping' state first; observability handlers run second. See
        # docs/backend-stop-contract.md item 6.
        await self._emit_stop("aborted")

    async def shutdown(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
        if self._session is not None:
            self._session.dispose()
        for conn in self._mcp_connections:
            await conn.close()
        self._mcp_connections.clear()
