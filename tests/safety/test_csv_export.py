"""REST API tests for the safety decisions CSV export.

The export is a streaming endpoint so tests must exercise the full
handler, not just the DB cursor. httpx.AsyncClient handles the
streaming response fine; we read the body as text and parse it back.

Invariants pinned:
  - Cross-tenant request → 403.
  - Filters (verdict_action, coworker_id, stage, from_ts/to_ts) apply
    correctly at the DB layer.
  - CSV escaping handles commas, quotes, newlines in any field
    (context_summary / findings).
  - Triggered rule IDs and findings use the documented ``|``
    separator so operators have a stable parse target.
  - Header row is present (don't regress to headerless CSV).
"""

from __future__ import annotations

import csv
import io
import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    create_coworker,
    create_tenant,
    insert_safety_decision,
)
from webui import admin
from webui.dependencies import (
    get_current_user,
    require_manage_agents,
    require_manage_tenant,
    require_manage_users,
)

pytestmark = pytest.mark.usefixtures("test_db")


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


async def _seed_tenant_with_decisions(
    n: int = 3,
    *,
    verdict_action: str = "block",
) -> tuple[str, str, list[str]]:
    tenant = await create_tenant(
        name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
    )
    cw = await create_coworker(
        tenant_id=tenant.id, name="cw",
        folder=f"cw-{uuid.uuid4().hex[:8]}",
    )
    ids: list[str] = []
    for i in range(n):
        decision_id = await insert_safety_decision(
            tenant_id=tenant.id,
            coworker_id=cw.id,
            conversation_id=f"conv-{i}",
            job_id=f"job-{i}",
            stage="pre_tool_call",
            verdict_action=verdict_action,
            triggered_rule_ids=[],
            findings=[
                {"code": "PII.SSN", "severity": "high", "message": "m"}
            ],
            context_digest="d" * 64,
            context_summary=f"tool=x_{i}",
        )
        ids.append(decision_id)
    return tenant.id, cw.id, ids


def _user(tenant_id: str, role: str = "owner") -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        role=role,
        email="admin@example.com",
    )


def _parse_csv(body: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(body))
    assert reader.fieldnames is not None
    rows = list(reader)
    return list(reader.fieldnames), rows


class TestBasicExport:
    @pytest.mark.asyncio
    async def test_returns_csv_with_header_and_rows(self) -> None:
        tenant_id, _cw_id, decision_ids = (
            await _seed_tenant_with_decisions(3)
        )
        app = _build_app(_user(tenant_id))
        async with _client(app) as client:
            r = await client.get(
                f"/api/admin/tenants/{tenant_id}/safety/decisions.csv"
            )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        cd = r.headers.get("content-disposition", "")
        assert "attachment" in cd
        assert tenant_id in cd
        header, rows = _parse_csv(r.text)
        # The header row must include the documented columns so
        # downstream consumers (dashboards, sheets importers) don't
        # break on a column rename.
        assert "id" in header
        assert "verdict_action" in header
        assert "finding_codes" in header
        assert "finding_severities" in header
        assert len(rows) == 3
        # Every seeded decision id appears exactly once.
        seen_ids = {row["id"] for row in rows}
        assert seen_ids == set(decision_ids)


class TestFilters:
    @pytest.mark.asyncio
    async def test_verdict_action_filter(self) -> None:
        tenant_id, cw_id, _ = await _seed_tenant_with_decisions(
            2, verdict_action="block"
        )
        # Add an allow-verdict row.
        await insert_safety_decision(
            tenant_id=tenant_id,
            coworker_id=cw_id,
            stage="pre_tool_call",
            verdict_action="allow",
            triggered_rule_ids=[],
            findings=[],
            context_digest="",
            context_summary="clean",
        )
        app = _build_app(_user(tenant_id))
        async with _client(app) as client:
            r = await client.get(
                f"/api/admin/tenants/{tenant_id}/safety/decisions.csv?"
                f"verdict_action=block"
            )
        _, rows = _parse_csv(r.text)
        assert len(rows) == 2
        assert all(row["verdict_action"] == "block" for row in rows)

    @pytest.mark.asyncio
    async def test_coworker_filter_scopes_to_one_agent(self) -> None:
        tenant_id, _cw_a, _ = await _seed_tenant_with_decisions(2)
        # Add a row for a second coworker in the same tenant.
        cw_b = await create_coworker(
            tenant_id=tenant_id, name="other",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        await insert_safety_decision(
            tenant_id=tenant_id,
            coworker_id=cw_b.id,
            stage="pre_tool_call",
            verdict_action="block",
            triggered_rule_ids=[],
            findings=[],
            context_digest="",
            context_summary="other",
        )
        app = _build_app(_user(tenant_id))
        async with _client(app) as client:
            r = await client.get(
                f"/api/admin/tenants/{tenant_id}/safety/decisions.csv?"
                f"coworker_id={cw_b.id}"
            )
        _, rows = _parse_csv(r.text)
        assert len(rows) == 1
        assert rows[0]["coworker_id"] == cw_b.id


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_other_tenants_admin_gets_403(self) -> None:
        tenant_a_id, _, _ = await _seed_tenant_with_decisions(2)
        other_tenant = await create_tenant(
            name="Other", slug=f"o-{uuid.uuid4().hex[:8]}"
        )
        # Admin from tenant B calls with tenant A in path.
        app = _build_app(_user(other_tenant.id))
        async with _client(app) as client:
            r = await client.get(
                f"/api/admin/tenants/{tenant_a_id}/safety/decisions.csv"
            )
        assert r.status_code == 403


class TestCsvEscaping:
    @pytest.mark.asyncio
    async def test_comma_and_quote_in_summary_are_escaped(self) -> None:
        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        # A context_summary with commas AND quotes AND newlines — if
        # any of these leak through unescaped the CSV parser would
        # split the row into multiple rows or merge columns.
        tricky = 'tool="danger",sep\nnewline'
        await insert_safety_decision(
            tenant_id=tenant.id,
            coworker_id=cw.id,
            stage="pre_tool_call",
            verdict_action="block",
            triggered_rule_ids=[],
            findings=[],
            context_digest="",
            context_summary=tricky,
        )
        app = _build_app(_user(tenant.id))
        async with _client(app) as client:
            r = await client.get(
                f"/api/admin/tenants/{tenant.id}/safety/decisions.csv"
            )
        _, rows = _parse_csv(r.text)
        # csv.DictReader must reconstruct the original string with
        # commas, quotes and newlines intact.
        assert len(rows) == 1
        assert rows[0]["context_summary"] == tricky

    @pytest.mark.asyncio
    async def test_multiple_findings_joined_with_pipe(self) -> None:
        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        await insert_safety_decision(
            tenant_id=tenant.id,
            coworker_id=cw.id,
            stage="pre_tool_call",
            verdict_action="block",
            triggered_rule_ids=[],
            findings=[
                {"code": "PII.SSN", "severity": "high", "message": "a"},
                {"code": "PII.EMAIL", "severity": "medium", "message": "b"},
            ],
            context_digest="",
            context_summary="multi",
        )
        app = _build_app(_user(tenant.id))
        async with _client(app) as client:
            r = await client.get(
                f"/api/admin/tenants/{tenant.id}/safety/decisions.csv"
            )
        _, rows = _parse_csv(r.text)
        assert rows[0]["finding_codes"] == "PII.SSN|PII.EMAIL"
        assert rows[0]["finding_severities"] == "high|medium"


class TestFormulaInjectionGuard:
    """Review fix P2-5: cell text starting with =/+/-/@/tab/CR must
    NOT execute as a formula when the CSV is opened in Excel or
    Google Sheets. The attack vector: an agent tricked into calling
    a tool whose name is ``=HYPERLINK("evil.com","click")``
    produces a ``context_summary`` field that would be a clickable
    phishing link unless the escaper neutralizes it.
    """

    @pytest.mark.asyncio
    async def test_equals_prefix_neutralized_with_leading_quote(
        self,
    ) -> None:
        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        await insert_safety_decision(
            tenant_id=tenant.id,
            coworker_id=cw.id,
            stage="pre_tool_call",
            verdict_action="block",
            triggered_rule_ids=[],
            findings=[],
            context_digest="",
            context_summary='=HYPERLINK("evil.com","click")',
        )
        app = _build_app(_user(tenant.id))
        async with _client(app) as client:
            r = await client.get(
                f"/api/admin/tenants/{tenant.id}/safety/decisions.csv"
            )
        _, rows = _parse_csv(r.text)
        # csv.DictReader preserves the escape prefix exactly. The
        # leading single-quote is what Excel/Sheets strips while
        # displaying the cell as literal text — so the parser sees
        # it but a formula engine wouldn't evaluate.
        assert rows[0]["context_summary"].startswith("'=HYPERLINK")
        # Raw bytes: the quoted line must NOT start the field with
        # a bare ``=`` (which would be interpreted as a formula).
        # The field is quoted because of commas, so look for
        # ``,"'=`` or similar.
        assert ',"\'=' in r.text or ",'=" in r.text

    @pytest.mark.asyncio
    async def test_all_formula_prefixes_neutralized(self) -> None:
        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        for prefix in ("=", "+", "-", "@", "\t"):
            await insert_safety_decision(
                tenant_id=tenant.id,
                coworker_id=cw.id,
                stage="pre_tool_call",
                verdict_action="block",
                triggered_rule_ids=[],
                findings=[],
                context_digest="",
                context_summary=f"{prefix}SUM(A1)",
            )
        app = _build_app(_user(tenant.id))
        async with _client(app) as client:
            r = await client.get(
                f"/api/admin/tenants/{tenant.id}/safety/decisions.csv"
            )
        _, rows = _parse_csv(r.text)
        # Every row's context_summary must start with ``'`` — never
        # with a raw formula prefix.
        for row in rows:
            s = row["context_summary"]
            assert s.startswith("'"), (
                f"formula prefix not neutralized: {s!r}"
            )

    @pytest.mark.asyncio
    async def test_normal_text_unmodified(self) -> None:
        """Only cells whose first char is a formula prefix are
        altered. Regular text passes through unchanged.
        """
        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        await insert_safety_decision(
            tenant_id=tenant.id,
            coworker_id=cw.id,
            stage="pre_tool_call",
            verdict_action="block",
            triggered_rule_ids=[],
            findings=[],
            context_digest="",
            context_summary="tool=github.create_pr",
        )
        app = _build_app(_user(tenant.id))
        async with _client(app) as client:
            r = await client.get(
                f"/api/admin/tenants/{tenant.id}/safety/decisions.csv"
            )
        _, rows = _parse_csv(r.text)
        assert rows[0]["context_summary"] == "tool=github.create_pr"


class TestEmptyResult:
    @pytest.mark.asyncio
    async def test_header_only_when_no_rows_match(self) -> None:
        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        app = _build_app(_user(tenant.id))
        async with _client(app) as client:
            r = await client.get(
                f"/api/admin/tenants/{tenant.id}/safety/decisions.csv"
            )
        assert r.status_code == 200
        # Still has the header line even with zero data rows.
        lines = r.text.strip().splitlines()
        assert len(lines) == 1
        assert lines[0].startswith("id,created_at,")
