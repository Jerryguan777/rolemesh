"""Full-chain Pi E2E — real create_agent_session + registered fake provider.

What this file covers beyond the other hook tests:

  - The ONLY stub is a fake LLM `ApiProvider` registered into Pi's real
    `pi.ai.api_registry`. Everything else — Agent, AgentSession,
    `_wrap_tools_lazy`, `ExtensionRunner`, `ModelRegistry`, AuthStorage,
    SessionManager, DefaultResourceLoader — is constructed by the
    production code paths via `create_agent_session`.

  - Specifically, this exercises the "lazy tool wrap" guard in
    `src/pi/coding_agent/core/sdk.py`:

        if options.extension_runner_ref is not None:
            active_tools = _wrap_tools_lazy(...)

    If that guard ever regresses from `is not None` to truthy (`if
    options.extension_runner_ref:`), passing an empty dict `{}`
    silently skips wrapping — hooks never fire on real tool calls.
    The earlier harness bypassed this by calling `_wrap_tools_lazy`
    manually; this one doesn't.

The three scenarios exercise the same observable hook contract, but
now every event comes from Pi's real dispatch:

  1. Happy path — tool call then final reply. PreToolUse + PostToolUse
     fire via `_ExtensionWrappedTool` that `create_agent_session`
     itself wrapped.

  2. Tool failure — inner tool raises. `_ExtensionWrappedTool` emits
     tool_result with is_error=True, routed to
     `on_post_tool_use_failure`.

  3. Compaction — `await session.compact()` runs the real compaction
     pipeline and fires `session_before_compact` via `runner.emit`.
     The summary LLM round then fails (our scripted provider has no
     more events) — caught after the hook has fired.

Test isolation: `pi.ai.api_registry._api_provider_registry` is a
module-level mutable dict. The autouse fixture below clears it
after every test so a provider registered in one test does not leak
into the next.
"""

from __future__ import annotations

import asyncio  # noqa: TC003 — used at runtime in async signatures
from collections.abc import AsyncIterator  # noqa: TC003 — runtime use in provider stream signature
from pathlib import Path  # noqa: TC003 — runtime use via pytest tmp_path
from typing import Any

import pytest

from agent_runner import pi_backend
from agent_runner.hooks import (
    CompactionEvent,
    HookRegistry,
    StopEvent,
    ToolCallEvent,
    ToolCallVerdict,
    ToolResultEvent,
    ToolResultVerdict,
)
from agent_runner.hooks.handlers.transcript_archive import TranscriptArchiveHandler
from agent_runner.pi_backend import _build_bridge_extension
from pi.agent.types import AgentTool, AgentToolResult
from pi.ai.api_registry import (
    ApiProvider,
    clear_api_providers,
    register_api_provider,
)
from pi.ai.types import (
    AssistantMessage,
    AssistantMessageEvent,
    Context,
    DoneEvent,
    Model,
    SimpleStreamOptions,
    StreamOptions,
    TextContent,
    ToolCall,
)
from pi.coding_agent.core.auth_storage import AuthStorage
from pi.coding_agent.core.extensions.loader import create_extension_runtime
from pi.coding_agent.core.extensions.runner import ExtensionRunner
from pi.coding_agent.core.model_registry import ModelRegistry
from pi.coding_agent.core.resource_loader import (
    DefaultResourceLoader,
    DefaultResourceLoaderOptions,
)
from pi.coding_agent.core.sdk import (
    CreateAgentSessionOptions,
    create_agent_session,
)
from pi.coding_agent.core.session_manager import SessionManager

_FAKE_API = "fake-api"
_FAKE_PROVIDER = "fake-provider"


# ---------------------------------------------------------------------------
# Fixture: clean the module-level provider registry between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_api_providers() -> Any:
    """`register_api_provider` writes into a module-level global dict.
    Without cleanup, a scripted provider left over from test N pollutes
    test N+1 and causes surprising cross-file interactions. Always
    clear after the test runs."""
    yield
    clear_api_providers()


# ---------------------------------------------------------------------------
# Test tools
# ---------------------------------------------------------------------------


class _SendMessageTool(AgentTool):
    """In-process MCP-style tool that records every call it received."""

    def __init__(self) -> None:
        self._calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "send_message"

    @property
    def label(self) -> str:
        return "Send Message"

    @property
    def description(self) -> str:
        return "Send a message."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "body"],
        }

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self._calls

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        self._calls.append(dict(params))
        return AgentToolResult(
            content=[TextContent(text=f"sent to {params.get('to')}")],
            details=None,
        )


class _BrokenTool(_SendMessageTool):
    """Raises from inside execute() — mimics an MCP transport failure."""

    async def execute(  # type: ignore[override]
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        self._calls.append(dict(params))
        raise RuntimeError("transport timeout")


# ---------------------------------------------------------------------------
# Fake provider — the sole stub
# ---------------------------------------------------------------------------


def _fake_model() -> Model:
    """Model whose `api` field matches our registered fake provider. The
    api string is the key the registry dispatches on."""
    return Model(
        api=_FAKE_API,
        provider=_FAKE_PROVIDER,
        id="fake-model",
        name="Fake Model",
    )


def _register_scripted_provider(events: list[DoneEvent]) -> None:
    """Register a fake `ApiProvider` that yields pre-scripted events.

    Each call to stream_simple (i.e. each Agent LLM round) consumes the
    next event from the list. If the Agent asks for more rounds than we
    scripted, we raise a RuntimeError — that failure mode is what
    scenario 4 relies on to catch the summary LLM call after the
    PreCompact hook has already fired.
    """
    counter = {"i": 0}

    def _stream_fn(
        model: Model,
        context: Context,
        options: StreamOptions | SimpleStreamOptions | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        async def _gen() -> AsyncIterator[AssistantMessageEvent]:
            i = counter["i"]
            counter["i"] = i + 1
            if i >= len(events):
                raise RuntimeError(
                    f"scripted provider exhausted: call #{i + 1} but only "
                    f"{len(events)} events were scripted"
                )
            yield events[i]

        return _gen()

    register_api_provider(
        ApiProvider(
            api=_FAKE_API,
            stream=_stream_fn,  # type: ignore[arg-type]
            stream_simple=_stream_fn,  # type: ignore[arg-type]
        )
    )


def _tool_call_done(
    call_id: str, tool_name: str, args: dict[str, Any]
) -> DoneEvent:
    return DoneEvent(
        reason="toolUse",
        message=AssistantMessage(
            content=[ToolCall(id=call_id, name=tool_name, arguments=args)],
            api=_FAKE_API,
            provider=_FAKE_PROVIDER,
            model="fake-model",
            stop_reason="toolUse",
        ),
    )


def _text_done(text: str) -> DoneEvent:
    return DoneEvent(
        reason="stop",
        message=AssistantMessage(
            content=[TextContent(text=text)],
            api=_FAKE_API,
            provider=_FAKE_PROVIDER,
            model="fake-model",
            stop_reason="stop",
        ),
    )


# ---------------------------------------------------------------------------
# Harness: real create_agent_session + fake provider registered
# ---------------------------------------------------------------------------


class _HookRecorder:
    def __init__(self) -> None:
        self.pre_tool_use: list[ToolCallEvent] = []
        self.post_tool_use: list[ToolResultEvent] = []
        self.post_tool_use_failure: list[ToolResultEvent] = []
        self.pre_compact: list[CompactionEvent] = []
        self.stop: list[StopEvent] = []

    async def on_pre_tool_use(
        self, event: ToolCallEvent
    ) -> ToolCallVerdict | None:
        self.pre_tool_use.append(event)
        return None

    async def on_post_tool_use(
        self, event: ToolResultEvent
    ) -> ToolResultVerdict | None:
        self.post_tool_use.append(event)
        return None

    async def on_post_tool_use_failure(self, event: ToolResultEvent) -> None:
        self.post_tool_use_failure.append(event)

    async def on_pre_compact(self, event: CompactionEvent) -> None:
        self.pre_compact.append(event)

    async def on_stop(self, event: StopEvent) -> None:
        self.stop.append(event)


async def _build_harness(
    tmp_path: Path,
    *,
    tool: AgentTool,
    scripted_events: list[DoneEvent],
    extra_handlers: list[Any] | None = None,
) -> tuple[pi_backend.PiBackend, _HookRecorder, SessionManager, Any]:
    """Build the full production-path pipeline with only the provider stubbed.

    Real components:
      - AuthStorage (in-memory, so no disk writes)
      - ModelRegistry
      - SessionManager (writes to tmp_path)
      - DefaultResourceLoader (no extensions discovered in tmp_path)
      - create_agent_session → Agent, AgentSession, _wrap_tools_lazy
      - ExtensionRunner + our bridge extension

    Only stubbed:
      - The LLM provider's `stream_simple`, via _register_scripted_provider.
    """
    _register_scripted_provider(scripted_events)

    # In-memory auth with a runtime key for our fake provider — ModelRegistry
    # will find this on get_api_key() without touching disk.
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key(_FAKE_PROVIDER, "fake-api-key")
    model_registry = ModelRegistry(auth)

    # Session manager with explicit tmp_path session file, so no cwd pollution.
    session_manager = SessionManager.create(str(tmp_path))
    session_file = str(tmp_path / "session.jsonl")
    session_manager.set_session_file(session_file)

    # Agent dir inside tmp_path — fresh, no extensions to discover.
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    resource_loader = DefaultResourceLoader(
        DefaultResourceLoaderOptions(
            cwd=str(tmp_path),
            agent_dir=str(agent_dir),
        )
    )
    await resource_loader.reload()

    # Hook registry + handlers
    recorder = _HookRecorder()
    registry = HookRegistry()
    registry.register(recorder)
    for h in extra_handlers or []:
        registry.register(h)

    # THE critical line under test: non-None ref triggers
    # `_wrap_tools_lazy` inside create_agent_session. Pass an empty dict
    # so the lazy proxy reads ref["current"] at tool-execute time.
    extension_runner_ref: dict[str, Any] = {}

    result = await create_agent_session(
        CreateAgentSessionOptions(
            cwd=str(tmp_path),
            model=_fake_model(),
            session_manager=session_manager,
            auth_storage=auth,
            model_registry=model_registry,
            resource_loader=resource_loader,
            custom_tools=[tool],
            extension_runner_ref=extension_runner_ref,
        )
    )
    session = result.session

    # Install the bridge ExtensionRunner AFTER create_agent_session, exactly
    # the way PiBackend.start() does in production (mirrors the ordering in
    # src/agent_runner/pi_backend.py).
    bridge = _build_bridge_extension(registry)
    runtime = create_extension_runtime()
    runner = ExtensionRunner(
        extensions=[bridge], runtime=runtime, cwd=str(tmp_path)
    )
    extension_runner_ref["current"] = runner

    # Wire into PiBackend
    backend = pi_backend.PiBackend()
    backend._hooks = registry
    backend._session = session
    backend._session_file = session_file
    backend._unsubscribe = session.subscribe(backend._handle_event)

    return backend, recorder, session_manager, session


# ---------------------------------------------------------------------------
# Scenario 1 — happy path: tool call, then final text reply
# ---------------------------------------------------------------------------


async def test_full_chain_happy_path_tool_call_to_final_reply(
    tmp_path: Path,
) -> None:
    tool = _SendMessageTool()
    scripted = [
        _tool_call_done(
            "call-1",
            "send_message",
            {"to": "jerry", "body": "hello"},
        ),
        _text_done("done."),
    ]
    backend, recorder, _, _ = await _build_harness(
        tmp_path, tool=tool, scripted_events=scripted
    )

    await backend.run_prompt("please send hi to jerry")

    # Inner tool ran with the LLM's args verbatim — proves the full
    # _LazyWrappedTool → _ExtensionWrappedTool → inner.execute chain.
    assert tool.calls == [{"to": "jerry", "body": "hello"}], (
        f"inner tool must receive the LLM's args verbatim, got {tool.calls!r}"
    )

    # Hooks fired via Pi's real ExtensionRunner → our bridge
    assert len(recorder.pre_tool_use) == 1
    assert recorder.pre_tool_use[0].tool_name == "send_message"
    assert recorder.pre_tool_use[0].tool_input == {
        "to": "jerry",
        "body": "hello",
    }
    assert len(recorder.post_tool_use) == 1
    assert recorder.post_tool_use[0].is_error is False
    assert "sent to jerry" in recorder.post_tool_use[0].tool_result
    assert recorder.post_tool_use_failure == []

    # Stop hook fires exactly once — Pi's real AgentSession.prompt
    # returned normally, run_prompt's finally emits completed.
    assert len(recorder.stop) == 1
    assert recorder.stop[0].reason == "completed"


# ---------------------------------------------------------------------------
# Scenario 3 — inner tool raises; failure routed, turn still completes
# ---------------------------------------------------------------------------


async def test_full_chain_tool_failure_routes_to_failure_handler(
    tmp_path: Path,
) -> None:
    tool = _BrokenTool()
    scripted = [
        _tool_call_done(
            "call-1",
            "send_message",
            {"to": "jerry", "body": "hi"},
        ),
        _text_done("sorry, send failed."),
    ]
    backend, recorder, _, _ = await _build_harness(
        tmp_path, tool=tool, scripted_events=scripted
    )

    await backend.run_prompt("send hi")

    # Inner tool was invoked even though it raised
    assert tool.calls == [{"to": "jerry", "body": "hi"}]

    # Routing split: failure path only, success path must stay empty
    assert len(recorder.pre_tool_use) == 1
    assert recorder.post_tool_use == [], (
        "success handler must NOT be called when the tool raised"
    )
    assert len(recorder.post_tool_use_failure) == 1
    assert recorder.post_tool_use_failure[0].is_error is True
    assert (
        "transport timeout"
        in recorder.post_tool_use_failure[0].tool_result
    )

    # Pi absorbs the exception and the turn completes normally
    assert len(recorder.stop) == 1
    assert recorder.stop[0].reason == "completed"


# ---------------------------------------------------------------------------
# Scenario 4 — force compact(), verify PreCompact fires + archive written
# ---------------------------------------------------------------------------


async def test_full_chain_compact_fires_precompact_and_archives(
    tmp_path: Path,
) -> None:
    """session.compact() runs the production compaction pipeline:
    prepare_compaction → runner.emit('session_before_compact', ...) →
    our bridge → PreCompact hook → TranscriptArchiveHandler writes
    markdown. The subsequent summary LLM round hits our scripted
    provider with zero scripted events, so it raises; we catch that
    AFTER the hook has already fired."""
    tool = _SendMessageTool()
    archive_dir = tmp_path / "archive"
    archive_handler = TranscriptArchiveHandler(
        assistant_name="TestBot", archive_dir=archive_dir
    )

    # Scenario 4 scripts no events — the summary call will raise.
    backend, recorder, session_manager, session = await _build_harness(
        tmp_path,
        tool=tool,
        scripted_events=[],
        extra_handlers=[archive_handler],
    )
    assert backend is not None

    # Stub the session_manager's compaction settings to use tiny budgets,
    # so prepare_compaction actually produces a non-empty summary list
    # from just a handful of seeded messages (instead of needing the
    # full 20k-token default budget).
    class _TinyCompactionSettings:
        def get_compaction_settings(self) -> dict[str, Any]:
            return {
                "enabled": True,
                "reserve_tokens": 1,
                "keep_recent_tokens": 1,
            }

        # Delegate anything else to the real settings_manager if needed.
        def __getattr__(self, name: str) -> Any:
            return getattr(session._settings_manager, name)

    session._settings_manager = _TinyCompactionSettings()  # type: ignore[assignment]

    # Pre-seed the session with real Pi UserMessage / AssistantMessage
    # entries — this is the important part for validating
    # TranscriptArchiveHandler's Pi branch against production types
    # rather than duck-typed fakes.
    from pi.ai.types import AssistantMessage as PiAssistant
    from pi.ai.types import UserMessage as PiUser

    for i in range(5):
        session_manager.append_message(
            PiUser(content=[TextContent(text=f"user question {i}")])
        )
        session_manager.append_message(
            PiAssistant(
                content=[TextContent(text=f"assistant reply {i}")],
                api=_FAKE_API,
                provider=_FAKE_PROVIDER,
                model="fake-model",
                stop_reason="stop",
            )
        )

    # Force-trigger compaction. The summary LLM round will raise from
    # our scripted provider (zero events), caught here. Hook MUST have
    # fired before that raise.
    with pytest.raises(Exception):  # noqa: B017 — exact exception type not contractual
        await session.compact()

    # PreCompact hook fired exactly once via Pi's real runner.emit
    assert len(recorder.pre_compact) == 1, (
        f"PreCompact hook must fire exactly once on compact(), got "
        f"{len(recorder.pre_compact)}"
    )
    compact_event = recorder.pre_compact[0]
    assert len(compact_event.messages) > 0, (
        "compact() should hand the bridge a non-empty messages list"
    )

    # Archive file was written using real Pi UserMessage / AssistantMessage
    archive_files = list(archive_dir.glob("*.md"))
    assert len(archive_files) == 1, (
        f"archive handler must write exactly one file; found {archive_files}"
    )
    body = archive_files[0].read_text()
    # Pi retains the most-recent turn in the live context, so at least
    # the first 3 of 5 seeded turns are guaranteed to reach the archive.
    archived_turns = sum(1 for i in range(5) if f"user question {i}" in body)
    assert archived_turns >= 3
    assert "**User**: user question 0" in body
    assert "**TestBot**: assistant reply 0" in body
