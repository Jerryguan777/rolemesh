"""Freeze a Coworker's behavior-affecting config into a JSON-able dict.

Inlined into ``eval_runs.coworker_config`` so an eval is reproducible
even after the live Coworker is edited or deleted. The hash over a
canonical JSON serialization (``coworker_config_sha256``) lets the
``rolemesh-eval list`` command cluster runs that share a configuration.

Skills are queried from the new ``skills`` / ``skill_files`` schema (see
``docs/skills-architecture.md``). The legacy ``coworkers.skills`` JSONB
column is intentionally NOT consulted — it only holds names and would
miss the actual file contents that drive container behavior.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rolemesh.db.pg import get_coworker, tenant_conn

if TYPE_CHECKING:
    from rolemesh.core.types import Coworker


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


def _coworker_to_dict(c: Coworker) -> dict[str, Any]:
    """Behavior-affecting fields only — name/folder/created_at omitted.

    ``tools`` are MCP server configs that drive what tools the agent can
    invoke; ``permissions`` and ``agent_role`` gate which tools the
    container actually permits. ``container_config`` carries timeout
    and resource limits that change runtime semantics. Anything purely
    cosmetic (id, name, status, created_at) is left out so two
    differently-named coworkers with identical behavior produce the same
    hash.
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
                # the credential proxy); leaking the header dict here
                # is the same surface as ``coworkers.tools`` in DB.
                "headers": dict(t.headers),
                "auth_mode": t.auth_mode,
                "tool_reversibility": dict(t.tool_reversibility),
            }
            for t in c.tools
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


async def _freeze_skills(
    tenant_id: str, coworker_id: str
) -> list[dict[str, Any]]:
    """Snapshot enabled skills with their full file trees.

    Mirrors the orchestrator's container-time projection: only enabled
    skills, ordered by ``name`` then file ``path`` for determinism so
    the resulting JSON hash is stable across runs that don't actually
    differ. ``frontmatter_common`` and ``frontmatter_backend`` are kept
    separate (rather than pre-merged) so the config snapshot reflects
    what the DB held, not what the active backend would see — that lets
    the same snapshot describe a Coworker switched between backends.

    Skills tables ship in the ``feat/skills`` branch (PR 1). Until that
    lands on main, this function tolerates their absence by returning an
    empty list — running eval against a coworker on a build without the
    skills schema produces no skill snapshot rather than a crash.

    The query shape (column names, ``enabled = TRUE`` filter, ordering)
    matches the real PR 1 DDL exactly, verified via dev-DB e2e against
    the ``feat/skills`` schema. Once that PR lands on main, the
    ``information_schema`` guard becomes dead code and should be deleted
    so a future schema mismatch fails loud instead of silent-emptying.
    """
    async with tenant_conn(tenant_id) as conn:
        # Defensive presence check — the parallel worktree introduces
        # these tables; running eval before that lands should not
        # crash. Empty list signals "no skills snapshotted" which is a
        # legitimate state (Coworker may genuinely have no skills).
        has_tables = await conn.fetchval(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.tables "
            "   WHERE table_name = 'skills'"
            ")"
        )
        if not has_tables:
            return []
        skill_rows = await conn.fetch(
            """
            SELECT id, name, frontmatter_common, frontmatter_backend, enabled
              FROM skills
             WHERE coworker_id = $1::uuid AND enabled = TRUE
             ORDER BY name
            """,
            coworker_id,
        )
        out: list[dict[str, Any]] = []
        for sr in skill_rows:
            file_rows = await conn.fetch(
                """
                SELECT path, content
                  FROM skill_files
                 WHERE skill_id = $1::uuid
                 ORDER BY path
                """,
                sr["id"],
            )
            fc_raw = sr["frontmatter_common"]
            fb_raw = sr["frontmatter_backend"]
            out.append(
                {
                    "name": sr["name"],
                    "enabled": bool(sr["enabled"]),
                    "frontmatter_common": (
                        fc_raw if isinstance(fc_raw, dict)
                        else json.loads(fc_raw) if fc_raw else {}
                    ),
                    "frontmatter_backend": (
                        fb_raw if isinstance(fb_raw, dict)
                        else json.loads(fb_raw) if fb_raw else {}
                    ),
                    "files": {
                        fr["path"]: fr["content"] for fr in file_rows
                    },
                }
            )
    return out


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
    config = _coworker_to_dict(coworker)
    config["skills"] = await _freeze_skills(tenant_id, coworker_id)
    return FrozenConfig(config=config, sha256=_hash_config(config))
