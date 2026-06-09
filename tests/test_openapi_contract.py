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
    Backend,
    Conversation,
    CoworkerMCPBindingCreate,
    CoworkerMCPBindingResponse,
    CoworkerSkillBinding,
    CredentialResponse,
    CredentialUpsert,
    ErrorResponse,
    MCPServer,
    MCPServerCreate,
    Message,
    Model,
    Run,
    SafetyCheck,
    SafetyDecision,
    SafetyDecisionPage,
    SafetyRule,
    SafetyRuleAuditEntry,
    Skill,
    SkillCreate,
    SkillSummary,
)

OPENAPI_PATH = (
    Path(__file__).resolve().parent.parent / "contracts" / "openapi.yaml"
)


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
        "/api/v1/credentials",
        "/api/v1/credentials/{provider}",
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


def test_phase_3_skills_endpoints_are_present_in_yaml() -> None:
    """03b session lands these v1 endpoints; the legacy
    /api/admin/agents/{id}/skills surface stays for the 6-month
    compatibility window (NOT listed here).
    """
    spec = _load_spec()
    paths = set(spec["paths"].keys())  # type: ignore[union-attr]
    expected = {
        "/api/v1/skills",
        "/api/v1/skills/{id}",
        "/api/v1/skills/{id}/files",
        "/api/v1/skills/{id}/files/{path}",
        "/api/v1/coworkers/{id}/skills",
        "/api/v1/coworkers/{id}/skills/{skill_id}",
    }
    missing = expected - paths
    assert not missing, f"yaml missing v1 skills endpoints: {sorted(missing)}"


def test_v1_skill_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(_schema(spec, "Skill")["required"])  # type: ignore[arg-type]
    py_required = {
        name for name, f in Skill.model_fields.items() if f.is_required()
    }
    assert yaml_required == py_required, (
        f"Skill.required drift: yaml={yaml_required} python={py_required}"
    )


def test_v1_skill_summary_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(_schema(spec, "SkillSummary")["required"])  # type: ignore[arg-type]
    py_required = {
        name
        for name, f in SkillSummary.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"SkillSummary.required drift: yaml={yaml_required} "
        f"python={py_required}"
    )


def test_v1_skill_create_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(_schema(spec, "SkillCreate")["required"])  # type: ignore[arg-type]
    py_required = {
        name
        for name, f in SkillCreate.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"SkillCreate.required drift: yaml={yaml_required} "
        f"python={py_required}"
    )


def test_v1_coworker_skill_binding_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(
        _schema(spec, "CoworkerSkillBinding")["required"]  # type: ignore[arg-type]
    )
    py_required = {
        name
        for name, f in CoworkerSkillBinding.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"CoworkerSkillBinding.required drift: yaml={yaml_required} "
        f"python={py_required}"
    )


def test_phase_4_safety_endpoints_are_present_in_yaml() -> None:
    """04 session lands these v1 read endpoints; admin keeps writes
    + CSV export for the duration of the compatibility window
    (NOT listed here — design §3 Phase 4 is GET-only on v1).
    """
    spec = _load_spec()
    paths = set(spec["paths"].keys())  # type: ignore[union-attr]
    expected = {
        "/api/v1/safety/rules",
        "/api/v1/safety/rules/{id}",
        "/api/v1/safety/rules/{id}/audit",
        "/api/v1/safety/checks",
        "/api/v1/safety/decisions",
        "/api/v1/safety/decisions/{id}",
    }
    missing = expected - paths
    assert not missing, f"yaml missing v1 safety endpoints: {sorted(missing)}"


def test_v1_safety_rule_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(_schema(spec, "SafetyRule")["required"])  # type: ignore[arg-type]
    py_required = {
        name for name, f in SafetyRule.model_fields.items() if f.is_required()
    }
    assert yaml_required == py_required, (
        f"SafetyRule.required drift: yaml={yaml_required} python={py_required}"
    )


def test_v1_safety_check_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(_schema(spec, "SafetyCheck")["required"])  # type: ignore[arg-type]
    py_required = {
        name for name, f in SafetyCheck.model_fields.items() if f.is_required()
    }
    assert yaml_required == py_required, (
        f"SafetyCheck.required drift: yaml={yaml_required} python={py_required}"
    )


def test_v1_safety_decision_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(_schema(spec, "SafetyDecision")["required"])  # type: ignore[arg-type]
    py_required = {
        name
        for name, f in SafetyDecision.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"SafetyDecision.required drift: yaml={yaml_required} "
        f"python={py_required}"
    )


def test_v1_safety_decision_page_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(
        _schema(spec, "SafetyDecisionPage")["required"]  # type: ignore[arg-type]
    )
    py_required = {
        name
        for name, f in SafetyDecisionPage.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"SafetyDecisionPage.required drift: yaml={yaml_required} "
        f"python={py_required}"
    )


def test_v1_safety_rule_audit_entry_required_matches_pydantic_model() -> None:
    spec = _load_spec()
    yaml_required = set(
        _schema(spec, "SafetyRuleAuditEntry")["required"]  # type: ignore[arg-type]
    )
    py_required = {
        name
        for name, f in SafetyRuleAuditEntry.model_fields.items()
        if f.is_required()
    }
    assert yaml_required == py_required, (
        f"SafetyRuleAuditEntry.required drift: yaml={yaml_required} "
        f"python={py_required}"
    )


def test_v1_safety_stage_enum_matches_safety_types_stage() -> None:
    """The wire-side SafetyStage Literal anchors against the engine
    ``Stage`` enum — drift here would let the API accept a stage
    string the engine has never heard of (silent no-op rule).
    """
    from rolemesh.safety.types import Stage

    spec = _load_spec()
    yaml_enum = set(_schema(spec, "SafetyStage")["enum"])  # type: ignore[arg-type]
    code_values = {s.value for s in Stage}
    assert yaml_enum == code_values, (
        f"SafetyStage enum drift: yaml={yaml_enum} code={code_values}"
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


# ---------------------------------------------------------------------------
# WS frame schemas (PR23 — see contracts/openapi.yaml WsServerEvent /
# WsClientFrame and the matching Pydantic models in schemas_v1.py).
# ---------------------------------------------------------------------------


# Expected discriminator → schema name mapping for the server→client
# event surface. Adding a new event type means: update the yaml,
# update schemas_v1, AND extend this constant — the test fails on any
# of the three by themselves, so they have to land together. Anti-
# mirror: we hard-code the expected set rather than re-deriving from
# the yaml, because the derive-then-compare pattern would happily
# match an empty yaml against an empty Pydantic union.
_EXPECTED_SERVER_EVENTS: dict[str, str] = {
    "event.run.started": "WsServerEventRunStarted",
    "event.run.token": "WsServerEventRunToken",
    "event.run.completed": "WsServerEventRunCompleted",
    "event.run.error": "WsServerEventRunError",
    "event.run.progress": "WsServerEventRunProgress",
    "event.message.appended": "WsServerEventMessageAppended",
    # HITL tool approval (docs/12-hitl-approval-architecture.md §10 S4).
    "event.approval.requested": "WsServerEventApprovalRequested",
    "event.approval.resolved": "WsServerEventApprovalResolved",
    # Frontdesk v1.5 delegation child-chip lifecycle.
    "event.delegation.started": "WsServerEventDelegationStarted",
    "event.delegation.progress": "WsServerEventDelegationProgress",
    "event.delegation.tool_use": "WsServerEventDelegationToolUse",
    "event.delegation.completed": "WsServerEventDelegationCompleted",
}

_EXPECTED_CLIENT_FRAMES: dict[str, str] = {
    "request.run": "WsClientFrameRequestRun",
    "request.cancel": "WsClientFrameRequestCancel",
    "request.stop": "WsClientFrameRequestStop",
    # HITL tool approval decision (docs/12-hitl-approval-architecture.md §10 S4).
    "request.approval_decision": "WsClientFrameApprovalDecision",
}


def test_ws_server_event_discriminator_mapping_matches_expected() -> None:
    """The yaml's discriminator mapping must list every event the SPA
    can branch on. A missing key here means a future event was added
    to the union but the discriminator wasn't updated — openapi-
    typescript would happily generate the union but the SPA's
    pattern-match on ``event.type`` couldn't narrow it.
    """
    spec = _load_spec()
    schema = _schema(spec, "WsServerEvent")
    mapping = schema["discriminator"]["mapping"]  # type: ignore[index]
    keys = set(mapping.keys())
    assert keys == set(_EXPECTED_SERVER_EVENTS.keys()), (
        f"WsServerEvent discriminator drift: "
        f"yaml={sorted(keys)} expected={sorted(_EXPECTED_SERVER_EVENTS)}"
    )


def test_ws_client_frame_discriminator_mapping_matches_expected() -> None:
    spec = _load_spec()
    schema = _schema(spec, "WsClientFrame")
    mapping = schema["discriminator"]["mapping"]  # type: ignore[index]
    keys = set(mapping.keys())
    assert keys == set(_EXPECTED_CLIENT_FRAMES.keys()), (
        f"WsClientFrame discriminator drift: "
        f"yaml={sorted(keys)} expected={sorted(_EXPECTED_CLIENT_FRAMES)}"
    )


def test_ws_server_event_pydantic_models_match_yaml_required_fields() -> None:
    """For each WsServerEvent member, the yaml's `required` set must
    equal the Pydantic model's required-fields set. Catches the
    common drift mode where a frontend dev adds an optional field to
    the yaml without updating Pydantic (or vice versa).
    """
    import webui.schemas_v1 as v1_schemas

    spec = _load_spec()
    for event_name, schema_name in _EXPECTED_SERVER_EVENTS.items():
        yaml_schema = _schema(spec, schema_name)
        yaml_required = set(yaml_schema.get("required") or [])  # type: ignore[arg-type]
        model = getattr(v1_schemas, schema_name)
        py_required = {
            name for name, f in model.model_fields.items() if f.is_required()
        }
        assert yaml_required == py_required, (
            f"{schema_name} required drift "
            f"(event {event_name!r}): yaml={sorted(yaml_required)} "
            f"python={sorted(py_required)}"
        )


def test_ws_client_frame_pydantic_models_match_yaml_required_fields() -> None:
    import webui.schemas_v1 as v1_schemas

    spec = _load_spec()
    for frame_name, schema_name in _EXPECTED_CLIENT_FRAMES.items():
        yaml_schema = _schema(spec, schema_name)
        yaml_required = set(yaml_schema.get("required") or [])  # type: ignore[arg-type]
        model = getattr(v1_schemas, schema_name)
        py_required = {
            name for name, f in model.model_fields.items() if f.is_required()
        }
        assert yaml_required == py_required, (
            f"{schema_name} required drift "
            f"(frame {frame_name!r}): yaml={sorted(yaml_required)} "
            f"python={sorted(py_required)}"
        )


def test_ws_server_event_pydantic_round_trips_each_member() -> None:
    """For each event member, build a minimal valid instance and
    serialize it, then re-parse via the discriminated union. Round-
    tripping pins that the Pydantic discriminator routes on `type`
    correctly — a refactor that renames the `type` field on any
    member silently breaks deserialization without this test.
    """
    from typing import Annotated

    from pydantic import Field as PField
    from pydantic import TypeAdapter

    from webui.schemas_v1 import (
        WsServerEventRunCompleted,
        WsServerEventRunError,
        WsServerEventRunStarted,
        WsServerEventRunToken,
    )

    union = Annotated[
        WsServerEventRunStarted
        | WsServerEventRunToken
        | WsServerEventRunCompleted
        | WsServerEventRunError,
        PField(discriminator="type"),
    ]
    adapter = TypeAdapter(union)
    samples = [
        WsServerEventRunStarted(
            type="event.run.started", run_id="r", idempotent=False,
        ),
        WsServerEventRunToken(
            type="event.run.token", run_id="r", delta="hi",
        ),
        WsServerEventRunCompleted(
            type="event.run.completed", run_id="r",
        ),
        WsServerEventRunError(
            type="event.run.error", code="X", message="m",
        ),
    ]
    for sample in samples:
        parsed = adapter.validate_python(sample.model_dump())
        assert type(parsed) is type(sample), (
            f"discriminator mis-routed: {type(sample).__name__} → "
            f"{type(parsed).__name__}"
        )
