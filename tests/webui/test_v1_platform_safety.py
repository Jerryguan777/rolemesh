"""Integration tests for ``/api/v1/platform/safety/rules`` (platform rules).

Pins the platform-admin safety-rule surface:

- ``safety.platform.manage`` gating: a tenant ``owner`` is denied 403 on
  every endpoint; only ``platform_admin`` reaches them.
- list returns ALL tiers — including ``floor`` (which the tenant-facing
  read hides) — plus the 5 seeded factory defaults.
- create / get / patch / enable / disable round-trips; ``check_id`` is
  validated against the orchestrator registry (unknown / unsupported-stage
  / bad action_override → 400) and a duplicate identity → 409.
- seeded factory defaults (``is_seeded=True``) are disable-only: a hard
  DELETE is refused (409) but disable + config edits are allowed.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import admin_conn, create_tenant, create_user
from webui.api_v1 import router as api_v1_router
from webui.dependencies import get_current_user
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")

_H = {"Authorization": "Bearer x"}
_BASE = "/api/v1/platform/safety/rules"

# check_ids that exist in the ORCHESTRATOR registry (the validation
# authority). Seeded defaults reference container-side checks
# (secret_scanner / llm_guard.*) that are absent from it, so creates use
# these instead.
_PII = "pii.regex"  # stages: input_prompt, model_output, post_tool_result, pre_tool_call
_DOMAIN = "domain_allowlist"  # stage: pre_tool_call only

_EXPECTED_SEEDED_CHECKS = {
    "secret_scanner",
    "pii.regex",
    "llm_guard.prompt_injection",
    "llm_guard.jailbreak",
    "llm_guard.toxicity",
}


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


async def _make_user(role: str, slug: str = "plat") -> AuthenticatedUser:
    t = await create_tenant(name=f"T-{slug}", slug=f"{slug}-{uuid.uuid4().hex[:8]}")
    u = await create_user(
        tenant_id=t.id, name="Op",
        email=f"o-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    return AuthenticatedUser(
        user_id=u.id, tenant_id=t.id, role=role, email="x@x.com", name="X",
    )


async def _create(ac: httpx.AsyncClient, **body: object) -> httpx.Response:
    payload: dict[str, object] = {
        "tier": "transparent_floor",
        "stage": "input_prompt",
        "check_id": _PII,
        "config": {},
        "priority": 500,
        "description": "test rule",
    }
    payload.update(body)
    return await ac.post(_BASE, json=payload, headers=_H)


# ---------------------------------------------------------------------------
# Role gate — only platform_admin
# ---------------------------------------------------------------------------


async def test_owner_is_forbidden_on_every_endpoint() -> None:
    user = await _make_user("owner")
    rid = str(uuid.uuid4())
    async with _client(_build_app(user)) as ac:
        assert (await ac.get(_BASE, headers=_H)).status_code == 403
        assert (await _create(ac)).status_code == 403
        assert (await ac.get(f"{_BASE}/{rid}", headers=_H)).status_code == 403
        assert (
            await ac.patch(f"{_BASE}/{rid}", json={"enabled": False}, headers=_H)
        ).status_code == 403
        assert (
            await ac.post(f"{_BASE}/{rid}/enable", headers=_H)
        ).status_code == 403
        assert (
            await ac.post(f"{_BASE}/{rid}/disable", headers=_H)
        ).status_code == 403
        assert (await ac.delete(f"{_BASE}/{rid}", headers=_H)).status_code == 403


# ---------------------------------------------------------------------------
# List — all tiers (incl floor) + seeded defaults
# ---------------------------------------------------------------------------


async def test_list_includes_seeded_defaults_and_floor_tier() -> None:
    user = await _make_user("platform_admin")
    # Inject a floor-tier rule directly — the tenant read hides floor; the
    # platform list must surface it.
    async with admin_conn() as conn:
        await conn.execute(
            "INSERT INTO platform_safety_rules (tier, stage, check_id) "
            "VALUES ('floor', 'input_prompt', 'pii.regex') "
            "ON CONFLICT (tier, check_id, stage) DO NOTHING"
        )
    async with _client(_build_app(user)) as ac:
        resp = await ac.get(_BASE, headers=_H)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    seeded = {r["check_id"] for r in rows if r["is_seeded"]}
    assert seeded >= _EXPECTED_SEEDED_CHECKS
    assert all(r["is_seeded"] for r in rows if r["tier"] == "default" and
               r["check_id"] in _EXPECTED_SEEDED_CHECKS)
    # floor tier is visible to the platform admin (unlike the tenant read).
    assert any(r["tier"] == "floor" for r in rows)


# ---------------------------------------------------------------------------
# Create / get / validation
# ---------------------------------------------------------------------------


async def test_create_then_get_roundtrip() -> None:
    user = await _make_user("platform_admin")
    async with _client(_build_app(user)) as ac:
        created = await _create(
            ac, config={"patterns": {"EMAIL": True}}, priority=300,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["tier"] == "transparent_floor"
        assert body["check_id"] == _PII
        assert body["is_seeded"] is False
        assert body["config"] == {"patterns": {"EMAIL": True}}
        assert body["priority"] == 300
        rid = body["id"]

        got = await ac.get(f"{_BASE}/{rid}", headers=_H)
        assert got.status_code == 200
        assert got.json()["id"] == rid


async def test_get_unknown_id_is_404() -> None:
    user = await _make_user("platform_admin")
    async with _client(_build_app(user)) as ac:
        resp = await ac.get(f"{_BASE}/{uuid.uuid4()}", headers=_H)
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_create_unknown_check_id_rejected() -> None:
    user = await _make_user("platform_admin")
    async with _client(_build_app(user)) as ac:
        resp = await _create(ac, check_id="nope.not_a_check")
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_RULE"


async def test_create_unsupported_stage_rejected() -> None:
    user = await _make_user("platform_admin")
    async with _client(_build_app(user)) as ac:
        # domain_allowlist only supports pre_tool_call.
        resp = await _create(ac, check_id=_DOMAIN, stage="input_prompt")
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_RULE"


async def test_create_bad_action_override_rejected() -> None:
    user = await _make_user("platform_admin")
    async with _client(_build_app(user)) as ac:
        resp = await _create(ac, config={"action_override": "redact"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_RULE"


async def test_create_duplicate_identity_conflict() -> None:
    user = await _make_user("platform_admin")
    async with _client(_build_app(user)) as ac:
        # tier=default + pii.regex + input_prompt is a seeded identity.
        resp = await _create(ac, tier="default", check_id=_PII, stage="input_prompt")
    assert resp.status_code == 409
    assert resp.json()["code"] == "CONFLICT"


# ---------------------------------------------------------------------------
# Patch / enable / disable
# ---------------------------------------------------------------------------


async def test_patch_updates_mutable_fields() -> None:
    user = await _make_user("platform_admin")
    async with _client(_build_app(user)) as ac:
        rid = (await _create(ac)).json()["id"]
        patched = await ac.patch(
            f"{_BASE}/{rid}",
            json={
                "config": {"patterns": {"SSN": True}},
                "priority": 10,
                "description": "edited",
                "enabled": False,
            },
            headers=_H,
        )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["config"] == {"patterns": {"SSN": True}}
    assert body["priority"] == 10
    assert body["description"] == "edited"
    assert body["enabled"] is False


async def test_patch_invalid_config_rejected() -> None:
    user = await _make_user("platform_admin")
    async with _client(_build_app(user)) as ac:
        rid = (await _create(ac)).json()["id"]
        resp = await ac.patch(
            f"{_BASE}/{rid}",
            json={"config": {"action_override": "redact"}},
            headers=_H,
        )
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_RULE"


async def test_enable_disable_toggles_flag() -> None:
    user = await _make_user("platform_admin")
    async with _client(_build_app(user)) as ac:
        rid = (await _create(ac)).json()["id"]
        disabled = await ac.post(f"{_BASE}/{rid}/disable", headers=_H)
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        enabled = await ac.post(f"{_BASE}/{rid}/enable", headers=_H)
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True


# ---------------------------------------------------------------------------
# Delete — PA-created deletable, seeded defaults disable-only
# ---------------------------------------------------------------------------


async def test_delete_pa_created_rule_then_404() -> None:
    user = await _make_user("platform_admin")
    async with _client(_build_app(user)) as ac:
        rid = (await _create(ac)).json()["id"]
        first = await ac.delete(f"{_BASE}/{rid}", headers=_H)
        assert first.status_code == 204
        assert (await ac.get(f"{_BASE}/{rid}", headers=_H)).status_code == 404


async def test_seeded_default_cannot_be_deleted_but_can_be_disabled() -> None:
    user = await _make_user("platform_admin")
    async with _client(_build_app(user)) as ac:
        rows = (await ac.get(_BASE, headers=_H)).json()
        seeded = next(r for r in rows if r["is_seeded"])
        rid = seeded["id"]

        # Hard delete is refused.
        deleted = await ac.delete(f"{_BASE}/{rid}", headers=_H)
        assert deleted.status_code == 409
        assert deleted.json()["code"] == "SEEDED_RULE_IMMUTABLE"

        # Disable IS allowed (the sanctioned suppression path).
        disabled = await ac.post(f"{_BASE}/{rid}/disable", headers=_H)
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        # Still present (not deleted), just disabled.
        assert (await ac.get(f"{_BASE}/{rid}", headers=_H)).status_code == 200
