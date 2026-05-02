"""coworker_config freeze tests.

Cover the contract:
  * canonical hashing — re-freezing produces the same sha256
  * order-independence — skill files / tools sorted before hashing
  * disabled skills excluded
  * unknown coworker → LookupError (RLS reduces wrong-tenant to
    not-found, which the CLI surfaces)

Skills are seeded via the production ``create_skill`` / ``update_skill``
helpers rather than raw SQL — that keeps the test honest against the
same path admin REST and the orchestrator use, including the CHECK
constraints and SECURITY DEFINER trigger.
"""

from __future__ import annotations

import uuid

import pytest

from rolemesh.core.types import SkillFile
from rolemesh.db import (
    create_coworker,
    create_skill,
    create_tenant,
    update_skill,
)
from rolemesh.evaluation.freeze import freeze_coworker_config

pytestmark = pytest.mark.usefixtures("test_db")


async def _seed_skill(
    *, tenant_id: str, coworker_id: str, name: str,
    files: dict[str, str], frontmatter_common: dict | None = None,
    enabled: bool = True,
) -> str:
    """Wrap ``create_skill`` with the test's flat ``{path: content}`` shape."""
    skill = await create_skill(
        tenant_id=tenant_id,
        coworker_id=coworker_id,
        name=name,
        frontmatter_common=dict(frontmatter_common or {}),
        frontmatter_backend={},
        files={
            path: SkillFile(path=path, content=content)
            for path, content in files.items()
        },
        enabled=enabled,
    )
    return skill.id


# ---------------------------------------------------------------------------
# Empty skills tree
# ---------------------------------------------------------------------------


async def test_freeze_empty_skills_when_coworker_has_none() -> None:
    """A coworker with no skills produces ``skills: []`` and a stable hash."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
        system_prompt="hi",
    )
    frozen = await freeze_coworker_config(cw.id, tenant_id=t.id)
    assert frozen.config["skills"] == []
    assert frozen.config["system_prompt"] == "hi"
    assert len(frozen.sha256) == 64  # sha256 hex digest


# ---------------------------------------------------------------------------
# Skills snapshot shape
# ---------------------------------------------------------------------------


async def test_freeze_includes_enabled_skill_files() -> None:
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    await _seed_skill(
        tenant_id=t.id, coworker_id=cw.id, name="alpha",
        files={"SKILL.md": "alpha body", "ref/notes.md": "beta"},
        frontmatter_common={"description": "alpha skill"},
    )
    frozen = await freeze_coworker_config(cw.id, tenant_id=t.id)
    skills = frozen.config["skills"]
    assert len(skills) == 1
    assert skills[0]["name"] == "alpha"
    # Files dict carries full contents — eval reproducibility hinges
    # on this; a name-only snapshot would let a silent skill edit go
    # unnoticed across runs.
    assert skills[0]["files"]["SKILL.md"] == "alpha body"
    assert skills[0]["files"]["ref/notes.md"] == "beta"


async def test_freeze_skips_disabled_skills() -> None:
    """Disabled skills are not bind-mounted into containers, so they
    must not appear in the snapshot either — including them would
    inflate the config sha and falsely diff against live behaviour."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    await _seed_skill(
        tenant_id=t.id, coworker_id=cw.id, name="enabled-one",
        files={"SKILL.md": "x"},
    )
    await _seed_skill(
        tenant_id=t.id, coworker_id=cw.id, name="disabled-one",
        files={"SKILL.md": "y"}, enabled=False,
    )
    frozen = await freeze_coworker_config(cw.id, tenant_id=t.id)
    names = [s["name"] for s in frozen.config["skills"]]
    assert names == ["enabled-one"]


async def test_freeze_hash_stable_across_calls() -> None:
    """Repeated freeze of unchanged coworker must produce identical
    sha — config-clustering depends on this. Mutation guard: sort_keys
    in canonical dump."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    await _seed_skill(
        tenant_id=t.id, coworker_id=cw.id, name="s1",
        files={"a.md": "1", "b.md": "2", "SKILL.md": "x"},
    )
    f1 = await freeze_coworker_config(cw.id, tenant_id=t.id)
    f2 = await freeze_coworker_config(cw.id, tenant_id=t.id)
    assert f1.sha256 == f2.sha256
    assert f1.config == f2.config


async def test_freeze_hash_changes_when_skill_edits() -> None:
    """A single byte change in a skill file body must move the hash —
    otherwise eval can't tell two runs apart by config."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    skill_id = await _seed_skill(
        tenant_id=t.id, coworker_id=cw.id, name="s1",
        files={"SKILL.md": "v1"},
    )
    h1 = (await freeze_coworker_config(cw.id, tenant_id=t.id)).sha256

    # update_skill with new files replaces the file set; SKILL.md must
    # always be present (application invariant enforced by create_skill).
    await update_skill(
        skill_id, tenant_id=t.id,
        files={"SKILL.md": SkillFile(path="SKILL.md", content="v2")},
    )
    h2 = (await freeze_coworker_config(cw.id, tenant_id=t.id)).sha256
    assert h1 != h2


async def test_freeze_unknown_coworker_raises_lookup_error() -> None:
    """RLS reduces wrong-tenant to not-found; the CLI relies on this
    to surface a clear error rather than silently freezing nothing."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    bogus_id = str(uuid.uuid4())
    with pytest.raises(LookupError):
        await freeze_coworker_config(bogus_id, tenant_id=t.id)


async def test_freeze_skill_file_order_deterministic() -> None:
    """Files are seeded in inverse alpha order via the dict iteration;
    freeze must still list them sorted so repeat freezes hash
    identically."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    # Insertion-order dict preserves z, m, a — the underlying scan
    # order. freeze must override it with sorted output.
    files = {"z.md": "z", "m.md": "m", "a.md": "a", "SKILL.md": "x"}
    await _seed_skill(
        tenant_id=t.id, coworker_id=cw.id, name="order-skill",
        files=files,
    )
    f1 = await freeze_coworker_config(cw.id, tenant_id=t.id)
    f2 = await freeze_coworker_config(cw.id, tenant_id=t.id)
    assert f1.sha256 == f2.sha256
    # Verify the canonical JSON keeps file paths in sorted order.
    files_dict = f1.config["skills"][0]["files"]
    assert list(files_dict.keys()) == sorted(files_dict.keys())
