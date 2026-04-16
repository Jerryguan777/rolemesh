"""SDK entry point for creating agent sessions.

Python port of packages/coding-agent/src/core/sdk.ts.
Provides createAgentSession() as the main high-level API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pi.agent.agent import Agent, AgentOptions
from pi.agent.types import AgentMessage, AgentState, ThinkingLevel
from pi.ai.types import Message, Model
from pi.coding_agent.core.agent_session import (
    DEFAULT_THINKING_LEVEL,
    AgentSession,
    AgentSessionConfig,
)
from pi.coding_agent.core.auth_storage import AuthStorage
from pi.coding_agent.core.config import get_agent_dir
from pi.coding_agent.core.messages import convert_to_llm
from pi.coding_agent.core.model_registry import ModelRegistry
from pi.coding_agent.core.model_resolver import ScopedModel, find_initial_model
from pi.coding_agent.core.resource_loader import (
    DefaultResourceLoader,
    DefaultResourceLoaderOptions,
)
from pi.coding_agent.core.session_manager import SessionManager
from pi.coding_agent.core.settings_manager import SettingsManager


@dataclass
class CreateAgentSessionOptions:
    """Options for create_agent_session()."""

    cwd: str | None = None
    agent_dir: str | None = None
    model: Model | None = None
    thinking_level: ThinkingLevel | None = None
    continue_session: bool = False
    session_id: str | None = None
    tools: list[Any] | None = None
    custom_tools: list[Any] | None = None
    resource_loader: DefaultResourceLoader | None = None
    session_manager: SessionManager | None = None
    settings_manager: SettingsManager | None = None
    auth_storage: AuthStorage | None = None
    model_registry: ModelRegistry | None = None
    scoped_models: list[ScopedModel] | None = None


@dataclass
class CreateAgentSessionResult:
    """Result from create_agent_session()."""

    session: AgentSession
    extensions_result: Any
    model_fallback_message: str | None = None


async def create_agent_session(
    options: CreateAgentSessionOptions | None = None,
) -> CreateAgentSessionResult:
    """Create an AgentSession with the specified options.

    This is the main SDK entry point for programmatic usage.
    Sets up auth, model registry, settings, session management,
    resource loading, extensions, and tool registration.
    """
    if options is None:
        options = CreateAgentSessionOptions()

    cwd = options.cwd or os.getcwd()
    agent_dir = options.agent_dir or str(get_agent_dir())

    # Use provided or create core services
    auth_path = Path(agent_dir, "auth.json") if options.agent_dir else None
    models_path = Path(agent_dir, "models.json") if options.agent_dir else None
    auth_storage = options.auth_storage or AuthStorage.create(auth_path)
    model_registry = options.model_registry or ModelRegistry(auth_storage, models_path)
    settings_manager = options.settings_manager or SettingsManager.create(Path(cwd), Path(agent_dir))
    session_manager = options.session_manager or SessionManager.create(cwd)

    # Resource loader
    resource_loader = options.resource_loader
    if resource_loader is None:
        resource_loader = DefaultResourceLoader(
            DefaultResourceLoaderOptions(cwd=cwd, agent_dir=agent_dir, settings_manager=settings_manager)
        )
        await resource_loader.reload()

    # Check if session has existing data to restore
    existing_session = session_manager.build_session_context()
    has_existing_session = len(existing_session.messages) > 0
    has_thinking_entry = any(getattr(e, "type", None) == "thinking_level_change" for e in session_manager.get_branch())

    model = options.model
    model_fallback_message: str | None = None

    # If session has data, try to restore model from it
    if model is None and has_existing_session and existing_session.model is not None:
        restored = model_registry.find(
            existing_session.model["provider"],
            existing_session.model["model_id"],
        )
        if restored is not None and await model_registry.get_api_key(restored):
            model = restored
        if model is None:
            model_fallback_message = (
                f"Could not restore model {existing_session.model['provider']}/{existing_session.model['model_id']}"
            )

    # If still no model, use find_initial_model
    # Note: scopedModels is intentionally empty here (matching TS); the real
    # scoped_models are passed to AgentSession for model cycling.
    if model is None:
        result = await find_initial_model(
            {
                "scoped_models": [],
                "is_continuing": has_existing_session,
                "default_provider": settings_manager.get_default_provider(),
                "default_model_id": settings_manager.get_default_model(),
                "default_thinking_level": settings_manager.get_default_thinking_level(),
                "model_registry": model_registry,
            }
        )
        model = result.model
        if model is None:
            if model_fallback_message is None:
                model_fallback_message = "No models available. Use /login or set an API key environment variable."
        elif model_fallback_message is not None:
            model_fallback_message += f". Using {model.provider}/{model.id}"

    # Resolve thinking level
    thinking_level = options.thinking_level

    if thinking_level is None and has_existing_session:
        if has_thinking_entry:
            thinking_level = existing_session.thinking_level  # type: ignore[assignment]
        else:
            thinking_level = settings_manager.get_default_thinking_level() or DEFAULT_THINKING_LEVEL  # type: ignore[assignment]

    if thinking_level is None:
        thinking_level = settings_manager.get_default_thinking_level() or DEFAULT_THINKING_LEVEL  # type: ignore[assignment]

    # Clamp to model capabilities
    if model is None or not getattr(model, "reasoning", False):
        thinking_level = "off"

    # Tool names
    default_tool_names: list[str] = ["read", "bash", "edit", "write"]
    if options.tools is not None:
        from pi.coding_agent.core.tools import all_tools

        initial_tool_names = [t.name for t in options.tools if t.name in all_tools]
    else:
        initial_tool_names = default_tool_names

    # Build convert_to_llm wrapper with block-images defense-in-depth
    def _convert_to_llm_with_block_images(messages: list[AgentMessage]) -> list[Message]:
        converted = convert_to_llm(messages)  # type: ignore[arg-type]
        # Check setting dynamically so mid-session changes take effect
        if not settings_manager.get_block_images():
            return converted
        from pi.ai.types import ImageContent, TextContent, UserMessage

        filtered: list[Message] = []
        for msg in converted:
            if getattr(msg, "role", "") in ("user", "toolResult"):
                content = getattr(msg, "content", None)
                if isinstance(content, list):
                    has_images = any(isinstance(c, ImageContent) for c in content)
                    if has_images:
                        replaced: list[Any] = [
                            TextContent(text="Image reading is disabled.") if isinstance(c, ImageContent) else c
                            for c in content
                        ]
                        # Dedupe consecutive "Image reading is disabled." texts
                        deduped: list[Any] = []
                        for item in replaced:
                            if (
                                isinstance(item, TextContent)
                                and item.text == "Image reading is disabled."
                                and deduped
                                and isinstance(deduped[-1], TextContent)
                                and deduped[-1].text == "Image reading is disabled."
                            ):
                                continue
                            deduped.append(item)
                        msg = UserMessage(content=deduped)
                filtered.append(msg)
            else:
                filtered.append(msg)
        return filtered

    # Extension runner ref for transformContext
    extension_runner_ref: dict[str, Any] = {}

    # Build transformContext callback
    async def _transform_context(messages: list[AgentMessage], _signal: Any = None) -> list[AgentMessage]:
        runner = extension_runner_ref.get("current")
        if runner is None:
            return messages
        return runner.emit_context(messages)  # type: ignore[no-any-return]

    # Build get_api_key callback
    async def _get_api_key(provider: str) -> str:
        # Use the provider argument from the in-flight request;
        # agent.state.model may already be switched mid-turn.
        resolved_provider = provider or getattr(getattr(agent.state, "model", None), "provider", None)
        if not resolved_provider:
            raise RuntimeError("No model selected")
        key = await model_registry.get_api_key_for_provider(resolved_provider)
        if not key:
            current_model = getattr(agent.state, "model", None)
            is_oauth = current_model is not None and model_registry.is_using_oauth(current_model)
            if is_oauth:
                raise RuntimeError(
                    f'Authentication failed for "{resolved_provider}". '
                    f"Credentials may have expired or network is unavailable. "
                    f"Run '/login {resolved_provider}' to re-authenticate."
                )
            raise RuntimeError(
                f'No API key found for "{resolved_provider}". '
                f"Set an API key environment variable or run '/login {resolved_provider}'."
            )
        return key

    # Assemble tools: built-in coding tools (filtered by active names) + custom tools
    from pi.coding_agent.core.tools import create_all_tools

    all_builtin = create_all_tools(cwd)
    active_tools: list[Any] = [all_builtin[name] for name in initial_tool_names if name in all_builtin]
    if options.custom_tools:
        active_tools.extend(options.custom_tools)

    # Create agent
    initial_state = AgentState(
        system_prompt="",
        model=model or Model(),
        thinking_level=thinking_level or "off",
        tools=active_tools,
        messages=[],
        is_streaming=False,
        stream_message=None,
        pending_tool_calls=set(),
        error=None,
    )

    agent = Agent(
        AgentOptions(
            initial_state=initial_state,
            convert_to_llm=_convert_to_llm_with_block_images,
            session_id=session_manager.get_session_id(),
            transform_context=_transform_context,
            steering_mode=settings_manager.get_steering_mode(),
            follow_up_mode=settings_manager.get_follow_up_mode(),
            transport=settings_manager.get_transport(),  # type: ignore[arg-type]
            thinking_budgets=_convert_thinking_budgets(settings_manager),
            max_retry_delay_ms=settings_manager.get_retry_settings().get("max_delay_ms"),
            get_api_key=_get_api_key,
        )
    )

    # Restore messages or record initial state
    if has_existing_session:
        agent.replace_messages(existing_session.messages)  # type: ignore[arg-type]
        if not has_thinking_entry:
            session_manager.append_thinking_level_change(thinking_level or "off")
    else:
        if model is not None:
            session_manager.append_model_change(model.provider, model.id)
        session_manager.append_thinking_level_change(thinking_level or "off")

    # Create session
    session = AgentSession(
        AgentSessionConfig(
            agent=agent,
            session_manager=session_manager,
            cwd=cwd,
            settings_manager=settings_manager,
            scoped_models=options.scoped_models,
            resource_loader=resource_loader,
            custom_tools=options.custom_tools,
            model_registry=model_registry,
            initial_active_tool_names=initial_tool_names,
            extension_runner_ref=extension_runner_ref,
        )
    )

    extensions_result = resource_loader.get_extensions()

    return CreateAgentSessionResult(
        session=session,
        extensions_result=extensions_result,
        model_fallback_message=model_fallback_message,
    )


def _convert_thinking_budgets(settings_manager: SettingsManager) -> Any:
    """Convert settings thinking budgets to pi.ai ThinkingBudgets."""
    budgets = settings_manager.get_thinking_budgets()
    if budgets is None:
        return None
    from pi.ai.types import ThinkingBudgets

    return ThinkingBudgets(
        minimal=budgets.minimal,
        low=budgets.low,
        medium=budgets.medium,
        high=budgets.high,
    )
