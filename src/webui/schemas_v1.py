"""Pydantic models for the ``/api/v1`` surface.

Kept separate from ``webui.schemas`` (which serves the legacy
``/api/admin`` surface) so the two contracts evolve independently.

The shapes here MUST stay in sync with ``web/openapi.yaml``. The
freshness CI (``tests/test_openapi_codegen_freshness.py``) catches
yaml/ts drift; ``tests/test_openapi_contract.py`` catches drift
between this Python contract and the yaml.

A new endpoint goes here only after the yaml has been updated and
``npm run openapi:gen`` has regenerated ``types.ts`` — committing
the generated file is part of the same change. Reversing that order
lets a wrong shape land first; the contract test catches the drift
later but the ts client is already broken in dev.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BackendName = Literal["claude", "pi"]
ModelProvider = Literal["anthropic", "bedrock", "openai", "google"]
ModelFamily = Literal["claude", "gpt", "gemini", "llama"]
AgentRole = Literal["super_agent", "agent"]
CoworkerStatus = Literal["active", "paused", "disabled"]
AuthMode = Literal["external", "oidc", "builtin", "bootstrap"]
UserRole = Literal["owner", "admin", "member"]


class ErrorResponse(BaseModel):
    """Design §13 — uniform error envelope.

    The shape is anchored against ``web/openapi.yaml`` by
    :func:`tests.test_openapi_contract.test_error_response_shape_matches_pydantic_model`.
    """

    code: str
    message: str
    details: dict[str, object] | None = None


class Backend(BaseModel):
    """Public projection of ``BackendCapability`` (design §2.3).

    ``supported_model_families == None`` encodes "any family the
    provider offers"; consumers must accept ``null`` here as a
    valid value distinct from an empty list.
    """

    # OpenAPI codegen rejects extra fields by default; mirror that
    # here so a stray `**kwargs` slip in the handler trips a 500
    # locally instead of leaking the field to the client.
    model_config = ConfigDict(extra="forbid")

    name: BackendName
    description: str
    supported_providers: list[ModelProvider] = Field(min_length=1)
    supported_model_families: list[ModelFamily] | None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class AuthConfig(BaseModel):
    """Public boot-time hint the SPA uses to decide how to log in.

    ``mode == "bootstrap"`` is what the dev fast-path advertises so
    the SPA knows it can drop the ``ADMIN_BOOTSTRAP_TOKEN`` it
    received out-of-band into ``Authorization`` without an IdP
    round-trip. ``login_url`` is non-null only when the SPA needs to
    redirect (OIDC PKCE).
    """

    model_config = ConfigDict(extra="forbid")

    mode: AuthMode
    login_url: str | None = None


class WsTicketRequest(BaseModel):
    """Body of ``POST /api/v1/auth/ws-ticket``.

    ``conversation_id`` is required. Design §4 binds the ticket to
    one conversation so the WS handshake can compare the path
    ``conversation_id`` against the ticket payload without an extra
    DB round-trip; binding the ticket too loosely would let a
    cross-tenant browser session attach to any conversation it
    knows the UUID of after a single legitimate ticket request.
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, description="UUID of the conversation the ticket authorises.")


class WsTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket: str = Field(description="Short-lived JWT (exp <= 60s).")
    expires_in_s: int = Field(ge=1, le=60)


class Me(BaseModel):
    """Identity surfacing for the SPA's user-menu."""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    tenant_id: str
    name: str | None = None
    email: str | None = None
    role: UserRole


# ---------------------------------------------------------------------------
# Coworkers
# ---------------------------------------------------------------------------


class Coworker(BaseModel):
    """Wire-side projection of the ``coworkers`` row.

    Kept narrow on purpose — Phase 1 deliberately leaves the admin
    sub-resources (``tools`` JSONB, ``permissions``) off the
    ``/api/v1`` surface so we can drop them without a contract
    bump (see design §9.3 three-stage retirement). Add them back
    only when an admin UI need is explicit.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    name: str
    folder: str
    agent_backend: BackendName
    model_id: str | None = None
    system_prompt: str | None = None
    status: CoworkerStatus
    agent_role: AgentRole
    max_concurrent: int = Field(ge=1)
    created_by_user_id: str | None = None
    created_at: str


class CoworkerCreate(BaseModel):
    """``POST /api/v1/coworkers`` body.

    ``folder`` must match the regex anchored in the yaml — kept on
    both sides because the typed client doesn't run the regex client-
    side; missing it server-side would let a creative folder name
    smuggle in (e.g. ``..``) and break the container mount path.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    folder: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
    )
    agent_backend: BackendName
    model_id: str | None = None
    system_prompt: str | None = None
    max_concurrent: int = Field(default=2, ge=1, le=20)
    agent_role: AgentRole = "agent"


class CoworkerUpdate(BaseModel):
    """``PATCH /api/v1/coworkers/{id}`` body.

    Every field is optional; the handler treats absence (not the
    value ``None``) as "leave alone". ``model_id`` accepts ``None``
    to *clear* the association but the handler currently rejects
    that — see :mod:`webui.v1.coworkers` for the policy.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    system_prompt: str | None = None
    model_id: str | None = None
    status: CoworkerStatus | None = None
    max_concurrent: int | None = Field(default=None, ge=1, le=20)
