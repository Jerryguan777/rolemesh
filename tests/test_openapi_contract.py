"""Anchor the OpenAPI yaml to live Python behavior.

The freshness test (``test_openapi_codegen_freshness.py``) checks
yaml -> generated TS stays in sync. This file checks two more
adjacency seams that the codegen alone cannot:

1. yaml ↔ ``webui.schemas_v1`` (the Pydantic models referenced by
   ``response_model=``). Drift here means the FastAPI handler
   passes validation against a stale shape and the typed frontend
   crashes on a real field.
2. yaml ↔ actual HTTP response from ``/api/v1/backends``. Catches
   "we declared four fields but the handler only emits three".

Both checks are tight against the implementation — the yaml's
``Backend.required`` list, the Pydantic field set, and the JSON
payload keys must be EQUAL (no superset relaxation). When a new
field is added, you change all three.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webui.api_v1 import router
from webui.schemas_v1 import (
    ApprovalAuditEntry,
    ApprovalDecide,
    ApprovalPolicy,
    ApprovalPolicyCreate,
    ApprovalRequest,
    Backend,
    Conversation,
    CoworkerMCPBindingCreate,
    CoworkerMCPBindingResponse,
    CredentialResponse,
    CredentialUpsert,
    ErrorResponse,
    MCPServer,
    MCPServerCreate,
    Message,
    Model,
    Run,
)

OPENAPI_PATH = Path(__file__).resolve().parent.parent / "web" / "openapi.yaml"


def _load_spec() -> dict[str, object]:
    with OPENAPI_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _schema(spec: dict[str, object], name: str) -> dict[str, object]:
    return spec["components"]["schemas"][name]  # type: ignore[index]


def test_phase_1_endpoints_listed_in_design_are_present_in_yaml() -> None:
    """Every Phase 1 endpoint named in design §3 must appear in the yaml.

    This is a coverage gate, not a structural check — yaml-side
    schema details are exercised in the per-endpoint tests below.
    """
    spec = _load_spec()
    paths = set(spec["paths"].keys())  # type: ignore[union-attr]
    expected = {
        "/api/v1/auth/config",
        "/api/v1/auth/ws-ticket",
        "/api/v1/me",
        "/api/v1/backends",
        "/api/v1/coworkers",
        "/api/v1/coworkers/{id}",
        "/api/v1/coworkers/{id}/conversations",
        "/api/v1/coworkers/{id}/mcp-servers",
        "/api/v1/coworkers/{id}/mcp-servers/{mcp_id}",
        "/api/v1/conversations/{id}",
        "/api/v1/conversations/{id}/messages",
        "/api/v1/mcp-servers",
        "/api/v1/mcp-servers/{id}",
        "/api/v1/models",
        "/api/v1/models/{id}",
        "/api/v1/tenant/credentials",
        "/api/v1/tenant/credentials/{provider}",
        "/api/v1/runs/{id}",
        "/api/v1/runs/{id}/cancel",
    }
    missing = expected - paths
    assert not missing, f"yaml missing Phase 1 endpoints: {sorted(missing)}"


def test_error_response_shape_matches_pydantic_model() -> None:
    """Design §13 requires `{code, message, details?}` everywhere.

    The yaml's ErrorResponse and the Python ErrorResponse should
    declare the *same* required field set — anything stricter on
    one side means a deserialization mismatch in production.
    """
    spec = _load_spec()
    yaml_err = _schema(spec, "ErrorResponse")
    yaml_required = set(yaml_err["required"])  # type: ignore[arg-type]
    py_required = {
        name
        for name, f in ErrorResponse.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"ErrorResponse.required drift: yaml={yaml_required} "
        f"python={py_required}"
    )
    # `details` must remain optional on both sides — making it
    # required would break every existing 4xx call site silently.
    assert "details" not in yaml_required
    assert "details" not in py_required


def test_backend_schema_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_backend = _schema(spec, "Backend")
    yaml_required = set(yaml_backend["required"])  # type: ignore[arg-type]
    py_required = {
        name for name, f in Backend.model_fields.items() if f.is_required()
    }
    assert yaml_required == py_required, (
        f"Backend.required drift: yaml={yaml_required} python={py_required}"
    )


def test_backends_endpoint_payload_matches_yaml_schema() -> None:
    """Live response keys must equal the yaml's declared Backend fields.

    Catches the very specific class of bug where the handler
    accidentally drops a key (e.g. a refactor renames it) but the
    OpenAPI contract still advertises it. ``response_model=`` only
    rejects *extra* keys, not missing ones.
    """
    spec = _load_spec()
    declared = set(_schema(spec, "Backend")["properties"].keys())  # type: ignore[index]

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.get("/api/v1/backends")
    assert resp.status_code == 200
    body = resp.json()
    assert body, "expected at least one backend"

    for row in body:
        keys = set(row.keys())
        missing = declared - keys
        extra = keys - declared
        assert not missing, (
            f"backend payload missing yaml-declared fields: {sorted(missing)}"
        )
        assert not extra, (
            f"backend payload has fields not in yaml: {sorted(extra)} — "
            "yaml is the source of truth; update both yaml + schemas_v1"
        )


def test_backend_name_enum_matches_code_constants() -> None:
    """Adding a backend to ``ALL_BACKENDS`` without listing it in
    the yaml enum would let the handler return a value the typed
    client rejects.
    """
    from rolemesh.core.backend_capabilities import ALL_BACKENDS

    spec = _load_spec()
    yaml_enum = set(_schema(spec, "BackendName")["enum"])  # type: ignore[arg-type]
    assert yaml_enum == set(ALL_BACKENDS.keys()), (
        f"BackendName enum drift: yaml={yaml_enum} code={set(ALL_BACKENDS)}"
    )


def test_v1_runs_required_matches_pydantic_model() -> None:
    """``required`` for ``Run`` must agree on both sides.

    A field promoted to required in the yaml but optional in
    Pydantic would let the handler return ``null`` for what the
    typed client expects to always be present.
    """
    spec = _load_spec()
    yaml_required = set(_schema(spec, "Run")["required"])  # type: ignore[arg-type]
    py_required = {
        name for name, f in Run.model_fields.items() if f.is_required()
    }
    assert yaml_required == py_required, (
        f"Run.required drift: yaml={yaml_required} python={py_required}"
    )


def test_v1_conversation_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(_schema(spec, "Conversation")["required"])  # type: ignore[arg-type]
    py_required = {
        name
        for name, f in Conversation.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"Conversation.required drift: yaml={yaml_required} python={py_required}"
    )


def test_v1_message_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(_schema(spec, "Message")["required"])  # type: ignore[arg-type]
    py_required = {
        name for name, f in Message.model_fields.items() if f.is_required()
    }
    assert yaml_required == py_required, (
        f"Message.required drift: yaml={yaml_required} python={py_required}"
    )


def test_run_status_enum_matches_lifecycle_terminal_set() -> None:
    """The yaml RunStatus enum must include every status the
    lifecycle helper can write (``running`` + the four terminal
    states). A drift would let the engine emit a status the typed
    client rejects."""
    from rolemesh.runs.lifecycle import _TERMINAL_STATUSES

    spec = _load_spec()
    yaml_enum = set(_schema(spec, "RunStatus")["enum"])  # type: ignore[arg-type]
    expected = set(_TERMINAL_STATUSES) | {"running"}
    assert yaml_enum == expected, (
        f"RunStatus enum drift: yaml={yaml_enum} expected={expected}"
    )


def test_v1_model_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(_schema(spec, "Model")["required"])  # type: ignore[arg-type]
    py_required = {
        name for name, f in Model.model_fields.items() if f.is_required()
    }
    assert yaml_required == py_required, (
        f"Model.required drift: yaml={yaml_required} python={py_required}"
    )


def test_v1_credential_response_required_matches_pydantic_model() -> None:
    """CredentialResponse may never grow a credential-payload field.

    Beyond the required-set match we also assert the absolute absence
    of any field name that could possibly carry the plaintext — a
    structural guard against accidentally re-introducing the leak
    surface design §8.1 explicitly forbids.
    """
    spec = _load_spec()
    yaml_required = set(_schema(spec, "CredentialResponse")["required"])  # type: ignore[arg-type]
    py_required = {
        name
        for name, f in CredentialResponse.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"CredentialResponse.required drift: yaml={yaml_required} "
        f"python={py_required}"
    )
    py_fields = set(CredentialResponse.model_fields.keys())
    for forbidden in ("credential_data", "api_key", "secret", "credential_ref"):
        assert forbidden not in py_fields, (
            f"CredentialResponse must NOT declare a {forbidden!r} field "
            "(design §8.1 — list/get response never exposes plaintext)"
        )
    yaml_props = set(
        _schema(spec, "CredentialResponse")["properties"].keys()  # type: ignore[index]
    )
    for forbidden in ("credential_data", "api_key", "secret", "credential_ref"):
        assert forbidden not in yaml_props, (
            f"CredentialResponse yaml must NOT declare a {forbidden!r} field"
        )


def test_v1_credential_upsert_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(_schema(spec, "CredentialUpsert")["required"])  # type: ignore[arg-type]
    py_required = {
        name
        for name, f in CredentialUpsert.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"CredentialUpsert.required drift: yaml={yaml_required} "
        f"python={py_required}"
    )


def test_v1_mcp_server_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(_schema(spec, "MCPServer")["required"])  # type: ignore[arg-type]
    py_required = {
        name for name, f in MCPServer.model_fields.items() if f.is_required()
    }
    assert yaml_required == py_required, (
        f"MCPServer.required drift: yaml={yaml_required} python={py_required}"
    )


def test_v1_mcp_server_create_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(_schema(spec, "MCPServerCreate")["required"])  # type: ignore[arg-type]
    py_required = {
        name
        for name, f in MCPServerCreate.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"MCPServerCreate.required drift: yaml={yaml_required} "
        f"python={py_required}"
    )


def test_v1_coworker_mcp_binding_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(
        _schema(spec, "CoworkerMCPBindingResponse")["required"]  # type: ignore[arg-type]
    )
    py_required = {
        name
        for name, f in CoworkerMCPBindingResponse.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"CoworkerMCPBindingResponse.required drift: "
        f"yaml={yaml_required} python={py_required}"
    )


def test_v1_coworker_mcp_binding_create_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(
        _schema(spec, "CoworkerMCPBindingCreate")["required"]  # type: ignore[arg-type]
    )
    py_required = {
        name
        for name, f in CoworkerMCPBindingCreate.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"CoworkerMCPBindingCreate.required drift: "
        f"yaml={yaml_required} python={py_required}"
    )


def test_phase_3_approval_endpoints_are_present_in_yaml() -> None:
    """03a session lands these v1 endpoints; the legacy
    /api/admin/approval* surface stays for the 6-month
    compatibility window (NOT listed here).
    """
    spec = _load_spec()
    paths = set(spec["paths"].keys())  # type: ignore[union-attr]
    expected = {
        "/api/v1/approval-policies",
        "/api/v1/approval-policies/{id}",
        "/api/v1/approvals",
        "/api/v1/approvals/{id}",
        "/api/v1/approvals/{id}/audit-log",
        "/api/v1/approvals/{id}/decide",
    }
    missing = expected - paths
    assert not missing, f"yaml missing v1 approval endpoints: {sorted(missing)}"


def test_v1_approval_policy_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(_schema(spec, "ApprovalPolicy")["required"])  # type: ignore[arg-type]
    py_required = {
        name
        for name, f in ApprovalPolicy.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"ApprovalPolicy.required drift: yaml={yaml_required} "
        f"python={py_required}"
    )


def test_v1_approval_policy_create_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(
        _schema(spec, "ApprovalPolicyCreate")["required"]  # type: ignore[arg-type]
    )
    py_required = {
        name
        for name, f in ApprovalPolicyCreate.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"ApprovalPolicyCreate.required drift: yaml={yaml_required} "
        f"python={py_required}"
    )


def test_v1_approval_request_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(
        _schema(spec, "ApprovalRequest")["required"]  # type: ignore[arg-type]
    )
    py_required = {
        name
        for name, f in ApprovalRequest.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"ApprovalRequest.required drift: yaml={yaml_required} "
        f"python={py_required}"
    )


def test_v1_approval_audit_entry_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(
        _schema(spec, "ApprovalAuditEntry")["required"]  # type: ignore[arg-type]
    )
    py_required = {
        name
        for name, f in ApprovalAuditEntry.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"ApprovalAuditEntry.required drift: yaml={yaml_required} "
        f"python={py_required}"
    )


def test_v1_approval_decide_required_matches_pydantic_model() -> None:
    """INV-7 anchor: the wire-enum side of the translation must
    stay closed. A drift here would let a new wire value land on
    the API while the engine still maps a subset → silent dropped
    decisions.
    """
    spec = _load_spec()
    yaml_required = set(
        _schema(spec, "ApprovalDecide")["required"]  # type: ignore[arg-type]
    )
    py_required = {
        name
        for name, f in ApprovalDecide.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"ApprovalDecide.required drift: yaml={yaml_required} "
        f"python={py_required}"
    )
    yaml_enum = set(
        _schema(spec, "ApprovalDecide")["properties"]["action"]["enum"]  # type: ignore[index]
    )
    from rolemesh.approval.enum_translate import _HTTP_ACTIONS

    assert yaml_enum == set(_HTTP_ACTIONS), (
        f"ApprovalDecide.action enum drift: yaml={yaml_enum} "
        f"engine={set(_HTTP_ACTIONS)}"
    )


def test_backends_endpoint_advertises_cache_control_header_in_yaml() -> None:
    """The handler sets ``Cache-Control: max-age=3600``; the yaml
    declares that contract. If somebody drops the header in code
    the freshness check at the *header* level still catches it.
    """
    spec = _load_spec()
    op = spec["paths"]["/api/v1/backends"]["get"]  # type: ignore[index]
    headers = op["responses"]["200"]["headers"]
    assert "Cache-Control" in headers
