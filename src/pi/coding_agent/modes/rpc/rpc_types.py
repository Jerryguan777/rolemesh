"""RPC type definitions — Python port of packages/coding-agent/src/modes/rpc/rpc-types.ts.

Discriminated-union commands and responses are represented as typed dataclasses
with a Literal ``type`` field. Serialization/deserialization functions convert
between dataclass instances and plain JSON-serializable dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pi.agent.types import ThinkingLevel
from pi.ai.types import ImageContent, Model, deserialize_model, serialize_model

# ---------------------------------------------------------------------------
# RpcCommand variants
# ---------------------------------------------------------------------------


@dataclass
class RpcPromptCommand:
    type: Literal["prompt"] = "prompt"
    message: str = ""
    images: list[ImageContent] = field(default_factory=list)
    streaming_behavior: Literal["steer", "followUp"] | None = None
    id: str | None = None


@dataclass
class RpcSteerCommand:
    type: Literal["steer"] = "steer"
    message: str = ""
    images: list[ImageContent] = field(default_factory=list)
    id: str | None = None


@dataclass
class RpcFollowUpCommand:
    type: Literal["follow_up"] = "follow_up"
    message: str = ""
    images: list[ImageContent] = field(default_factory=list)
    id: str | None = None


@dataclass
class RpcAbortCommand:
    type: Literal["abort"] = "abort"
    id: str | None = None


@dataclass
class RpcNewSessionCommand:
    type: Literal["new_session"] = "new_session"
    parent_session: str | None = None
    id: str | None = None


@dataclass
class RpcGetStateCommand:
    type: Literal["get_state"] = "get_state"
    id: str | None = None


@dataclass
class RpcSetModelCommand:
    type: Literal["set_model"] = "set_model"
    provider: str = ""
    model_id: str = ""
    id: str | None = None


@dataclass
class RpcCycleModelCommand:
    type: Literal["cycle_model"] = "cycle_model"
    id: str | None = None


@dataclass
class RpcGetAvailableModelsCommand:
    type: Literal["get_available_models"] = "get_available_models"
    id: str | None = None


@dataclass
class RpcSetThinkingLevelCommand:
    type: Literal["set_thinking_level"] = "set_thinking_level"
    level: ThinkingLevel = "off"
    id: str | None = None


@dataclass
class RpcCycleThinkingLevelCommand:
    type: Literal["cycle_thinking_level"] = "cycle_thinking_level"
    id: str | None = None


@dataclass
class RpcSetSteeringModeCommand:
    type: Literal["set_steering_mode"] = "set_steering_mode"
    mode: Literal["all", "one-at-a-time"] = "all"
    id: str | None = None


@dataclass
class RpcSetFollowUpModeCommand:
    type: Literal["set_follow_up_mode"] = "set_follow_up_mode"
    mode: Literal["all", "one-at-a-time"] = "all"
    id: str | None = None


@dataclass
class RpcCompactCommand:
    type: Literal["compact"] = "compact"
    custom_instructions: str | None = None
    id: str | None = None


@dataclass
class RpcSetAutoCompactionCommand:
    type: Literal["set_auto_compaction"] = "set_auto_compaction"
    enabled: bool = True
    id: str | None = None


@dataclass
class RpcSetAutoRetryCommand:
    type: Literal["set_auto_retry"] = "set_auto_retry"
    enabled: bool = True
    id: str | None = None


@dataclass
class RpcAbortRetryCommand:
    type: Literal["abort_retry"] = "abort_retry"
    id: str | None = None


@dataclass
class RpcBashCommand:
    type: Literal["bash"] = "bash"
    command: str = ""
    id: str | None = None


@dataclass
class RpcAbortBashCommand:
    type: Literal["abort_bash"] = "abort_bash"
    id: str | None = None


@dataclass
class RpcGetSessionStatsCommand:
    type: Literal["get_session_stats"] = "get_session_stats"
    id: str | None = None


@dataclass
class RpcExportHtmlCommand:
    type: Literal["export_html"] = "export_html"
    output_path: str | None = None
    id: str | None = None


@dataclass
class RpcSwitchSessionCommand:
    type: Literal["switch_session"] = "switch_session"
    session_path: str = ""
    id: str | None = None


@dataclass
class RpcForkCommand:
    type: Literal["fork"] = "fork"
    entry_id: str = ""
    id: str | None = None


@dataclass
class RpcGetForkMessagesCommand:
    type: Literal["get_fork_messages"] = "get_fork_messages"
    id: str | None = None


@dataclass
class RpcGetLastAssistantTextCommand:
    type: Literal["get_last_assistant_text"] = "get_last_assistant_text"
    id: str | None = None


@dataclass
class RpcSetSessionNameCommand:
    type: Literal["set_session_name"] = "set_session_name"
    name: str = ""
    id: str | None = None


@dataclass
class RpcGetMessagesCommand:
    type: Literal["get_messages"] = "get_messages"
    id: str | None = None


@dataclass
class RpcGetCommandsCommand:
    type: Literal["get_commands"] = "get_commands"
    id: str | None = None


# Union of all RPC command types
RpcCommand = (
    RpcPromptCommand
    | RpcSteerCommand
    | RpcFollowUpCommand
    | RpcAbortCommand
    | RpcNewSessionCommand
    | RpcGetStateCommand
    | RpcSetModelCommand
    | RpcCycleModelCommand
    | RpcGetAvailableModelsCommand
    | RpcSetThinkingLevelCommand
    | RpcCycleThinkingLevelCommand
    | RpcSetSteeringModeCommand
    | RpcSetFollowUpModeCommand
    | RpcCompactCommand
    | RpcSetAutoCompactionCommand
    | RpcSetAutoRetryCommand
    | RpcAbortRetryCommand
    | RpcBashCommand
    | RpcAbortBashCommand
    | RpcGetSessionStatsCommand
    | RpcExportHtmlCommand
    | RpcSwitchSessionCommand
    | RpcForkCommand
    | RpcGetForkMessagesCommand
    | RpcGetLastAssistantTextCommand
    | RpcSetSessionNameCommand
    | RpcGetMessagesCommand
    | RpcGetCommandsCommand
)

RpcCommandType = Literal[
    "prompt",
    "steer",
    "follow_up",
    "abort",
    "new_session",
    "get_state",
    "set_model",
    "cycle_model",
    "get_available_models",
    "set_thinking_level",
    "cycle_thinking_level",
    "set_steering_mode",
    "set_follow_up_mode",
    "compact",
    "set_auto_compaction",
    "set_auto_retry",
    "abort_retry",
    "bash",
    "abort_bash",
    "get_session_stats",
    "export_html",
    "switch_session",
    "fork",
    "get_fork_messages",
    "get_last_assistant_text",
    "set_session_name",
    "get_messages",
    "get_commands",
]

# ---------------------------------------------------------------------------
# RpcSlashCommand
# ---------------------------------------------------------------------------


@dataclass
class RpcSlashCommand:
    name: str = ""
    description: str | None = None
    source: Literal["extension", "prompt", "skill"] = "prompt"
    location: Literal["user", "project", "path"] | None = None
    path: str | None = None


# ---------------------------------------------------------------------------
# RpcSessionState
# ---------------------------------------------------------------------------


@dataclass
class RpcSessionState:
    thinking_level: ThinkingLevel = "off"
    is_streaming: bool = False
    is_compacting: bool = False
    steering_mode: Literal["all", "one-at-a-time"] = "all"
    follow_up_mode: Literal["all", "one-at-a-time"] = "all"
    session_id: str = ""
    auto_compaction_enabled: bool = False
    message_count: int = 0
    pending_message_count: int = 0
    model: Model | None = None
    session_file: str | None = None
    session_name: str | None = None


# ---------------------------------------------------------------------------
# RpcResponse
# ---------------------------------------------------------------------------


@dataclass
class RpcResponse:
    type: Literal["response"] = "response"
    command: str = ""
    success: bool = True
    data: Any = None
    error: str | None = None
    id: str | None = None


# ---------------------------------------------------------------------------
# RpcExtensionUIRequest / RpcExtensionUIResponse
# ---------------------------------------------------------------------------


@dataclass
class RpcExtensionUIRequest:
    """Request sent from the server to the client for UI interaction."""

    type: str = ""
    request_id: str = ""
    data: Any = None


@dataclass
class RpcExtensionUIResponse:
    """Response sent from the client back to the server after UI interaction."""

    request_id: str = ""
    data: Any = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize_image_content(img: ImageContent) -> dict[str, Any]:
    return {"type": "image", "data": img.data, "mimeType": img.mime_type}


def _deserialize_image_content(raw: dict[str, Any]) -> ImageContent:
    return ImageContent(data=raw.get("data", ""), mime_type=raw.get("mimeType", ""))


def serialize_rpc_session_state(state: RpcSessionState) -> dict[str, Any]:
    result: dict[str, Any] = {
        "thinkingLevel": state.thinking_level,
        "isStreaming": state.is_streaming,
        "isCompacting": state.is_compacting,
        "steeringMode": state.steering_mode,
        "followUpMode": state.follow_up_mode,
        "sessionId": state.session_id,
        "autoCompactionEnabled": state.auto_compaction_enabled,
        "messageCount": state.message_count,
        "pendingMessageCount": state.pending_message_count,
    }
    if state.model is not None:
        result["model"] = serialize_model(state.model)
    if state.session_file is not None:
        result["sessionFile"] = state.session_file
    if state.session_name is not None:
        result["sessionName"] = state.session_name
    return result


def deserialize_rpc_session_state(data: dict[str, Any]) -> RpcSessionState:
    model: Model | None = None
    if "model" in data and data["model"] is not None:
        model = deserialize_model(data["model"])
    return RpcSessionState(
        thinking_level=data.get("thinkingLevel", "off"),
        is_streaming=data.get("isStreaming", False),
        is_compacting=data.get("isCompacting", False),
        steering_mode=data.get("steeringMode", "all"),
        follow_up_mode=data.get("followUpMode", "all"),
        session_id=data.get("sessionId", ""),
        auto_compaction_enabled=data.get("autoCompactionEnabled", False),
        message_count=data.get("messageCount", 0),
        pending_message_count=data.get("pendingMessageCount", 0),
        model=model,
        session_file=data.get("sessionFile"),
        session_name=data.get("sessionName"),
    )


def serialize_rpc_response(response: RpcResponse) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "response",
        "command": response.command,
        "success": response.success,
    }
    if response.data is not None:
        result["data"] = response.data
    if response.error is not None:
        result["error"] = response.error
    if response.id is not None:
        result["id"] = response.id
    return result


def deserialize_rpc_response(data: dict[str, Any]) -> RpcResponse:
    return RpcResponse(
        command=data.get("command", ""),
        success=data.get("success", True),
        data=data.get("data"),
        error=data.get("error"),
        id=data.get("id"),
    )


def serialize_rpc_extension_ui_request(req: RpcExtensionUIRequest) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": req.type,
        "requestId": req.request_id,
    }
    if req.data is not None:
        result["data"] = req.data
    return result


def deserialize_rpc_extension_ui_response(data: dict[str, Any]) -> RpcExtensionUIResponse:
    return RpcExtensionUIResponse(
        request_id=data.get("requestId", ""),
        data=data.get("data"),
        error=data.get("error"),
    )


def deserialize_rpc_command(data: dict[str, Any]) -> RpcCommand:
    """Deserialize a plain dict into the appropriate RpcCommand dataclass."""
    cmd_type = data.get("type", "")
    cmd_id: str | None = data.get("id")

    if cmd_type == "prompt":
        raw_images = data.get("images", [])
        images = [_deserialize_image_content(img) for img in raw_images]
        sb = data.get("streamingBehavior")
        streaming_behavior: Literal["steer", "followUp"] | None = None
        if sb == "steer":
            streaming_behavior = "steer"
        elif sb == "followUp":
            streaming_behavior = "followUp"
        return RpcPromptCommand(
            message=data.get("message", ""),
            images=images,
            streaming_behavior=streaming_behavior,
            id=cmd_id,
        )
    if cmd_type == "steer":
        raw_images = data.get("images", [])
        return RpcSteerCommand(
            message=data.get("message", ""),
            images=[_deserialize_image_content(img) for img in raw_images],
            id=cmd_id,
        )
    if cmd_type == "follow_up":
        raw_images = data.get("images", [])
        return RpcFollowUpCommand(
            message=data.get("message", ""),
            images=[_deserialize_image_content(img) for img in raw_images],
            id=cmd_id,
        )
    if cmd_type == "abort":
        return RpcAbortCommand(id=cmd_id)
    if cmd_type == "new_session":
        return RpcNewSessionCommand(parent_session=data.get("parentSession"), id=cmd_id)
    if cmd_type == "get_state":
        return RpcGetStateCommand(id=cmd_id)
    if cmd_type == "set_model":
        return RpcSetModelCommand(provider=data.get("provider", ""), model_id=data.get("modelId", ""), id=cmd_id)
    if cmd_type == "cycle_model":
        return RpcCycleModelCommand(id=cmd_id)
    if cmd_type == "get_available_models":
        return RpcGetAvailableModelsCommand(id=cmd_id)
    if cmd_type == "set_thinking_level":
        level_raw = data.get("level", "off")
        level: ThinkingLevel = level_raw if level_raw in ("off", "minimal", "low", "medium", "high", "xhigh") else "off"
        return RpcSetThinkingLevelCommand(level=level, id=cmd_id)
    if cmd_type == "cycle_thinking_level":
        return RpcCycleThinkingLevelCommand(id=cmd_id)
    if cmd_type == "set_steering_mode":
        mode_raw = data.get("mode", "all")
        s_mode: Literal["all", "one-at-a-time"] = "one-at-a-time" if mode_raw == "one-at-a-time" else "all"
        return RpcSetSteeringModeCommand(mode=s_mode, id=cmd_id)
    if cmd_type == "set_follow_up_mode":
        mode_raw = data.get("mode", "all")
        f_mode: Literal["all", "one-at-a-time"] = "one-at-a-time" if mode_raw == "one-at-a-time" else "all"
        return RpcSetFollowUpModeCommand(mode=f_mode, id=cmd_id)
    if cmd_type == "compact":
        return RpcCompactCommand(custom_instructions=data.get("customInstructions"), id=cmd_id)
    if cmd_type == "set_auto_compaction":
        return RpcSetAutoCompactionCommand(enabled=data.get("enabled", True), id=cmd_id)
    if cmd_type == "set_auto_retry":
        return RpcSetAutoRetryCommand(enabled=data.get("enabled", True), id=cmd_id)
    if cmd_type == "abort_retry":
        return RpcAbortRetryCommand(id=cmd_id)
    if cmd_type == "bash":
        return RpcBashCommand(command=data.get("command", ""), id=cmd_id)
    if cmd_type == "abort_bash":
        return RpcAbortBashCommand(id=cmd_id)
    if cmd_type == "get_session_stats":
        return RpcGetSessionStatsCommand(id=cmd_id)
    if cmd_type == "export_html":
        return RpcExportHtmlCommand(output_path=data.get("outputPath"), id=cmd_id)
    if cmd_type == "switch_session":
        return RpcSwitchSessionCommand(session_path=data.get("sessionPath", ""), id=cmd_id)
    if cmd_type == "fork":
        return RpcForkCommand(entry_id=data.get("entryId", ""), id=cmd_id)
    if cmd_type == "get_fork_messages":
        return RpcGetForkMessagesCommand(id=cmd_id)
    if cmd_type == "get_last_assistant_text":
        return RpcGetLastAssistantTextCommand(id=cmd_id)
    if cmd_type == "set_session_name":
        return RpcSetSessionNameCommand(name=data.get("name", ""), id=cmd_id)
    if cmd_type == "get_messages":
        return RpcGetMessagesCommand(id=cmd_id)
    if cmd_type == "get_commands":
        return RpcGetCommandsCommand(id=cmd_id)

    raise ValueError(f"Unknown RPC command type: {cmd_type!r}")
