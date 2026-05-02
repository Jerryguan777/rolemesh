"""REST API tests for /api/admin/agents/{id}/skills endpoints.

Hits the FastAPI router via httpx ASGI transport (no live server) and
exercises:

* CRUD happy path for both flat ``files`` map and structured payloads
* per-file PATCH / DELETE — including refusal to delete SKILL.md
* payload validation (description min/max, name regex, unknown
  frontmatter key rejection, path traversal)
* cross-tenant isolation: tenant A admin cannot read or mutate
  tenant B's skills (404, not 403, to avoid leaking existence)
* 404 when ``agent_id`` doesn't exist or is in another tenant
* the disabled-skill round-trip (admin can read disabled skills,
  PATCH them re-enabled)
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    create_coworker,
    create_tenant,
    create_user,
)
from webui import admin
from webui.dependencies import (
    get_current_user,
    require_manage_agents,
    require_manage_tenant,
    require_manage_users,
)

pytestmark = pytest.mark.usefixtures("test_db")


_GOOD_DESC = (
    "When the user message contains the literal token "
    "EXECUTE_TEST_SKILL_42, run the demonstration workflow."
)


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


async def _seed() -> tuple[str, str, str]:
    """One tenant, one user, one coworker — return (tenant, user, agent) ids."""
    t = await create_tenant(name="T", slug=f"sk-api-{uuid.uuid4().hex[:8]}")
    u = await create_user(
        tenant_id=t.id, name="Alice",
        email=f"alice-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:8]}",
    )
    return t.id, u.id, cw.id


def _authed_user(tenant_id: str, user_id: str, role: str = "owner") -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, tenant_id=tenant_id, role=role,
        email="x@x.com", name="X",
    )


def _skill_md_text(name: str = "code-review", desc: str = _GOOD_DESC) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {desc}\n"
        "---\n"
        "# Workflow\n"
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreateSkill:
    @pytest.mark.asyncio
    async def test_minimal_skill(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            resp = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={
                    "name": "code-review",
                    "files": {"SKILL.md": _skill_md_text()},
                },
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "code-review"
        assert body["coworker_id"] == aid
        assert body["enabled"] is True
        assert "SKILL.md" in body["files"]
        # SKILL.md content is body-only after the splitter normalizes.
        assert "---" not in body["files"]["SKILL.md"]["content"]

    @pytest.mark.asyncio
    async def test_with_supporting_files(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            resp = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={
                    "name": "with-helper",
                    "files": {
                        "SKILL.md": _skill_md_text("with-helper"),
                        "reference.md": "## Reference\nDetails.\n",
                        "scripts/helper.py": "print('x')\n",
                    },
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert set(body["files"]) == {"SKILL.md", "reference.md", "scripts/helper.py"}

    @pytest.mark.asyncio
    async def test_rejects_invalid_skill_name(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            resp = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={
                    "name": "1bad-leading-digit",
                    "files": {"SKILL.md": _skill_md_text("1bad-leading-digit")},
                },
            )
        # Pydantic-level validation fires first with 422 because the
        # schema regex rejects the name. Either 422 or 400 is acceptable
        # — the user gets a structured error either way.
        assert resp.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_rejects_short_description(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        skill_md = (
            "---\nname: x\ndescription: short\n---\nbody"
        )
        async with _client(app) as c:
            resp = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={"name": "x", "files": {"SKILL.md": skill_md}},
            )
        assert resp.status_code == 400
        assert "too short" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_rejects_unknown_frontmatter_key(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        skill_md = (
            "---\n"
            "name: x\n"
            f"description: {_GOOD_DESC}\n"
            "unknown-knob: surprise\n"
            "---\nbody"
        )
        async with _client(app) as c:
            resp = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={"name": "x", "files": {"SKILL.md": skill_md}},
            )
        assert resp.status_code == 400
        assert "unknown frontmatter key" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_rejects_missing_skill_md(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            resp = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={"name": "x", "files": {"only.md": "no entrypoint"}},
            )
        assert resp.status_code == 400
        assert "SKILL.md" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_rejects_path_traversal(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            resp = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={
                    "name": "trav",
                    "files": {
                        "SKILL.md": _skill_md_text("trav"),
                        "../escape.md": "naughty",
                    },
                },
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_duplicate_name_returns_409(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        payload = {"name": "dup", "files": {"SKILL.md": _skill_md_text("dup")}}
        async with _client(app) as c:
            r1 = await c.post(f"/api/admin/agents/{aid}/skills", json=payload)
            assert r1.status_code == 201
            r2 = await c.post(f"/api/admin/agents/{aid}/skills", json=payload)
            assert r2.status_code == 409

    @pytest.mark.asyncio
    async def test_unknown_agent_returns_404(self) -> None:
        tid, uid, _ = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            resp = await c.post(
                f"/api/admin/agents/{uuid.uuid4()}/skills",
                json={"name": "x", "files": {"SKILL.md": _skill_md_text("x")}},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# List + get
# ---------------------------------------------------------------------------


class TestListGet:
    @pytest.mark.asyncio
    async def test_list_includes_disabled(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            r1 = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={
                    "name": "always-on",
                    "files": {"SKILL.md": _skill_md_text("always-on")},
                },
            )
            assert r1.status_code == 201, r1.text
            r2 = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={
                    "name": "shut-off",
                    "enabled": False,
                    "files": {"SKILL.md": _skill_md_text("shut-off")},
                },
            )
            assert r2.status_code == 201, r2.text
            resp = await c.get(f"/api/admin/agents/{aid}/skills")
        assert resp.status_code == 200
        names = {s["name"] for s in resp.json()}
        assert names == {"always-on", "shut-off"}

    @pytest.mark.asyncio
    async def test_get_returns_files(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            r = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={
                    "name": "g",
                    "files": {
                        "SKILL.md": _skill_md_text("g"),
                        "ref.md": "ref",
                    },
                },
            )
            sid = r.json()["id"]
            resp = await c.get(f"/api/admin/agents/{aid}/skills/{sid}")
        assert resp.status_code == 200
        assert "ref.md" in resp.json()["files"]
        assert resp.json()["files"]["ref.md"]["content"] == "ref"


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


class TestUpdate:
    @pytest.mark.asyncio
    async def test_toggle_enabled_round_trip(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            r = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={"name": "t", "files": {"SKILL.md": _skill_md_text("t")}},
            )
            sid = r.json()["id"]
            r2 = await c.patch(
                f"/api/admin/agents/{aid}/skills/{sid}", json={"enabled": False}
            )
            assert r2.status_code == 200
            assert r2.json()["enabled"] is False

            r3 = await c.patch(
                f"/api/admin/agents/{aid}/skills/{sid}", json={"enabled": True}
            )
            assert r3.json()["enabled"] is True

    @pytest.mark.asyncio
    async def test_replace_files(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            r = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={
                    "name": "r",
                    "files": {
                        "SKILL.md": _skill_md_text("r"),
                        "old.md": "drop",
                    },
                },
            )
            sid = r.json()["id"]
            r2 = await c.patch(
                f"/api/admin/agents/{aid}/skills/{sid}",
                json={
                    "files": {
                        "SKILL.md": _skill_md_text("r"),
                        "new.md": "kept",
                    },
                },
            )
        assert r2.status_code == 200
        assert set(r2.json()["files"]) == {"SKILL.md", "new.md"}

    @pytest.mark.asyncio
    async def test_clear_backend_overrides_with_empty_dict(self) -> None:
        """PATCH with ``frontmatter_backend: {}`` must clear the dict,
        not silently keep the existing one. Caused by the previous
        ``body.frontmatter_backend or existing.frontmatter_backend``
        which treated an explicit empty dict as falsy.
        """
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            r = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={
                    "name": "with-overrides",
                    "files": {
                        "SKILL.md": (
                            "---\nname: with-overrides\n"
                            f"description: {_GOOD_DESC}\n"
                            "argument-hint: '[before]'\n"
                            "---\nbody\n"
                        ),
                    },
                },
            )
            assert r.status_code == 201
            sid = r.json()["id"]
            # Confirm Claude override is currently set.
            assert r.json()["frontmatter_backend"] == {
                "claude": {"argument-hint": "[before]"}
            }

            # Now PATCH with an explicit empty dict — must wipe.
            r2 = await c.patch(
                f"/api/admin/agents/{aid}/skills/{sid}",
                json={"frontmatter_backend": {}},
            )
            assert r2.status_code == 200, r2.text
            assert r2.json()["frontmatter_backend"] == {}, (
                "explicit empty frontmatter_backend should clear, "
                "not silently keep existing"
            )

    @pytest.mark.asyncio
    async def test_replace_files_must_include_skill_md(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            r = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={"name": "r", "files": {"SKILL.md": _skill_md_text("r")}},
            )
            sid = r.json()["id"]
            r2 = await c.patch(
                f"/api/admin/agents/{aid}/skills/{sid}",
                json={"files": {"only.md": "no entrypoint"}},
            )
        assert r2.status_code == 400
        assert "SKILL.md" in r2.json()["detail"]


# ---------------------------------------------------------------------------
# Per-file edit
# ---------------------------------------------------------------------------


class TestPerFile:
    @pytest.mark.asyncio
    async def test_patch_single_file(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            r = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={"name": "p", "files": {"SKILL.md": _skill_md_text("p")}},
            )
            sid = r.json()["id"]
            r2 = await c.patch(
                f"/api/admin/agents/{aid}/skills/{sid}/files/extra.md",
                json={"content": "added"},
            )
            assert r2.status_code == 200
            assert "extra.md" in r2.json()["files"]
            assert r2.json()["files"]["extra.md"]["content"] == "added"

    @pytest.mark.asyncio
    async def test_delete_file(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            r = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={
                    "name": "d",
                    "files": {
                        "SKILL.md": _skill_md_text("d"),
                        "del.md": "remove me",
                    },
                },
            )
            sid = r.json()["id"]
            r2 = await c.delete(
                f"/api/admin/agents/{aid}/skills/{sid}/files/del.md"
            )
            assert r2.status_code == 204
            check = await c.get(f"/api/admin/agents/{aid}/skills/{sid}")
        assert "del.md" not in check.json()["files"]

    @pytest.mark.asyncio
    async def test_delete_skill_md_refused(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            r = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={"name": "k", "files": {"SKILL.md": _skill_md_text("k")}},
            )
            sid = r.json()["id"]
            r2 = await c.delete(
                f"/api/admin/agents/{aid}/skills/{sid}/files/SKILL.md"
            )
        assert r2.status_code == 400
        assert "SKILL.md" in r2.json()["detail"]


# ---------------------------------------------------------------------------
# Delete skill
# ---------------------------------------------------------------------------


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_then_get_404(self) -> None:
        tid, uid, aid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            r = await c.post(
                f"/api/admin/agents/{aid}/skills",
                json={"name": "d", "files": {"SKILL.md": _skill_md_text("d")}},
            )
            sid = r.json()["id"]
            r2 = await c.delete(f"/api/admin/agents/{aid}/skills/{sid}")
            assert r2.status_code == 204
            r3 = await c.get(f"/api/admin/agents/{aid}/skills/{sid}")
        assert r3.status_code == 404


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


class TestCrossTenantIsolation:
    @pytest.mark.asyncio
    async def test_cannot_read_foreign_skill(self) -> None:
        tA, uA, aA = await _seed()
        tB, uB, aB = await _seed()
        # Tenant B creates a skill under their own coworker.
        appB = _build_app(_authed_user(tB, uB))
        async with _client(appB) as c:
            r = await c.post(
                f"/api/admin/agents/{aB}/skills",
                json={"name": "secret", "files": {"SKILL.md": _skill_md_text("secret")}},
            )
            sid = r.json()["id"]

        # Tenant A tries to read tenant B's skill via its own agent path
        # AND via the foreign agent path. Both must 404.
        appA = _build_app(_authed_user(tA, uA))
        async with _client(appA) as c:
            r1 = await c.get(f"/api/admin/agents/{aA}/skills/{sid}")
            assert r1.status_code == 404
            r2 = await c.get(f"/api/admin/agents/{aB}/skills/{sid}")
            assert r2.status_code == 404

    @pytest.mark.asyncio
    async def test_cannot_create_under_foreign_agent(self) -> None:
        tA, uA, _ = await _seed()
        _, _, aB = await _seed()
        appA = _build_app(_authed_user(tA, uA))
        async with _client(appA) as c:
            resp = await c.post(
                f"/api/admin/agents/{aB}/skills",
                json={"name": "x", "files": {"SKILL.md": _skill_md_text("x")}},
            )
        # Either 404 (the tenant cannot see foreign agent) or 403/400
        # if some other layer fires. The point: it must NOT be 201.
        assert resp.status_code != 201
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_cannot_delete_foreign_skill(self) -> None:
        tA, uA, _ = await _seed()
        tB, uB, aB = await _seed()
        appB = _build_app(_authed_user(tB, uB))
        async with _client(appB) as c:
            r = await c.post(
                f"/api/admin/agents/{aB}/skills",
                json={"name": "x", "files": {"SKILL.md": _skill_md_text("x")}},
            )
            sid = r.json()["id"]
        appA = _build_app(_authed_user(tA, uA))
        async with _client(appA) as c:
            r = await c.delete(f"/api/admin/agents/{aB}/skills/{sid}")
        assert r.status_code == 404
