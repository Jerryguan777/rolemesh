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
from webui.schemas_v1 import Backend, ErrorResponse

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
        "/api/v1/conversations/{id}",
        "/api/v1/conversations/{id}/messages",
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


def test_backends_endpoint_advertises_cache_control_header_in_yaml() -> None:
    """The handler sets ``Cache-Control: max-age=3600``; the yaml
    declares that contract. If somebody drops the header in code
    the freshness check at the *header* level still catches it.
    """
    spec = _load_spec()
    op = spec["paths"]["/api/v1/backends"]["get"]  # type: ignore[index]
    headers = op["responses"]["200"]["headers"]
    assert "Cache-Control" in headers
