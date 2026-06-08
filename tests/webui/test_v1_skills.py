"""Integration tests for ``/api/v1/skills`` + the coworker relation
layer.

Hits the FastAPI app via httpx ASGI transport against a real
Postgres testcontainer. Covers:

* CRUD happy path on the per-tenant catalog
* RLS isolation (tenant A cannot see tenant B's skills)
* SKILL.md protection (DELETE → 409 ``SKILL_MANIFEST_PROTECTED``)
* RESOURCE_IN_USE 409 on catalog DELETE when bindings exist
* Path traversal rejection (422 ``INVALID_PATH``)
* ``web.coworker.skills_changed`` event publish fan-out
* Binding endpoints (POST/DELETE) idempotency + double-AND projection

Anti-mirror discipline: each test exercises the wire endpoint, not
the underlying DB helper — the suite would still catch a regression
where the helper silently returns wrong data through the JSON.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    create_coworker,
    create_tenant,
    create_user,
    enable_skill_for_coworker,
)
from webui.api_v1 import router as api_v1_router
from webui.dependencies import get_current_user
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")


_GOOD_DESC = (
    "When the user message contains the literal token "
    "EXECUTE_TEST_SKILL_42, run the demonstration workflow."
)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _build_app(user: AuthenticatedUser) -> FastAPI:
    app = FastAPI()
    install_error_handler(app)
    app.include_router(api_v1_router)

    async def _return_user() -> AuthenticatedUser:
        return user

    app.dependency_overrides[get_current_user] = _return_user
    return app


async def _make_user_and_coworker(
    slug: str = "sk",
) -> tuple[AuthenticatedUser, str]:
    t = await create_tenant(
        name=f"T-{slug}", slug=f"{slug}-{uuid.uuid4().hex[:8]}",
    )
    u = await create_user(
        tenant_id=t.id, name="Alice",
        email=f"a-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    cw = await create_coworker(
        tenant_id=t.id, name="CW",
        folder=f"cw-{slug}-{uuid.uuid4().hex[:6]}",
    )
    return (
        AuthenticatedUser(
            user_id=u.id, tenant_id=t.id, role="owner",
            email="x@x.com", name="X",
        ),
        cw.id,
    )


def _skill_md(name: str = "code-review", desc: str = _GOOD_DESC) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {desc}\n"
        "---\n"
        "# Workflow\n"
    )


_HDRS = {"Authorization": "Bearer x"}


# ---------------------------------------------------------------------------
# Flat /api/v1/skills CRUD
# ---------------------------------------------------------------------------


async def test_create_then_get_skill_round_trip() -> None:
    user, _cw = await _make_user_and_coworker("crt")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/skills",
            json={"name": "code-review", "files": {"SKILL.md": _skill_md()}},
            headers=_HDRS,
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "code-review"
    assert body["tenant_id"] == user.tenant_id
    assert body["enabled"] is True
    # SKILL.md is in the response and frontmatter has been stripped.
    assert "SKILL.md" in body["files"]
    assert "---" not in body["files"]["SKILL.md"]["content"]


async def test_create_rejects_missing_skill_md() -> None:
    user, _cw = await _make_user_and_coworker("nomd")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/skills",
            json={"name": "x", "files": {"only.md": "no entrypoint"}},
            headers=_HDRS,
        )
    assert resp.status_code == 422
    assert resp.json()["code"] == "SKILL_MANIFEST_REQUIRED"


async def test_create_rejects_path_traversal_with_422() -> None:
    user, _cw = await _make_user_and_coworker("trav")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/skills",
            json={
                "name": "trav",
                "files": {
                    "SKILL.md": _skill_md("trav"),
                    "../escape.md": "naughty",
                },
            },
            headers=_HDRS,
        )
    assert resp.status_code == 422
    assert resp.json()["code"] == "INVALID_PATH"


async def test_duplicate_skill_name_returns_409() -> None:
    user, _cw = await _make_user_and_coworker("dup")
    async with _client(_build_app(user)) as ac:
        payload = {"name": "dup", "files": {"SKILL.md": _skill_md("dup")}}
        r1 = await ac.post("/api/v1/skills", json=payload, headers=_HDRS)
        assert r1.status_code == 201
        r2 = await ac.post("/api/v1/skills", json=payload, headers=_HDRS)
    assert r2.status_code == 409
    assert r2.json()["code"] == "RESOURCE_IN_USE"


async def test_list_skills_returns_summary_with_zero_bound_count() -> None:
    """``bound_coworker_count`` is the relation-layer projection;
    a freshly-created catalog skill has zero bindings.
    """
    user, _cw = await _make_user_and_coworker("lst")
    async with _client(_build_app(user)) as ac:
        await ac.post(
            "/api/v1/skills",
            json={"name": "a", "files": {"SKILL.md": _skill_md("a")}},
            headers=_HDRS,
        )
        resp = await ac.get("/api/v1/skills", headers=_HDRS)
    assert resp.status_code == 200
    rows = resp.json()["items"]
    assert any(s["name"] == "a" and s["bound_coworker_count"] == 0 for s in rows)


async def test_list_skills_summary_carries_created_by_user_id() -> None:
    """The list projection must expose ``created_by_user_id`` (the
    creator) so the role-aware skills page can drive ownership-escape
    affordances — own-row Edit/Delete and the "Mine" filter — without an
    N+1 fetch of each full skill.

    Regression: ``SkillSummary`` previously dropped this field while the
    full ``Skill`` carried it, so the frontend list view classified every
    row as unowned and members lost the Edit/Delete shortcut on their own
    skills.
    """
    user, _cw = await _make_user_and_coworker("owns")
    async with _client(_build_app(user)) as ac:
        await ac.post(
            "/api/v1/skills",
            json={"name": "mine", "files": {"SKILL.md": _skill_md("mine")}},
            headers=_HDRS,
        )
        resp = await ac.get("/api/v1/skills", headers=_HDRS)
    assert resp.status_code == 200
    row = next(s for s in resp.json()["items"] if s["name"] == "mine")
    assert str(row["created_by_user_id"]) == str(user.user_id)


async def test_patch_skill_enables_and_publishes_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Toggle-disable then toggle-enable a skill — the second PATCH
    must not be locked out by an overly-strict auth check, and each
    PATCH that affects a coworker fans out one event.
    """
    user, cw_id = await _make_user_and_coworker("ptc")
    mock_pub = AsyncMock()
    monkeypatch.setattr(
        "webui.v1.skills.coworker_events.publish_coworker_skills_changed",
        mock_pub,
    )
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/skills",
            json={"name": "tog", "files": {"SKILL.md": _skill_md("tog")}},
            headers=_HDRS,
        )
        sid = created.json()["id"]
        # Bind to the coworker so a skills_changed event has a target.
        bind = await ac.post(
            f"/api/v1/coworkers/{cw_id}/skills/{sid}", headers=_HDRS,
        )
        assert bind.status_code == 201
        # One publish from the binding (the POST handler), zero from
        # creation (no coworkers bound at create time).
        first_count = mock_pub.await_count
        r2 = await ac.patch(
            f"/api/v1/skills/{sid}", json={"enabled": False}, headers=_HDRS,
        )
        assert r2.status_code == 200
        assert r2.json()["enabled"] is False
        r3 = await ac.patch(
            f"/api/v1/skills/{sid}", json={"enabled": True}, headers=_HDRS,
        )
        assert r3.status_code == 200
        assert r3.json()["enabled"] is True
    # Each catalog PATCH broadcasts once per bound coworker; we have 1.
    assert mock_pub.await_count == first_count + 2


async def test_delete_skill_in_use_returns_409() -> None:
    user, cw_id = await _make_user_and_coworker("inu")
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/skills",
            json={"name": "iu", "files": {"SKILL.md": _skill_md("iu")}},
            headers=_HDRS,
        )
        sid = created.json()["id"]
        await ac.post(
            f"/api/v1/coworkers/{cw_id}/skills/{sid}", headers=_HDRS,
        )
        resp = await ac.delete(f"/api/v1/skills/{sid}", headers=_HDRS)
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "RESOURCE_IN_USE"
    assert cw_id in body["details"]["coworker_ids"]


async def test_delete_skill_after_unbind_succeeds() -> None:
    user, cw_id = await _make_user_and_coworker("unb")
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/skills",
            json={"name": "u", "files": {"SKILL.md": _skill_md("u")}},
            headers=_HDRS,
        )
        sid = created.json()["id"]
        await ac.post(
            f"/api/v1/coworkers/{cw_id}/skills/{sid}", headers=_HDRS,
        )
        await ac.delete(
            f"/api/v1/coworkers/{cw_id}/skills/{sid}", headers=_HDRS,
        )
        resp = await ac.delete(f"/api/v1/skills/{sid}", headers=_HDRS)
    assert resp.status_code == 204
    # GET now 404s.
    async with _client(_build_app(user)) as ac:
        gone = await ac.get(f"/api/v1/skills/{sid}", headers=_HDRS)
    assert gone.status_code == 404


# ---------------------------------------------------------------------------
# File endpoints
# ---------------------------------------------------------------------------


async def test_put_file_then_get_round_trip() -> None:
    user, _cw = await _make_user_and_coworker("file")
    async with _client(_build_app(user)) as ac:
        sid = (await ac.post(
            "/api/v1/skills",
            json={"name": "f", "files": {"SKILL.md": _skill_md("f")}},
            headers=_HDRS,
        )).json()["id"]
        put = await ac.put(
            f"/api/v1/skills/{sid}/files/notes.md",
            json={"content": "hello"},
            headers=_HDRS,
        )
        assert put.status_code == 200
        assert put.json()["content"] == "hello"
        got = await ac.get(
            f"/api/v1/skills/{sid}/files/notes.md", headers=_HDRS,
        )
    assert got.status_code == 200
    assert got.json()["path"] == "notes.md"


async def test_delete_skill_md_returns_409_skill_manifest_protected() -> None:
    user, _cw = await _make_user_and_coworker("prot")
    async with _client(_build_app(user)) as ac:
        sid = (await ac.post(
            "/api/v1/skills",
            json={"name": "p", "files": {"SKILL.md": _skill_md("p")}},
            headers=_HDRS,
        )).json()["id"]
        resp = await ac.delete(
            f"/api/v1/skills/{sid}/files/SKILL.md", headers=_HDRS,
        )
    assert resp.status_code == 409
    assert resp.json()["code"] == "SKILL_MANIFEST_PROTECTED"


async def test_put_file_rejects_path_traversal_with_422() -> None:
    """Path validation at the wire layer — the URL ``{path:path}``
    matches greedy so ``../escape`` reaches the handler intact.
    The validator must reject it before any DB write.
    """
    user, _cw = await _make_user_and_coworker("pt")
    async with _client(_build_app(user)) as ac:
        sid = (await ac.post(
            "/api/v1/skills",
            json={"name": "pt", "files": {"SKILL.md": _skill_md("pt")}},
            headers=_HDRS,
        )).json()["id"]
        resp = await ac.put(
            f"/api/v1/skills/{sid}/files/..escape.md",
            json={"content": "x"},
            headers=_HDRS,
        )
    assert resp.status_code == 422
    assert resp.json()["code"] == "INVALID_PATH"


# ---------------------------------------------------------------------------
# Coworker relation endpoints
# ---------------------------------------------------------------------------


async def test_enable_then_disable_coworker_skill_idempotent() -> None:
    user, cw_id = await _make_user_and_coworker("rel")
    async with _client(_build_app(user)) as ac:
        sid = (await ac.post(
            "/api/v1/skills",
            json={"name": "r", "files": {"SKILL.md": _skill_md("r")}},
            headers=_HDRS,
        )).json()["id"]
        # First enable is the binding insert.
        r1 = await ac.post(
            f"/api/v1/coworkers/{cw_id}/skills/{sid}", headers=_HDRS,
        )
        assert r1.status_code == 201
        # Second enable is idempotent (the ON CONFLICT upsert keeps it
        # enabled). Still a 201 — repeated enables are not a 409.
        r2 = await ac.post(
            f"/api/v1/coworkers/{cw_id}/skills/{sid}", headers=_HDRS,
        )
        assert r2.status_code == 201
        # Disable removes the binding row.
        r3 = await ac.delete(
            f"/api/v1/coworkers/{cw_id}/skills/{sid}", headers=_HDRS,
        )
        assert r3.status_code == 204
        # Second disable returns 404 — the binding is gone, the
        # endpoint is not idempotent-on-absent.
        r4 = await ac.delete(
            f"/api/v1/coworkers/{cw_id}/skills/{sid}", headers=_HDRS,
        )
    assert r4.status_code == 404


async def test_list_coworker_skills_returns_bindings_with_state() -> None:
    """The list endpoint surfaces the junction's ``enabled`` flag,
    not the catalog's ``enabled`` flag. Double-AND projection in the
    orchestrator is a downstream concern.
    """
    user, cw_id = await _make_user_and_coworker("lb")
    # Bind one skill globally-enabled and one globally-disabled.
    # Skill names dodge the YAML 1.1 boolean keywords (on/off/yes/no
    # /true/false) — those get parsed as booleans in unquoted
    # frontmatter and rejected before the validator even runs.
    async with _client(_build_app(user)) as ac:
        on_resp = await ac.post(
            "/api/v1/skills",
            json={"name": "lit-on", "files": {"SKILL.md": _skill_md("lit-on")}},
            headers=_HDRS,
        )
        assert on_resp.status_code == 201, on_resp.text
        sid_on = on_resp.json()["id"]
        off_resp = await ac.post(
            "/api/v1/skills",
            json={
                "name": "lit-off",
                "enabled": False,
                "files": {"SKILL.md": _skill_md("lit-off")},
            },
            headers=_HDRS,
        )
        assert off_resp.status_code == 201, off_resp.text
        sid_off = off_resp.json()["id"]
        await ac.post(
            f"/api/v1/coworkers/{cw_id}/skills/{sid_on}", headers=_HDRS,
        )
        await ac.post(
            f"/api/v1/coworkers/{cw_id}/skills/{sid_off}", headers=_HDRS,
        )
        resp = await ac.get(
            f"/api/v1/coworkers/{cw_id}/skills", headers=_HDRS,
        )
    assert resp.status_code == 200
    by_id = {row["skill_id"]: row for row in resp.json()}
    assert by_id[sid_on]["enabled"] is True
    # Junction flag is TRUE even though the catalog skill is disabled.
    assert by_id[sid_off]["enabled"] is True


async def test_disable_skill_flag_via_helper_then_list_still_returns_binding() -> None:
    """Flipping the junction's ``enabled`` flag (helper-level, not a
    wire endpoint) keeps the binding row but disables projection.
    The list endpoint still shows it so admin tooling can see the
    disabled state.
    """
    user, cw_id = await _make_user_and_coworker("flag")
    async with _client(_build_app(user)) as ac:
        sid = (await ac.post(
            "/api/v1/skills",
            json={"name": "fl", "files": {"SKILL.md": _skill_md("fl")}},
            headers=_HDRS,
        )).json()["id"]
        await ac.post(
            f"/api/v1/coworkers/{cw_id}/skills/{sid}", headers=_HDRS,
        )
    # Flip the junction flag via the DB helper directly — no wire
    # endpoint for this yet (it would be PATCH on the binding).
    await enable_skill_for_coworker(
        skill_id=sid, coworker_id=cw_id,
        tenant_id=user.tenant_id, enabled=False,
    )
    async with _client(_build_app(user)) as ac:
        resp = await ac.get(
            f"/api/v1/coworkers/{cw_id}/skills", headers=_HDRS,
        )
    by_id = {row["skill_id"]: row for row in resp.json()}
    assert sid in by_id
    assert by_id[sid]["enabled"] is False


# ---------------------------------------------------------------------------
# RLS isolation
# ---------------------------------------------------------------------------


async def test_cross_tenant_read_returns_404() -> None:
    """RLS reduces wrong-tenant to not-found at the catalog level."""
    user_a, _ = await _make_user_and_coworker("a")
    user_b, _ = await _make_user_and_coworker("b")
    async with _client(_build_app(user_b)) as ac:
        sid = (await ac.post(
            "/api/v1/skills",
            json={"name": "secret", "files": {"SKILL.md": _skill_md("secret")}},
            headers=_HDRS,
        )).json()["id"]
    async with _client(_build_app(user_a)) as ac:
        resp = await ac.get(f"/api/v1/skills/{sid}", headers=_HDRS)
    assert resp.status_code == 404


async def test_cross_tenant_list_excludes_other_tenant() -> None:
    user_a, _ = await _make_user_and_coworker("la")
    user_b, _ = await _make_user_and_coworker("lb")
    async with _client(_build_app(user_b)) as ac:
        await ac.post(
            "/api/v1/skills",
            json={"name": "secret-b", "files": {"SKILL.md": _skill_md("secret-b")}},
            headers=_HDRS,
        )
    async with _client(_build_app(user_a)) as ac:
        resp = await ac.get("/api/v1/skills", headers=_HDRS)
    names = {s["name"] for s in resp.json()["items"]}
    assert "secret-b" not in names


# ---------------------------------------------------------------------------
# Event broadcasts
# ---------------------------------------------------------------------------


async def test_put_file_publishes_skills_changed_per_bound_coworker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file edit on a catalog skill bound to N coworkers must emit
    exactly N ``skills_changed`` events — one per cached projection
    that needs refreshing.
    """
    user, cw_id = await _make_user_and_coworker("ev")
    async with _client(_build_app(user)) as ac:
        sid = (await ac.post(
            "/api/v1/skills",
            json={"name": "ev", "files": {"SKILL.md": _skill_md("ev")}},
            headers=_HDRS,
        )).json()["id"]
        await ac.post(
            f"/api/v1/coworkers/{cw_id}/skills/{sid}", headers=_HDRS,
        )

    mock_pub = AsyncMock()
    monkeypatch.setattr(
        "webui.v1.skills.coworker_events.publish_coworker_skills_changed",
        mock_pub,
    )
    async with _client(_build_app(user)) as ac:
        resp = await ac.put(
            f"/api/v1/skills/{sid}/files/extra.md",
            json={"content": "x"},
            headers=_HDRS,
        )
    assert resp.status_code == 200
    # One bound coworker → one publish.
    assert mock_pub.await_count == 1
    args = mock_pub.await_args_list[0].kwargs
    assert args["coworker_id"] == cw_id
    assert args["tenant_id"] == user.tenant_id


# ---------------------------------------------------------------------------
# PR21: PATCH /api/v1/skills/{id} with `files` — full-set replacement
# ---------------------------------------------------------------------------
#
# Pre-PR21 the SkillUpdate schema dropped `files` entirely, so the
# edit dialog's PATCH silently lost any body or extra-file changes. The
# tests below pin the new behavior end-to-end (wire boundary, not the
# DB helper) and would all fail against the pre-PR21 handler.


async def test_patch_with_files_replaces_skill_md_body() -> None:
    """Edit dialog's main case: user changed SKILL.md body, PATCH
    replaces the on-disk content.

    Pre-PR21 this was the silent-drop bug — PATCH returned 200 but the
    GET still showed the old body.
    """
    user, _cw = await _make_user_and_coworker("pfb")
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/skills",
            json={
                "name": "edit-body",
                "files": {"SKILL.md": _skill_md("edit-body")},
            },
            headers=_HDRS,
        )
        sid = created.json()["id"]
        new_md = (
            "---\n"
            "name: edit-body\n"
            f"description: {_GOOD_DESC}\n"
            "---\n"
            "# Updated body\nNew instructions live here.\n"
        )
        patch = await ac.patch(
            f"/api/v1/skills/{sid}",
            json={"files": {"SKILL.md": new_md}},
            headers=_HDRS,
        )
        assert patch.status_code == 200, patch.text
        got = await ac.get(f"/api/v1/skills/{sid}", headers=_HDRS)
    assert got.status_code == 200
    body = got.json()
    md = body["files"]["SKILL.md"]["content"]
    assert "# Updated body" in md
    assert "Updated body" in md
    # Frontmatter is stripped from on-disk SKILL.md — the create
    # contract carries through to PATCH.
    assert "name: edit-body" not in md


async def test_patch_with_files_replaces_extras_atomically() -> None:
    """Add `references/intro.md`, then PATCH with a different file map
    that drops it and adds `scripts/helper.py`. The result must be
    EXACTLY the new map — atomic replacement, not merge.

    Without atomic-replace, a user removing a stale reference would
    have to call DELETE explicitly; the dialog can just send the new
    full snapshot.
    """
    user, _cw = await _make_user_and_coworker("pfe")
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/skills",
            json={
                "name": "extras",
                "files": {
                    "SKILL.md": _skill_md("extras"),
                    "references/intro.md": "intro v1",
                },
            },
            headers=_HDRS,
        )
        sid = created.json()["id"]
        patch = await ac.patch(
            f"/api/v1/skills/{sid}",
            json={
                "files": {
                    "SKILL.md": _skill_md("extras"),
                    "scripts/helper.py": "print('hi')\n",
                },
            },
            headers=_HDRS,
        )
        assert patch.status_code == 200, patch.text
        got = await ac.get(f"/api/v1/skills/{sid}", headers=_HDRS)
    files = got.json()["files"]
    # The new map wins entirely — references/intro.md is gone.
    assert set(files.keys()) == {"SKILL.md", "scripts/helper.py"}
    assert files["scripts/helper.py"]["content"] == "print('hi')\n"


async def test_patch_files_rejects_invalid_path_with_422() -> None:
    """Path traversal via PATCH must hit the same INVALID_PATH 422
    that POST does — defense in depth: the API surface for paths is
    uniform regardless of HTTP verb.
    """
    user, _cw = await _make_user_and_coworker("pfi")
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/skills",
            json={
                "name": "bp",
                "files": {"SKILL.md": _skill_md("bp")},
            },
            headers=_HDRS,
        )
        sid = created.json()["id"]
        patch = await ac.patch(
            f"/api/v1/skills/{sid}",
            json={
                "files": {
                    "SKILL.md": _skill_md("bp"),
                    "../etc/passwd": "x",
                },
            },
            headers=_HDRS,
        )
    assert patch.status_code == 422
    assert patch.json()["code"] == "INVALID_PATH"


async def test_patch_files_without_skill_md_returns_422() -> None:
    """Same invariant as create: SKILL.md must always be present.
    Without it the skill has no manifest and the projector breaks.
    """
    user, _cw = await _make_user_and_coworker("pfm")
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/skills",
            json={"name": "nm", "files": {"SKILL.md": _skill_md("nm")}},
            headers=_HDRS,
        )
        sid = created.json()["id"]
        patch = await ac.patch(
            f"/api/v1/skills/{sid}",
            json={"files": {"only-extra.md": "x"}},
            headers=_HDRS,
        )
    assert patch.status_code == 422
    assert patch.json()["code"] == "SKILL_MANIFEST_REQUIRED"


async def test_patch_name_matching_existing_is_accepted() -> None:
    """The edit dialog sends a full snapshot including the unchanged
    name; accepting a no-op name eliminates a special case in the
    frontend's body construction.
    """
    user, _cw = await _make_user_and_coworker("pnm")
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/skills",
            json={"name": "stable", "files": {"SKILL.md": _skill_md("stable")}},
            headers=_HDRS,
        )
        sid = created.json()["id"]
        patch = await ac.patch(
            f"/api/v1/skills/{sid}",
            json={"name": "stable", "enabled": False},
            headers=_HDRS,
        )
    assert patch.status_code == 200, patch.text


async def test_patch_name_change_rejected_with_400() -> None:
    """Rename is multi-step (filesystem dir on the agent side,
    UNIQUE (tenant_id, name) constraint, plus potential coworker
    binding churn). Reject at the wire to keep the contract simple.
    """
    user, _cw = await _make_user_and_coworker("prn")
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/skills",
            json={"name": "original", "files": {"SKILL.md": _skill_md("original")}},
            headers=_HDRS,
        )
        sid = created.json()["id"]
        patch = await ac.patch(
            f"/api/v1/skills/{sid}",
            json={"name": "renamed"},
            headers=_HDRS,
        )
    assert patch.status_code == 400
    assert patch.json()["code"] == "INVALID_PAYLOAD"
    assert "immutable" in patch.json()["message"]


async def test_patch_files_refreshes_frontmatter_description() -> None:
    """If the user edits SKILL.md to change `description:`, the list
    summary (which reads from `frontmatter_common.description`) must
    reflect the new value. Without re-parsing on PATCH, the list
    keeps showing the old description while GET on the skill shows
    the new SKILL.md body — a silent staleness bug.
    """
    user, _cw = await _make_user_and_coworker("pfd")
    new_desc = (
        "After PR21 the dialog can edit the description by replacing "
        "the whole SKILL.md and the list reflects it."
    )
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/skills",
            json={"name": "rdesc", "files": {"SKILL.md": _skill_md("rdesc")}},
            headers=_HDRS,
        )
        sid = created.json()["id"]
        new_md = (
            "---\n"
            "name: rdesc\n"
            f"description: {new_desc}\n"
            "---\n# body\n"
        )
        await ac.patch(
            f"/api/v1/skills/{sid}",
            json={"files": {"SKILL.md": new_md}},
            headers=_HDRS,
        )
        listing = await ac.get("/api/v1/skills", headers=_HDRS)
    summaries = {s["name"]: s for s in listing.json()["items"]}
    assert summaries["rdesc"]["description"] == new_desc
