"""End-to-end skills integration: REST → DB → container projection.

Verifies that the three PR layers compose correctly:

* PR 3's REST surface accepts a SKILL.md with inline frontmatter,
  splits it into structured ``frontmatter_common`` /
  ``frontmatter_backend`` columns, and persists files under the
  parent skill row.
* PR 1's RLS + cross-tenant trigger enforces tenant scoping at
  every layer of that pipeline.
* PR 2's projector reads the persisted skill, merges frontmatter
  for the active backend (dropping the other backend's keys), and
  materializes a self-contained ``<spawn>/<name>/`` tree on host
  disk that mirrors what the agent SDK / Pi loader will scan.

Two backend passes (Claude and Pi) prove the design's "write once,
project to either" claim. Real model invocation is gated behind
``@pytest.mark.e2e`` and skipped by default — those tests document
the manual flow for verifying the description-based auto-invocation
loop.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
import yaml
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.container.skill_projection import (
    SPAWN_ROOT,
    cleanup_spawn_skills,
    materialize_skills_for_spawn,
)
from rolemesh.db.pg import (
    create_coworker,
    create_tenant,
    create_user,
    get_coworker,
)
from webui import admin
from webui.dependencies import (
    get_current_user,
    require_manage_agents,
    require_manage_tenant,
    require_manage_users,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


pytestmark = pytest.mark.usefixtures("test_db")


_TRIGGER_TOKEN = "EXECUTE_TEST_SKILL_42"
_DESCRIPTION = (
    f"When the user message contains the literal token "
    f"{_TRIGGER_TOKEN}, run the demonstration workflow."
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_app(user: AuthenticatedUser) -> FastAPI:
    app = FastAPI()
    app.include_router(admin.router)

    async def _return_user() -> AuthenticatedUser:
        return user

    app.dependency_overrides[get_current_user] = _return_user
    app.dependency_overrides[require_manage_agents] = _return_user
    app.dependency_overrides[require_manage_tenant] = _return_user
    app.dependency_overrides[require_manage_users] = _return_user
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _seed(agent_backend: str = "claude-code") -> tuple[AuthenticatedUser, str]:
    """One tenant + one admin user + one coworker on ``agent_backend``.

    Returns ``(user_for_app, coworker_id)``.
    """
    tag = uuid.uuid4().hex[:8]
    t = await create_tenant(name="T", slug=f"int-{tag}")
    u = await create_user(
        tenant_id=t.id, name="Alice",
        email=f"alice-{tag}@x.com", role="owner",
    )
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{tag}",
        agent_backend=agent_backend,
    )
    user = AuthenticatedUser(
        user_id=u.id, tenant_id=t.id, role="owner",
        email="x@x.com", name="X",
    )
    return user, cw.id


def _skill_payload(name: str = "echo") -> dict[str, object]:
    """Inline-frontmatter SKILL.md plus a supporting reference file.

    This mirrors what an admin would type in the WebUI: name +
    description in inline YAML, body below the closing ``---``,
    plus an examples file the model can reference at call time.
    """
    skill_md = (
        "---\n"
        f"name: {name}\n"
        f"description: {_DESCRIPTION}\n"
        "argument-hint: '[trigger token]'\n"
        "---\n"
        "# Workflow\n"
        "1. Acknowledge the trigger token.\n"
        "2. Read examples.md and emit the canonical reply.\n"
    )
    return {
        "name": name,
        "files": {
            "SKILL.md": skill_md,
            "examples.md": (
                "## Canonical reply\n"
                "When invoked, respond with: 'EXECUTE_TEST_SKILL_42_OK'.\n"
            ),
        },
    }


# ---------------------------------------------------------------------------
# Layered integration: REST → DB → projection (Claude backend)
# ---------------------------------------------------------------------------


async def test_rest_to_projection_claude_backend() -> None:
    user, agent_id = await _seed(agent_backend="claude-code")
    app = _build_app(user)
    async with _client(app) as c:
        resp = await c.post(
            f"/api/admin/agents/{agent_id}/skills",
            json=_skill_payload("echo"),
        )
    assert resp.status_code == 201, resp.text

    coworker = await get_coworker(agent_id, tenant_id=user.tenant_id)
    assert coworker is not None
    job_id = f"int-{uuid.uuid4().hex[:8]}"
    try:
        mount = await materialize_skills_for_spawn(
            coworker, job_id, backend="claude-code"
        )
        assert mount is not None
        assert mount.container_path == "/home/agent/.claude/skills"
        assert mount.readonly is True

        skill_root = Path(mount.host_path) / "echo"
        skill_md_text = (skill_root / "SKILL.md").read_text()
        assert skill_md_text.startswith("---\n")
        _, fm_block, body = skill_md_text.split("---\n", 2)
        merged = yaml.safe_load(fm_block)
        # Common fields preserved
        assert merged["name"] == "echo"
        assert merged["description"] == _DESCRIPTION
        # Claude-specific field merged in (and present)
        assert merged["argument-hint"] == "[trigger token]"
        # No stray keys from the Pi side (this skill never set any,
        # but the contract is "drop other backend's keys")
        assert "disable_model_invocation" not in merged
        # Body comes from the inline-frontmatter block, not the
        # JSONB columns
        assert "Acknowledge the trigger token" in body

        # Supporting file projected verbatim
        examples_text = (skill_root / "examples.md").read_text()
        assert _TRIGGER_TOKEN + "_OK" in examples_text

        # File mode 0644 — agent UID can read regardless of owner
        for f in (skill_root / "SKILL.md", skill_root / "examples.md"):
            mode = f.stat().st_mode & 0o777
            assert mode & 0o004, f"file {f} not world-readable (mode {mode:o})"
    finally:
        cleanup_spawn_skills(job_id)


# ---------------------------------------------------------------------------
# Layered integration: REST → DB → projection (Pi backend)
# ---------------------------------------------------------------------------


async def test_rest_to_projection_pi_backend() -> None:
    user, agent_id = await _seed(agent_backend="pi")
    app = _build_app(user)
    # The same skill with both Claude- and Pi-side frontmatter knobs
    # set so we can prove projection drops the Claude-side keys.
    skill_md = (
        "---\n"
        "name: echo\n"
        f"description: {_DESCRIPTION}\n"
        "argument-hint: '[ignored on Pi]'\n"
        "disable_model_invocation: false\n"
        "---\n"
        "# Workflow body for Pi.\n"
    )
    async with _client(app) as c:
        resp = await c.post(
            f"/api/admin/agents/{agent_id}/skills",
            json={
                "name": "echo",
                "files": {
                    "SKILL.md": skill_md,
                    "examples.md": "## Canonical reply\nrespond with OK.\n",
                },
            },
        )
    assert resp.status_code == 201, resp.text

    coworker = await get_coworker(agent_id, tenant_id=user.tenant_id)
    assert coworker is not None
    job_id = f"int-{uuid.uuid4().hex[:8]}"
    try:
        mount = await materialize_skills_for_spawn(
            coworker, job_id, backend="pi"
        )
        assert mount is not None
        assert mount.container_path == "/home/agent/.pi/agent/skills"

        skill_md_out = (Path(mount.host_path) / "echo" / "SKILL.md").read_text()
        _, fm_block, _ = skill_md_out.split("---\n", 2)
        merged = yaml.safe_load(fm_block)
        # Pi-specific knob makes it through
        assert merged["disable_model_invocation"] is False
        # Claude-specific knob is dropped — the whole point of split
        # frontmatter storage
        assert "argument-hint" not in merged, (
            "Claude-only field leaked into Pi projection"
        )
    finally:
        cleanup_spawn_skills(job_id)


# ---------------------------------------------------------------------------
# Disabled skills round-trip via REST + projection
# ---------------------------------------------------------------------------


async def test_disabled_skill_visible_in_rest_invisible_to_projection() -> None:
    """A disabled skill must remain readable through REST so admins
    can re-enable it, but must NOT appear in the projected directory.
    The model literally cannot see disabled skills — this is a
    stronger guarantee than relying on a "do not use" description.
    """
    user, agent_id = await _seed(agent_backend="claude-code")
    app = _build_app(user)
    async with _client(app) as c:
        r = await c.post(
            f"/api/admin/agents/{agent_id}/skills",
            json={
                "name": "off-by-default",
                "enabled": False,
                "files": {
                    "SKILL.md": (
                        "---\nname: off-by-default\n"
                        f"description: {_DESCRIPTION}\n---\nbody\n"
                    ),
                },
            },
        )
        assert r.status_code == 201

        list_resp = await c.get(f"/api/admin/agents/{agent_id}/skills")
        assert list_resp.status_code == 200
        names = {s["name"] for s in list_resp.json()}
        assert "off-by-default" in names

    coworker = await get_coworker(agent_id, tenant_id=user.tenant_id)
    assert coworker is not None
    job_id = f"int-{uuid.uuid4().hex[:8]}"
    try:
        mount = await materialize_skills_for_spawn(
            coworker, job_id, backend="claude-code"
        )
        # Only the disabled skill exists, so projection returns None.
        assert mount is None
        # And no spawn dir was created.
        assert not (SPAWN_ROOT / job_id).exists()
    finally:
        cleanup_spawn_skills(job_id)


# ---------------------------------------------------------------------------
# Tenant boundary: REST + projection both refuse cross-tenant access
# ---------------------------------------------------------------------------


async def test_cross_tenant_skill_invisible_to_other_tenant_projection() -> None:
    """Tenant B's skill must never be projected for tenant A's
    coworker, even when A's projection happens to use the same
    skill name in its own tenant.
    """
    userA, agentA = await _seed(agent_backend="claude-code")
    userB, agentB = await _seed(agent_backend="claude-code")

    # Tenant A creates "shared-name" in their own scope.
    appA = _build_app(userA)
    async with _client(appA) as c:
        r = await c.post(
            f"/api/admin/agents/{agentA}/skills",
            json={
                "name": "shared-name",
                "files": {
                    "SKILL.md": (
                        "---\nname: shared-name\n"
                        f"description: {_DESCRIPTION}\n---\n"
                        "Tenant A body\n"
                    ),
                },
            },
        )
        assert r.status_code == 201

    # Tenant B does the same — different content, same name, in B's tenant.
    appB = _build_app(userB)
    async with _client(appB) as c:
        r = await c.post(
            f"/api/admin/agents/{agentB}/skills",
            json={
                "name": "shared-name",
                "files": {
                    "SKILL.md": (
                        "---\nname: shared-name\n"
                        f"description: {_DESCRIPTION}\n---\n"
                        "Tenant B body\n"
                    ),
                },
            },
        )
        assert r.status_code == 201

    # Project for tenant A — should only see A's content.
    cwA = await get_coworker(agentA, tenant_id=userA.tenant_id)
    assert cwA is not None
    job_id = f"int-{uuid.uuid4().hex[:8]}"
    try:
        mount = await materialize_skills_for_spawn(
            cwA, job_id, backend="claude-code"
        )
        assert mount is not None
        skill_md = (Path(mount.host_path) / "shared-name" / "SKILL.md").read_text()
        assert "Tenant A body" in skill_md
        assert "Tenant B body" not in skill_md
    finally:
        cleanup_spawn_skills(job_id)


# ---------------------------------------------------------------------------
# Full round-trip: edit a single file via REST and re-project
# ---------------------------------------------------------------------------


async def test_per_file_patch_visible_in_next_projection() -> None:
    user, agent_id = await _seed(agent_backend="claude-code")
    app = _build_app(user)
    async with _client(app) as c:
        r = await c.post(
            f"/api/admin/agents/{agent_id}/skills",
            json={
                "name": "iterative",
                "files": {
                    "SKILL.md": (
                        "---\nname: iterative\n"
                        f"description: {_DESCRIPTION}\n---\nv1 body\n"
                    ),
                },
            },
        )
        sid = r.json()["id"]
        # Add a supporting file via the per-file PATCH endpoint.
        r2 = await c.patch(
            f"/api/admin/agents/{agent_id}/skills/{sid}/files/notes.md",
            json={"content": "## v2 notes\nadded later\n"},
        )
        assert r2.status_code == 200

    coworker = await get_coworker(agent_id, tenant_id=user.tenant_id)
    assert coworker is not None
    job_id = f"int-{uuid.uuid4().hex[:8]}"
    try:
        mount = await materialize_skills_for_spawn(
            coworker, job_id, backend="claude-code"
        )
        assert mount is not None
        skill_root = Path(mount.host_path) / "iterative"
        # The PATCH content must be present in the next projection.
        notes = (skill_root / "notes.md").read_text()
        assert "added later" in notes
    finally:
        cleanup_spawn_skills(job_id)


# ---------------------------------------------------------------------------
# Real-model E2E (skipped by default)
#
# These tests require:
#   * A reachable Anthropic API or self-hosted Claude proxy
#   * Docker + the rolemesh-agent image built
#   * NATS available to the orchestrator
#
# They are documented here as the canonical proof of the
# auto-invocation contract: the model decides to call a skill
# purely from its frontmatter ``description``, then reads
# supporting files referenced from SKILL.md. To run:
#
#   pytest -m e2e tests/test_skills_integration.py
#
# Run on a dedicated CI lane that has the dependencies wired up,
# not on per-PR CI.
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.skipif(
    os.environ.get("ROLEMESH_E2E_SKILLS") != "1",
    reason="Real-model skill e2e — set ROLEMESH_E2E_SKILLS=1 to enable",
)
async def test_e2e_model_invokes_skill_claude() -> None:
    """The model receives a prompt containing the trigger token,
    autonomously calls the skill (Claude SDK loads the projected
    SKILL.md and the model decides), reads ``examples.md``, and
    emits the canonical reply.

    Implementation lives outside this module — this stub is the
    place to plumb in the live runner. Marked skipped to avoid
    spurious failures on CI without the right wiring.
    """
    pytest.skip("Live-model e2e wiring intentionally not bundled in PR 4.")


@pytest.mark.e2e
@pytest.mark.skipif(
    os.environ.get("ROLEMESH_E2E_SKILLS") != "1",
    reason="Real-model skill e2e — set ROLEMESH_E2E_SKILLS=1 to enable",
)
async def test_e2e_model_invokes_skill_pi() -> None:
    """Pi backend variant of the live-model test."""
    pytest.skip("Live-model e2e wiring intentionally not bundled in PR 4.")
