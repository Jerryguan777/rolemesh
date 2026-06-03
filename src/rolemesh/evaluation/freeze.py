"""Freeze a Coworker's behavior-affecting config into a JSON-able dict.

Inlined into ``eval_runs.coworker_config`` so an eval is reproducible
even after the live Coworker is edited or deleted. The hash over a
canonical JSON serialization (``coworker_config_sha256``) lets the
``rolemesh-eval list`` command cluster runs that share a configuration.

MCP bindings are read via ``list_coworker_mcp_configs`` (the
``coworker_mcp_servers`` JOIN ``mcp_servers`` projection) so the
freeze stays in lockstep with the orchestrator's runtime view. Skills
go through ``rolemesh.db.list_skills_for_coworker`` for the same
parity reason — same ``enabled_only`` filter, same name-ordered scan,
same per-file content fetch. ``frontmatter_common`` and
``frontmatter_backend`` are kept separate (rather than pre-merged into
the active backend's view) so one snapshot describes a Coworker
switched between backends.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rolemesh.db import (
    get_coworker,
    list_coworker_mcp_configs,
    list_skills_for_coworker,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rolemesh.core.types import Coworker, McpServerConfig, Skill


@dataclass(frozen=True)
class FrozenConfig:
    """Output of ``freeze_coworker_config``."""

    config: dict[str, Any]
    sha256: str


def _canonical_dumps(obj: Any) -> str:
    """Stable JSON dump for hashing.

    sort_keys keeps two equivalent dicts hashing identically; separators
    drop whitespace; ensure_ascii=False keeps multibyte content readable
    in DB and on disk.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_config(config: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_dumps(config).encode("utf-8")).hexdigest()


def _coworker_to_dict(
    c: Coworker, mcp_configs: Sequence[McpServerConfig],
) -> dict[str, Any]:
    """Behavior-affecting fields only — name/folder/created_at omitted.

    ``tools`` come from the relation-table projection (not the dropped
    JSONB column); ``permissions`` and ``agent_role`` gate which tools
    the container actually permits. ``container_config`` carries
    timeout and resource limits that change runtime semantics. Anything
    purely cosmetic (id, name, status, created_at) is left out so two
    differently-named coworkers with identical behavior produce the
    same hash.
    """
    perms = c.permissions
    perms_dict = perms.to_dict() if perms is not None else {}
    return {
        "agent_backend": c.agent_backend,
        "system_prompt": c.system_prompt,
        "agent_role": c.agent_role,
        "permissions": perms_dict,
        "tools": [
            {
                "name": t.name,
                "type": t.type,
                "url": t.url,
                # headers + auth_mode + tool_reversibility all change
                # how the container talks to MCP, so they participate
                # in the hash. Secrets are NOT in headers (those go via
                # the credential proxy); the per-MCP-server config that
                # this snapshot mirrors lives in ``mcp_servers``.
                "headers": dict(t.headers),
                "auth_mode": t.auth_mode,
                "tool_reversibility": dict(t.tool_reversibility),
            }
            for t in mcp_configs
        ],
        "container_config": (
            {
                "timeout": c.container_config.timeout,
                "runtime": c.container_config.runtime,
                "memory_limit": c.container_config.memory_limit,
                "cpu_limit": c.container_config.cpu_limit,
            }
            if c.container_config
            else None
        ),
    }


def _skill_to_dict(s: Skill) -> dict[str, Any]:
    """Project a ``Skill`` dataclass to the JSON shape we hash and store.

    Files are sorted by path for hash determinism — the production CRUD
    helper already orders the SELECT, but we re-sort here as a
    defense-in-depth so a future API change can't silently shift hashes.
    """
    return {
        "name": s.name,
        "enabled": s.enabled,
        "frontmatter_common": dict(s.frontmatter_common),
        "frontmatter_backend": {
            backend: dict(overrides)
            for backend, overrides in s.frontmatter_backend.items()
        },
        "files": {path: s.files[path].content for path in sorted(s.files)},
    }


async def freeze_coworker_config(
    coworker_id: str, *, tenant_id: str
) -> FrozenConfig:
    """Read DB and produce a hashable, JSON-able snapshot.

    Raises ``LookupError`` when the coworker is not visible to
    ``tenant_id`` — RLS reduces "wrong tenant" to "not found", which is
    fail-loud enough for a CLI: the caller surfaces the error rather
    than silently freezing a stranger's coworker.
    """
    coworker = await get_coworker(coworker_id, tenant_id=tenant_id)
    if coworker is None:
        msg = f"coworker {coworker_id!r} not found in tenant {tenant_id!r}"
        raise LookupError(msg)
    mcp_configs = await list_coworker_mcp_configs(
        coworker_id, tenant_id=tenant_id,
    )
    config = _coworker_to_dict(coworker, mcp_configs)
    skills = await list_skills_for_coworker(
        coworker_id, tenant_id=tenant_id, enabled_only=True, with_files=True,
    )
    config["skills"] = [_skill_to_dict(s) for s in skills]
    return FrozenConfig(config=config, sha256=_hash_config(config))
