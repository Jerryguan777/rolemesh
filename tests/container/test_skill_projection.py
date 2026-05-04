"""Skill projection at container spawn time.

Covers the materialization pipeline end-to-end against a real
postgres testcontainer: per-backend target paths, frontmatter
merge filtering (Claude-only fields stay out of Pi projections
and vice versa), multi-file atomic rename, projection-time
path-traversal defense (realpath stay-inside + symlink
rejection), cleanup on normal exit, and orphan sweep on
``kill -9``-style abandonment.

Tests deliberately call ``materialize_skills_for_spawn`` directly
and inspect the host filesystem rather than spawning a real
container — that integration is covered by PR 4's e2e test.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import yaml

from rolemesh.container.skill_projection import (
    CONTAINER_TARGETS,
    SPAWN_ROOT,
    cleanup_orphan_spawns,
    cleanup_spawn_skills,
    materialize_skills_for_spawn,
)
from rolemesh.core.skills import SkillValidationError
from rolemesh.core.types import SkillFile
from rolemesh.db import (
    create_coworker,
    create_skill,
    create_tenant,
    get_coworker,
)

pytestmark = pytest.mark.usefixtures("test_db")


_GOOD_DESC = (
    "When the user message contains the literal token "
    "EXECUTE_TEST_SKILL_42, run the demonstration workflow."
)


async def _make_coworker(tag: str, *, agent_backend: str = "claude") -> tuple[str, str]:
    t = await create_tenant(name=f"T{tag}", slug=f"sp-{tag}-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id,
        name=f"CW{tag}",
        folder=f"cw-{tag}-{uuid.uuid4().hex[:6]}",
        agent_backend=agent_backend,
    )
    return t.id, cw.id


async def _projected_coworker(tag: str, *, agent_backend: str) -> tuple[str, str]:
    """Make a coworker via the DB CRUD then re-fetch through ``get_coworker``
    so the dataclass contains the chosen ``agent_backend`` (the projector
    reads from ``coworker.agent_backend`` only — but we route through the
    backend kwarg so this is mostly for symmetry).
    """
    return await _make_coworker(tag, agent_backend=agent_backend)


# ---------------------------------------------------------------------------
# Per-backend target path
# ---------------------------------------------------------------------------


async def test_projects_to_claude_path_for_claude_backend() -> None:
    tenant_id, coworker_id = await _projected_coworker("clp", agent_backend="claude")
    await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="echo",
        frontmatter_common={"name": "echo", "description": _GOOD_DESC},
        frontmatter_backend={},
        files={"SKILL.md": SkillFile(path="SKILL.md", content="# body")},
    )
    coworker = await get_coworker(coworker_id, tenant_id=tenant_id)
    assert coworker is not None
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    try:
        mount = await materialize_skills_for_spawn(
            coworker, job_id, backend="claude"
        )
        assert mount is not None
        assert mount.readonly is True
        assert mount.container_path == "/home/agent/.claude/skills"
        assert (Path(mount.host_path) / "echo" / "SKILL.md").exists()
    finally:
        cleanup_spawn_skills(job_id)


async def test_projects_to_pi_path_for_pi_backend() -> None:
    tenant_id, coworker_id = await _projected_coworker("pip", agent_backend="pi")
    await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="echo",
        frontmatter_common={"name": "echo", "description": _GOOD_DESC},
        frontmatter_backend={},
        files={"SKILL.md": SkillFile(path="SKILL.md", content="# body")},
    )
    coworker = await get_coworker(coworker_id, tenant_id=tenant_id)
    assert coworker is not None
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    try:
        mount = await materialize_skills_for_spawn(coworker, job_id, backend="pi")
        assert mount is not None
        assert mount.container_path == "/home/agent/.pi/skills"
    finally:
        cleanup_spawn_skills(job_id)


async def test_unknown_backend_rejected() -> None:
    tenant_id, coworker_id = await _make_coworker("unk")
    coworker = await get_coworker(coworker_id, tenant_id=tenant_id)
    assert coworker is not None
    with pytest.raises(SkillValidationError, match="unknown backend"):
        await materialize_skills_for_spawn(
            coworker, "j", backend="crystalball"
        )


# ---------------------------------------------------------------------------
# Frontmatter merge — backend filtering
# ---------------------------------------------------------------------------


async def test_pi_projection_drops_claude_specific_fields() -> None:
    tenant_id, coworker_id = await _projected_coworker("dpf", agent_backend="pi")
    await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="multi",
        frontmatter_common={"name": "multi", "description": _GOOD_DESC},
        frontmatter_backend={
            "claude": {"argument-hint": "[file]"},
            "pi": {"disable_model_invocation": False},
        },
        files={"SKILL.md": SkillFile(path="SKILL.md", content="# body")},
    )
    coworker = await get_coworker(coworker_id, tenant_id=tenant_id)
    assert coworker is not None
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    try:
        mount = await materialize_skills_for_spawn(coworker, job_id, backend="pi")
        assert mount is not None
        skill_md_text = (Path(mount.host_path) / "multi" / "SKILL.md").read_text()
        # Strip the leading ---..--- block and parse it.
        assert skill_md_text.startswith("---\n")
        _, fm_block, body = skill_md_text.split("---\n", 2)
        merged = yaml.safe_load(fm_block)
        assert merged["name"] == "multi"
        assert merged["disable_model_invocation"] is False
        assert "argument-hint" not in merged, (
            "Claude-only field leaked into Pi projection"
        )
        assert body.startswith("# body")
    finally:
        cleanup_spawn_skills(job_id)


async def test_claude_projection_drops_pi_specific_fields() -> None:
    tenant_id, coworker_id = await _projected_coworker("dcf", agent_backend="claude")
    await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="multi",
        frontmatter_common={"name": "multi", "description": _GOOD_DESC},
        frontmatter_backend={
            "claude": {"argument-hint": "[file]"},
            "pi": {"disable_model_invocation": True},
        },
        files={"SKILL.md": SkillFile(path="SKILL.md", content="# body")},
    )
    coworker = await get_coworker(coworker_id, tenant_id=tenant_id)
    assert coworker is not None
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    try:
        mount = await materialize_skills_for_spawn(
            coworker, job_id, backend="claude"
        )
        assert mount is not None
        skill_md_text = (Path(mount.host_path) / "multi" / "SKILL.md").read_text()
        _, fm_block, _ = skill_md_text.split("---\n", 2)
        merged = yaml.safe_load(fm_block)
        assert merged["argument-hint"] == "[file]"
        assert "disable_model_invocation" not in merged
    finally:
        cleanup_spawn_skills(job_id)


# ---------------------------------------------------------------------------
# Multi-file projection
# ---------------------------------------------------------------------------


async def test_projects_supporting_files_verbatim() -> None:
    tenant_id, coworker_id = await _make_coworker("mfp")
    await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="three-files",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={
            "SKILL.md": SkillFile(path="SKILL.md", content="# entry"),
            "reference.md": SkillFile(path="reference.md", content="## Detail\n"),
            "scripts/helper.py": SkillFile(
                path="scripts/helper.py", content="print('hello')\n"
            ),
        },
    )
    coworker = await get_coworker(coworker_id, tenant_id=tenant_id)
    assert coworker is not None
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    try:
        mount = await materialize_skills_for_spawn(
            coworker, job_id, backend="claude"
        )
        assert mount is not None
        skill_root = Path(mount.host_path) / "three-files"
        assert (skill_root / "reference.md").read_text() == "## Detail\n"
        assert (skill_root / "scripts" / "helper.py").read_text() == (
            "print('hello')\n"
        )
        # Permissions: 0644 on files, 0755 on parent dirs (so the
        # agent UID can traverse).
        scripts_dir = skill_root / "scripts"
        assert scripts_dir.is_dir()
        assert (skill_root / "reference.md").stat().st_mode & 0o777 == 0o644
    finally:
        cleanup_spawn_skills(job_id)


# ---------------------------------------------------------------------------
# Disabled skills
# ---------------------------------------------------------------------------


async def test_disabled_skills_are_not_projected() -> None:
    tenant_id, coworker_id = await _make_coworker("dis")
    await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="enabled",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={"SKILL.md": SkillFile(path="SKILL.md", content="x")},
        enabled=True,
    )
    await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="disabled",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={"SKILL.md": SkillFile(path="SKILL.md", content="x")},
        enabled=False,
    )
    coworker = await get_coworker(coworker_id, tenant_id=tenant_id)
    assert coworker is not None
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    try:
        mount = await materialize_skills_for_spawn(
            coworker, job_id, backend="claude"
        )
        assert mount is not None
        host = Path(mount.host_path)
        assert (host / "enabled").is_dir()
        assert not (host / "disabled").exists()
    finally:
        cleanup_spawn_skills(job_id)


async def test_no_enabled_skills_returns_none() -> None:
    tenant_id, coworker_id = await _make_coworker("emp")
    coworker = await get_coworker(coworker_id, tenant_id=tenant_id)
    assert coworker is not None
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    mount = await materialize_skills_for_spawn(
        coworker, job_id, backend="claude"
    )
    assert mount is None
    # When there are no skills, the spawn dir should not be created.
    assert not (SPAWN_ROOT / job_id).exists()


# ---------------------------------------------------------------------------
# Atomic projection — partial dir is gone after success
# ---------------------------------------------------------------------------


async def test_partial_dir_cleaned_up_after_success() -> None:
    tenant_id, coworker_id = await _make_coworker("atm")
    await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="a",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={"SKILL.md": SkillFile(path="SKILL.md", content="b")},
    )
    coworker = await get_coworker(coworker_id, tenant_id=tenant_id)
    assert coworker is not None
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    try:
        mount = await materialize_skills_for_spawn(
            coworker, job_id, backend="claude"
        )
        assert mount is not None
        partial = Path(mount.host_path) / ".partial"
        assert not partial.exists(), (
            ".partial dir should be removed after successful projection"
        )
    finally:
        cleanup_spawn_skills(job_id)


# ---------------------------------------------------------------------------
# Symlink rejection — projection refuses to write through pre-existing symlink
# ---------------------------------------------------------------------------


async def test_rejects_symlinked_skill_partial_dir(tmp_path: Path) -> None:
    """Inner ``_materialize_one_skill`` must reject a pre-existing
    symlink at ``<build_dir>/.partial/<skill_name>``. Bypassing the
    outer guard requires unusual conditions (e.g. the build dir
    itself was just created, but a malicious actor races to
    symlink the per-skill partial subdir before projection iterates
    that skill). The fix: refuse loudly instead of silently
    rmtree+ignore_errors which is a no-op on symlinks.
    """
    from rolemesh.container.skill_projection import _materialize_one_skill
    from rolemesh.core.skills import SkillValidationError as _SVE

    tenant_id, coworker_id = await _make_coworker("symp")
    skill = await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="trapped",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={"SKILL.md": SkillFile(path="SKILL.md", content="b")},
    )
    fetched_skills = await __import__(
        "rolemesh.db", fromlist=["list_skills_for_coworker"]
    ).list_skills_for_coworker(
        coworker_id, tenant_id=tenant_id, with_files=True,
    )
    fetched = next(s for s in fetched_skills if s.id == skill.id)

    partial_root = tmp_path / "partial"
    final_root = tmp_path / "final"
    partial_root.mkdir()
    final_root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (partial_root / "trapped").symlink_to(elsewhere)

    with pytest.raises(_SVE, match="symlink"):
        _materialize_one_skill(fetched, "claude", partial_root, final_root)
    assert list(elsewhere.iterdir()) == [], (
        f"projection wrote through symlink to {elsewhere}"
    )


async def test_rejects_symlinked_skill_final_dir(tmp_path: Path) -> None:
    """``skill_final`` (the published location) must also reject a
    pre-existing symlink — ``rename`` over a symlinked target has
    undefined POSIX behaviour. Same root cause as above.
    """
    from rolemesh.container.skill_projection import _materialize_one_skill
    from rolemesh.core.skills import SkillValidationError as _SVE

    tenant_id, coworker_id = await _make_coworker("symf")
    skill = await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="finaltrap",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={"SKILL.md": SkillFile(path="SKILL.md", content="b")},
    )
    fetched_skills = await __import__(
        "rolemesh.db", fromlist=["list_skills_for_coworker"]
    ).list_skills_for_coworker(
        coworker_id, tenant_id=tenant_id, with_files=True,
    )
    fetched = next(s for s in fetched_skills if s.id == skill.id)

    partial_root = tmp_path / "partial"
    final_root = tmp_path / "final"
    partial_root.mkdir()
    final_root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (final_root / "finaltrap").symlink_to(elsewhere)

    with pytest.raises(_SVE, match="symlink"):
        _materialize_one_skill(fetched, "claude", partial_root, final_root)


def test_cleanup_orphan_spawns_safe_under_iteration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``iterdir()`` + ``rmtree`` mid-iteration is implementation-
    defined on POSIX (entries can be skipped or revisited).
    Snapshotting via ``list()`` before removal avoids that. Test
    proves all orphans are removed in one pass.
    """
    from rolemesh.container import skill_projection as sp

    fake_root = tmp_path / "spawns"
    fake_root.mkdir()
    monkeypatch.setattr(sp, "SPAWN_ROOT", fake_root)

    # Create 5 orphan spawn dirs.
    orphan_ids = [f"orphan-{i}" for i in range(5)]
    for jid in orphan_ids:
        (fake_root / jid / "skills").mkdir(parents=True)
        (fake_root / jid / "skills" / "marker").write_text("x")

    removed = sp.cleanup_orphan_spawns(set())
    assert removed == 5
    # All orphans gone in a single pass — no skipped entries.
    assert list(fake_root.iterdir()) == []


async def test_rejects_symlinked_build_dir(tmp_path: Path) -> None:
    """If a malicious actor pre-creates a symlinked spawn subdirectory
    (pointing somewhere else on the host), the projector must abort
    loudly rather than silently follow or replace it. Tampering with
    the spawn dir layout is always a red flag.
    """
    tenant_id, coworker_id = await _make_coworker("sym")
    await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="x",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={"SKILL.md": SkillFile(path="SKILL.md", content="b")},
    )
    coworker = await get_coworker(coworker_id, tenant_id=tenant_id)
    assert coworker is not None
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    spawn_dir = SPAWN_ROOT / job_id
    skills_path = spawn_dir / "skills"
    spawn_dir.mkdir(parents=True, exist_ok=True)
    target_dir = tmp_path / "elsewhere"
    target_dir.mkdir()
    skills_path.symlink_to(target_dir)
    try:
        with pytest.raises(SkillValidationError, match="symlink"):
            await materialize_skills_for_spawn(
                coworker, job_id, backend="claude"
            )
        # No bytes ever crossed the symlink to the redirect target.
        assert list(target_dir.iterdir()) == []
    finally:
        # Clean up the symlink + parent dir manually since cleanup
        # logic for a symlinked layout is not exercised in production.
        skills_path.unlink(missing_ok=True)
        if spawn_dir.exists():
            spawn_dir.rmdir()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


async def test_cleanup_spawn_skills_removes_dir() -> None:
    tenant_id, coworker_id = await _make_coworker("cln")
    await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="c",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={"SKILL.md": SkillFile(path="SKILL.md", content="b")},
    )
    coworker = await get_coworker(coworker_id, tenant_id=tenant_id)
    assert coworker is not None
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    await materialize_skills_for_spawn(coworker, job_id, backend="claude")
    assert (SPAWN_ROOT / job_id).exists()
    cleanup_spawn_skills(job_id)
    assert not (SPAWN_ROOT / job_id).exists()


def test_cleanup_spawn_skills_idempotent_on_missing_dir() -> None:
    cleanup_spawn_skills(f"definitely-not-a-real-job-{uuid.uuid4().hex[:6]}")


async def test_cleanup_orphan_spawns_sweeps_abandoned() -> None:
    """Simulate a kill -9 scenario: spawn dir exists but the orchestrator
    has no record of the job_id. The orphan cleaner sweeps it.
    """
    tenant_id, coworker_id = await _make_coworker("orp")
    await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="o",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={"SKILL.md": SkillFile(path="SKILL.md", content="b")},
    )
    coworker = await get_coworker(coworker_id, tenant_id=tenant_id)
    assert coworker is not None
    abandoned = f"orphan-{uuid.uuid4().hex[:8]}"
    active = f"alive-{uuid.uuid4().hex[:8]}"
    try:
        await materialize_skills_for_spawn(
            coworker, abandoned, backend="claude"
        )
        await materialize_skills_for_spawn(
            coworker, active, backend="claude"
        )
        assert (SPAWN_ROOT / abandoned).exists()
        assert (SPAWN_ROOT / active).exists()
        removed = cleanup_orphan_spawns({active})
        assert removed >= 1
        assert not (SPAWN_ROOT / abandoned).exists()
        assert (SPAWN_ROOT / active).exists()
    finally:
        cleanup_spawn_skills(abandoned)
        cleanup_spawn_skills(active)


# ---------------------------------------------------------------------------
# Container target path map sanity
# ---------------------------------------------------------------------------


async def test_outer_finally_cleans_up_on_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``_execute_after_setup`` raises (any line — projection,
    spec build, runtime spawn, approval loader…), the ``execute``
    wrapper's ``finally`` must still call ``cleanup_spawn_skills``.
    Otherwise an exception path leaks the spawn dir until the orphan
    cleaner sweeps it on a much later schedule.

    Test creates a real spawn dir on disk (mimicking what projection
    would have produced) then runs ``execute`` with an inner method
    that raises immediately. Asserts the dir is gone afterwards.
    """
    from rolemesh.agent.container_executor import ContainerAgentExecutor
    from rolemesh.agent.executor import AgentBackendConfig, AgentInput
    from rolemesh.container import skill_projection as sp

    monkeypatch.setattr(sp, "SPAWN_ROOT", tmp_path / "spawns")

    # Capture the auto-generated job_id so we can pre-create its dir.
    captured: dict[str, str] = {}

    class _Boom(Exception):
        pass

    async def _raising_inner(self, inp, on_process, on_output, **kw):
        captured["job_id"] = kw["job_id"]
        # Pre-create the spawn dir to simulate "projection ran, then
        # something later failed". Without the outer finally we'd
        # observe this dir still on disk after execute returns.
        spawn_dir = sp.SPAWN_ROOT / kw["job_id"]
        spawn_dir.mkdir(parents=True, exist_ok=True)
        (spawn_dir / "marker").write_text("x")
        raise _Boom("simulated mid-execute failure")

    monkeypatch.setattr(
        ContainerAgentExecutor,
        "_execute_after_setup",
        _raising_inner,
        raising=False,
    )

    # Build a minimally-functional executor stub. Only the prelude
    # needs to work — coworker lookup + execute wrapper.
    @dataclass_for_test
    class _FakeCoworker:
        id: str = "cw-1"
        tenant_id: str = "t-1"
        name: str = "Bot"
        folder: str = "bot"
        agent_backend: str = "claude"
        agent_role: str = "agent"
        permissions: object = None
        tools: object = ()
        container_config: object = None

    fake_cw = _FakeCoworker()
    cfg = AgentBackendConfig(name="claude", image="x", extra_env={})
    executor = ContainerAgentExecutor.__new__(ContainerAgentExecutor)
    executor._config = cfg
    executor._get_coworker = lambda cid: fake_cw if cid == "cw-1" else None
    executor._transport = None  # not reached — inner raises first
    executor._runtime = None

    inp = AgentInput(
        prompt="hi",
        group_folder="bot",
        chat_jid="x@g.us",
        permissions={"data_scope": "self", "task_schedule": False,
                     "task_manage_others": False, "agent_delegate": False},
        tenant_id="t-1",
        coworker_id="cw-1",
    )

    with pytest.raises(_Boom):
        await executor.execute(inp, on_process=lambda *_: None)

    assert "job_id" in captured, "inner method was never invoked"
    spawn_dir = sp.SPAWN_ROOT / captured["job_id"]
    assert not spawn_dir.exists(), (
        f"outer finally failed to clean up {spawn_dir}; orphan leaked"
    )


def dataclass_for_test(cls):
    """Tiny helper because the real Coworker dataclass has __post_init__
    side-effects we don't want to reproduce here.
    """
    from dataclasses import dataclass
    return dataclass(cls)


def test_container_targets_match_design_doc() -> None:
    """Defends against accidental edits to the path constants — these
    are part of the public spec because the agent SDKs expect to scan
    those exact paths.
    """
    assert CONTAINER_TARGETS["claude"] == "/home/agent/.claude/skills"
    assert CONTAINER_TARGETS["claude"] == "/home/agent/.claude/skills"
    assert CONTAINER_TARGETS["pi"] == "/home/agent/.pi/skills"


def test_skill_projection_uses_shared_constants() -> None:
    """The projector's ``CONTAINER_TARGETS`` must be the SAME object
    (not just equal-valued) as the shared ``CONTAINER_TARGETS_BY_BACKEND``
    from ``rolemesh.ipc.skill_mount``. Without this the in-container
    Pi runtime and the orchestrator-side projector can drift on a
    path edit (a real bug we hit before the constants were shared).
    """
    from rolemesh.ipc.skill_mount import CONTAINER_TARGETS_BY_BACKEND

    assert CONTAINER_TARGETS is CONTAINER_TARGETS_BY_BACKEND, (
        "skill_projection.CONTAINER_TARGETS must be re-exported from "
        "rolemesh.ipc.skill_mount.CONTAINER_TARGETS_BY_BACKEND, not a copy"
    )


def test_pi_backend_uses_shared_skill_mount_constant() -> None:
    """``agent_runner.pi_backend`` must wire ``additional_skill_paths``
    from the shared ``PI_SKILLS_PATH`` constant rather than
    a hardcoded literal. Catches the realistic regression: someone
    replaces ``PI_SKILLS_PATH`` with a literal string while editing,
    which compiles and passes other tests, but breaks silently if
    the orchestrator-side path ever moves again.

    Also asserts the constant value matches the projector's target —
    the contract that this whole shared-module refactor exists to
    enforce.
    """
    import inspect

    from agent_runner import pi_backend
    from rolemesh.ipc.skill_mount import PI_SKILLS_PATH

    src = inspect.getsource(pi_backend)
    assert "from rolemesh.ipc.skill_mount import PI_SKILLS_PATH" in src, (
        "pi_backend must import PI_SKILLS_PATH from the shared module"
    )
    assert "additional_skill_paths=[PI_SKILLS_PATH]" in src, (
        "pi_backend must pass the shared constant to "
        "DefaultResourceLoaderOptions, not a literal path string"
    )
    assert CONTAINER_TARGETS["pi"] == PI_SKILLS_PATH, (
        f"PI_SKILLS_PATH ({PI_SKILLS_PATH!r}) must equal projector's "
        f"CONTAINER_TARGETS['pi'] ({CONTAINER_TARGETS['pi']!r})"
    )


# ---------------------------------------------------------------------------
# Re-projection idempotence — two calls with the same job_id should
# produce the same final state, even though re-using a job_id is
# anomalous in production.
# ---------------------------------------------------------------------------


async def test_re_projection_overwrites_cleanly() -> None:
    tenant_id, coworker_id = await _make_coworker("rep")
    await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="r",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={"SKILL.md": SkillFile(path="SKILL.md", content="v1")},
    )
    coworker = await get_coworker(coworker_id, tenant_id=tenant_id)
    assert coworker is not None
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    try:
        mount1 = await materialize_skills_for_spawn(
            coworker, job_id, backend="claude"
        )
        assert mount1 is not None
        # Tamper with the projected dir, then re-project to the same
        # job_id — projection must wipe and reproject cleanly.
        leftover = Path(mount1.host_path) / "stale.tmp"
        leftover.write_text("garbage")
        mount2 = await materialize_skills_for_spawn(
            coworker, job_id, backend="claude"
        )
        assert mount2 is not None
        assert not leftover.exists(), "re-projection must wipe stale files"
    finally:
        cleanup_spawn_skills(job_id)


# ---------------------------------------------------------------------------
# UID/permission sanity — host_uid running tests should match what
# the agent will see (ownership doesn't matter as long as 0644 file
# perms allow read).
# ---------------------------------------------------------------------------


async def test_files_world_readable() -> None:
    tenant_id, coworker_id = await _make_coworker("uid")
    await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="u",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={
            "SKILL.md": SkillFile(path="SKILL.md", content="x"),
            "deep/nested.txt": SkillFile(path="deep/nested.txt", content="y"),
        },
    )
    coworker = await get_coworker(coworker_id, tenant_id=tenant_id)
    assert coworker is not None
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    try:
        mount = await materialize_skills_for_spawn(
            coworker, job_id, backend="claude"
        )
        assert mount is not None
        skill_root = Path(mount.host_path) / "u"
        for f in (skill_root / "SKILL.md", skill_root / "deep" / "nested.txt"):
            mode = f.stat().st_mode & 0o777
            assert mode & 0o004, f"file {f} is not world-readable (mode {mode:o})"
    finally:
        cleanup_spawn_skills(job_id)


# ---------------------------------------------------------------------------
# Sanity: projection survives a tmp_dir context that's not under DATA_DIR
# (we use SPAWN_ROOT under DATA_DIR, but let's ensure relative paths in
# the projector don't depend on cwd).
# ---------------------------------------------------------------------------


async def test_projection_does_not_depend_on_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    tenant_id, coworker_id = await _make_coworker("cwd")
    await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="c",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={"SKILL.md": SkillFile(path="SKILL.md", content="x")},
    )
    coworker = await get_coworker(coworker_id, tenant_id=tenant_id)
    assert coworker is not None
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    try:
        mount = await materialize_skills_for_spawn(
            coworker, job_id, backend="claude"
        )
        assert mount is not None
        # SPAWN_ROOT is anchored on DATA_DIR (project root), not the cwd.
        assert os.path.isabs(mount.host_path)
        assert (Path(mount.host_path) / "c" / "SKILL.md").exists()
    finally:
        cleanup_spawn_skills(job_id)
