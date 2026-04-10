"""RPC mode package — JSON-line protocol over stdin/stdout."""

from pi.coding_agent.modes.rpc.rpc_client import RpcClient
from pi.coding_agent.modes.rpc.rpc_mode import run_rpc_mode
from pi.coding_agent.modes.rpc.rpc_types import (
    RpcCommand,
    RpcCommandType,
    RpcExtensionUIRequest,
    RpcExtensionUIResponse,
    RpcResponse,
    RpcSessionState,
    RpcSlashCommand,
    deserialize_rpc_command,
    deserialize_rpc_extension_ui_response,
    serialize_rpc_extension_ui_request,
    serialize_rpc_response,
    serialize_rpc_session_state,
)

__all__ = [
    "RpcClient",
    "RpcCommand",
    "RpcCommandType",
    "RpcExtensionUIRequest",
    "RpcExtensionUIResponse",
    "RpcResponse",
    "RpcSessionState",
    "RpcSlashCommand",
    "deserialize_rpc_command",
    "deserialize_rpc_extension_ui_response",
    "run_rpc_mode",
    "serialize_rpc_extension_ui_request",
    "serialize_rpc_response",
    "serialize_rpc_session_state",
]
