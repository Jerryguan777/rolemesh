"""MCP tool-name contract — compose and restore LLM-visible names.

The LLM-visible name of an MCP tool is ``mcp__{server}__{tool}``.
Amazon Bedrock's Converse API caps tool names at 64 characters with
charset ``[a-zA-Z0-9_-]`` (source: AWS Bedrock Runtime API,
``ToolSpecification.name``); Anthropic-direct and OpenAI accept 128
and a wider charset. The 64-char contract is enforced HERE, for every
provider, rather than as a Bedrock-only rename: a per-provider name
would give the same tool different identities depending on which model
a coworker runs — splitting approval records, safety-policy matches,
logs, and prompt caches. Compliant names pass through verbatim; only
oversized or illegal-charset ones get an alias.

Aliasing rules (deterministic — stable across restarts and processes):

* The ``mcp__{server}__`` prefix is NEVER altered. Approval policies
  and reversibility maps parse the server segment out of it
  (``parse_mcp_tool_name`` / ``resolve_from_full_tool_name``); the
  server name itself is charset/length-validated at registration
  (``MCPServerCreate.name``).
* Only the trailing tool segment changes: illegal chars map to ``_``,
  and the segment is truncated to budget with a ``_`` + 7-hex-char
  SHA-1 suffix derived from the ORIGINAL full name. ANY alteration
  gets the hash suffix — a sanitise-only rename without it could
  collide with a sibling tool's real name (``a.b`` -> ``a_b`` vs a
  real ``a_b``) and mis-route policy lookups.
* Every alias is recorded in a module-level registry so hook-time
  policy / reversibility lookups can restore the ORIGINAL remote tool
  name (``restore_bare_tool_name``). The tool loader and the hook
  handlers run in the same agent_runner process, so module state is
  sufficient; a runtime that never composes aliases (claude backend —
  the Agent SDK names its own MCP tools) sees an empty registry and
  restoration is the identity function.

This module must stay stdlib-only: it is imported by hook handlers in
runtimes where ``pi.mcp``'s external deps (the ``mcp`` package) may
not be needed or installed.
"""

from __future__ import annotations

import hashlib
import logging
import re

logger = logging.getLogger(__name__)

# Bedrock Converse ToolSpecification.name cap — the strictest provider
# contract in the supported matrix, applied uniformly (see module doc).
TOOL_NAME_MAX = 64

_HASH_LEN = 7
_ILLEGAL = re.compile(r"[^a-zA-Z0-9_-]")

# (server_name, alias bare segment) -> original remote tool name.
_ALIAS_REGISTRY: dict[tuple[str, str], str] = {}


def compose_llm_tool_name(server_name: str, remote_tool_name: str) -> str:
    """Return the LLM-visible name for a remote MCP tool (aliased if needed).

    Raises ``ValueError`` when the server name alone leaves no room for
    any tool segment — impossible for names passing the registration
    validator, so failing loud beats silently mangling the prefix.
    """
    prefix = f"mcp__{server_name}__"
    plain = prefix + remote_tool_name
    budget = TOOL_NAME_MAX - len(prefix)
    if budget < _HASH_LEN + 2:
        raise ValueError(
            f"MCP server name {server_name!r} is too long to fit any tool "
            f"under the {TOOL_NAME_MAX}-char tool-name contract: prefix "
            f"{prefix!r} leaves only {budget} chars for the tool segment."
        )

    seg = _ILLEGAL.sub("_", remote_tool_name)
    if seg == remote_tool_name and len(plain) <= TOOL_NAME_MAX:
        return plain

    digest = hashlib.sha1(plain.encode("utf-8")).hexdigest()[:_HASH_LEN]
    seg = f"{seg[: budget - _HASH_LEN - 1]}_{digest}"
    _ALIAS_REGISTRY[(server_name, seg)] = remote_tool_name
    logger.warning(
        "MCP tool %r on server %r exceeds the %d-char/charset tool-name "
        "contract; exposing it to the model as %r",
        remote_tool_name,
        server_name,
        TOOL_NAME_MAX,
        prefix + seg,
    )
    return prefix + seg


def restore_bare_tool_name(server_name: str, bare_name: str) -> str:
    """Map an alias bare segment back to the original remote tool name.

    Identity for non-aliased names, so callers can apply it
    unconditionally before matching approval policies or reversibility
    maps — both of which operators configure against ORIGINAL remote
    tool names.
    """
    return _ALIAS_REGISTRY.get((server_name, bare_name), bare_name)
