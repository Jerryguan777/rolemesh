"""Pydantic models for the ``/api/v1`` surface.

Kept separate from ``webui.schemas`` (which serves the legacy
``/api/admin`` surface) so the two contracts evolve independently.

The shapes here MUST stay in sync with ``contracts/openapi.yaml``. The
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

from pydantic import BaseModel, ConfigDict, Field, field_validator

BackendName = Literal["claude", "pi"]
ModelProvider = Literal["anthropic", "bedrock", "openai", "google"]
ModelFamily = Literal["claude", "gpt", "gemini", "llama"]
CoworkerStatus = Literal["active", "paused", "disabled"]
# feat/roles PR3: per-resource visibility. 'private' = creator + managers
# only; 'shared' = whole tenant.
Visibility = Literal["private", "shared"]
AuthMode = Literal["external", "oidc", "builtin", "bootstrap"]
# Includes ``platform_admin`` (the platform-plane superset role): a seeded
# platform_admin authenticates and hits ``/me`` like anyone else, so the wire
# role must be able to represent it.
UserRole = Literal["platform_admin", "owner", "admin", "member"]
Plane = Literal["tenant", "platform"]
# Platform tenant lifecycle state (mirrors the tenants.status CHECK).
TenantStatus = Literal["active", "suspended"]


class ErrorResponse(BaseModel):
    """Design §13 — uniform error envelope.

    The shape is anchored against ``contracts/openapi.yaml`` by
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
    """Identity surfacing for the SPA's user-menu.

    ``capabilities`` is the caller's action set, populated server-side from
    the role->action matrix (``rolemesh.auth.permissions``). The SPA renders
    affordances from ``capabilities.includes(...)`` and never keeps its own
    copy of the matrix; the backend stays the single source of truth and the
    real enforcement still happens in ``require_action`` /
    ``require_manage_or_owner``. ``plane`` is ``"platform"`` only for the
    platform-superset role, else ``"tenant"``.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str
    tenant_id: str
    name: str | None = None
    email: str | None = None
    role: UserRole
    plane: Plane
    capabilities: list[str]


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
    max_concurrent: int = Field(ge=1)
    created_by_user_id: str | None = None
    # Always populated server-side (the DB column is NOT NULL), so this is
    # a REQUIRED response field — keep it without a default so the yaml
    # ``required`` list and the model agree (test_openapi_contract).
    visibility: Visibility
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


# ---------------------------------------------------------------------------
# Conversations / Messages / Runs (design §3 Phase 1)
# ---------------------------------------------------------------------------


MessageRole = Literal["user", "assistant"]
RunStatus = Literal[
    "running", "completed", "failed", "cancelled", "awaiting_reauth"
]


class Conversation(BaseModel):
    """Wire-side projection of a ``conversations`` row.

    The fields kept here are exactly what the SPA renders in its
    conversation list — ``user_id`` and ``last_agent_invocation``
    are intentionally omitted because Phase 1 surfaces neither in
    the UI and adding them later is contract-compatible (additive
    optional field).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    coworker_id: str
    channel_binding_id: str
    channel_chat_id: str
    name: str | None = None
    created_at: str


class ConversationCreate(BaseModel):
    """``POST /api/v1/coworkers/{id}/conversations`` body.

    Web-chat creation is server-driven: the handler auto-creates
    the coworker's ``web`` channel binding (if missing) and a fresh
    ``channel_chat_id`` so the SPA doesn't have to know about
    binding internals. ``name`` is purely a display label.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None


class Message(BaseModel):
    """Wire projection of a ``messages`` row.

    The ``role`` field is the wire-level projection of
    ``is_from_me`` / ``is_bot_message``; the persisted row carries
    more (``sender``, ``sender_name``, token counts) but those are
    not needed for the chat-history render path the SPA uses.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    role: MessageRole
    content: str
    timestamp: str
    run_id: str | None = None


# ---------------------------------------------------------------------------
# Models / Credentials (v1.1 §2.1, §8.1)
# ---------------------------------------------------------------------------


class Model(BaseModel):
    """Wire projection of a ``models`` row (platform catalog).

    Read-only — admin write surface is deferred to v2 per design §14.
    The SPA renders these in the read-only ``#/models`` page and
    filters by ``provider`` × ``model_family`` when constructing the
    coworker model picker.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    provider: ModelProvider
    model_id: str
    model_family: ModelFamily
    display_name: str
    is_active: bool
    created_at: str | None = None


CredentialMode = Literal["byok", "pool"]


class CredentialResponse(BaseModel):
    """Tenant credential metadata WITHOUT the secret.

    The plaintext API key never appears on this surface. No
    ``credential_data`` field is declared — even setting Pydantic's
    ``model_dump(exclude=...)`` would still leave the path open for
    a future refactor to start serialising it. The defence here is
    structural: a curious developer cannot ask the wire type for the
    field because it does not exist.

    ``mode`` tells the SPA which key the provider resolves to:
    ``'byok'`` (the tenant's own key) or ``'pool'`` (the platform
    credential pool). It is metadata, not a secret.
    """

    model_config = ConfigDict(extra="forbid")

    provider: ModelProvider
    mode: CredentialMode
    created_at: str
    updated_at: str


class PlatformCredentialResponse(BaseModel):
    """Platform pool credential metadata WITHOUT the secret.

    Same secret-omitting posture as :class:`CredentialResponse`;
    surfaced only to platform_admin via ``/api/v1/platform/credentials``.
    """

    model_config = ConfigDict(extra="forbid")

    provider: ModelProvider
    created_at: str
    updated_at: str


class PlatformTenantResponse(BaseModel):
    """A tenant as seen on the platform lifecycle plane.

    Distinct from the tenant-plane ``TenantResponse`` (owner self-service
    settings, :mod:`webui.schemas`): this carries ``status`` and is surfaced
    only to platform_admin via ``/api/v1/platform/tenants``. Kept separate so
    the lifecycle ``status`` field never leaks onto the owner-facing settings
    contract.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    slug: str | None = None
    plan: str | None = None
    max_concurrent_containers: int
    status: TenantStatus
    created_at: str


class PlatformTenantProvision(BaseModel):
    """``POST /api/v1/platform/tenants`` body — provision a new tenant.

    A provisioned tenant always starts ``active``; ``status`` is therefore
    not an accepted input (suspend/resume are separate verbs). ``slug`` is
    optional — omit it for an auto/unset slug, matching ``create_tenant``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=60)


class CredentialUpsert(BaseModel):
    """``PUT /api/v1/credentials/{provider}`` body.

    Today only ``api_key`` is recognised. Provider-specific extras
    (``api_base``, ``region``, ...) ride on top via ``extras`` so
    the schema does not have to grow a field per provider in lockstep
    with the credential proxy. ``extras`` is intentionally
    ``additionalProperties: true`` — the credential proxy reads
    whatever shape the provider needs.
    """

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=1, max_length=4096)
    extras: dict[str, object] | None = None


# ---------------------------------------------------------------------------
# MCP servers (design §2.1 / §3 Phase 2)
# ---------------------------------------------------------------------------


MCPType = Literal["sse", "http"]
MCPAuthMode = Literal["user", "service", "both"]


class MCPServer(BaseModel):
    """Wire projection of an ``mcp_servers`` row."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    name: str
    type: MCPType
    url: str
    auth_mode: MCPAuthMode
    extra_headers: dict[str, str] = Field(default_factory=dict)
    tool_reversibility: dict[str, bool] = Field(default_factory=dict)
    description: str | None = None
    created_at: str
    updated_at: str


class MCPServerCreate(BaseModel):
    """``POST /api/v1/mcp-servers`` body.

    ``auth_mode`` is required at the API even though the column has
    a ``'service'`` DB default — making it explicit avoids the
    "what mode did I create this in" question on the operator side.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    type: MCPType
    url: str = Field(min_length=1)
    auth_mode: MCPAuthMode
    extra_headers: dict[str, str] | None = None
    tool_reversibility: dict[str, bool] | None = None
    description: str | None = None


class MCPServerUpdate(BaseModel):
    """``PATCH /api/v1/mcp-servers/{id}`` body.

    Every field is optional; ``None`` is interpreted as "clear" for
    the nullable columns and "leave alone" for absent fields. The
    handler routes the difference via Pydantic's ``model_fields_set``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    type: MCPType | None = None
    url: str | None = Field(default=None, min_length=1)
    auth_mode: MCPAuthMode | None = None
    extra_headers: dict[str, str] | None = None
    tool_reversibility: dict[str, bool] | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# HITL tool-approval policies (docs/21-hitl-approval-plan.md §4 / §7 / §10 S5)
# ---------------------------------------------------------------------------


def _validate_condition_expr_field(value: dict[str, object]) -> dict[str, object]:
    """Pydantic-side adaptor: reject a structurally-invalid condition at the API.

    Delegates to the single pure validator in ``agent_runner.approval.policy``
    so the editor's notion of "valid" never drifts from the matcher's grammar.
    A malformed expression is a 422 here (clean operator feedback), not a policy
    that silently approval-gates everything at runtime (the fail-closed default).
    """
    # Imported lazily to keep the module import graph flat (the validator lives
    # in the zero-dep agent_runner matcher, shared with the container hook).
    from agent_runner.approval.policy import (
        ConditionValidationError,
        validate_condition_expr,
    )

    try:
        validate_condition_expr(value)
    except ConditionValidationError as exc:
        raise ValueError(str(exc)) from exc
    return value


class ApprovalPolicy(BaseModel):
    """Wire projection of an ``approval_policies`` row (§4.1)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    mcp_server_name: str
    tool_name: str
    condition_expr: dict[str, object]
    enabled: bool
    priority: int
    created_at: str
    updated_at: str


class ApprovalPolicyCreate(BaseModel):
    """``POST /api/v1/approval-policies`` body.

    ``tool_name`` is an exact MCP tool name or ``"*"`` for server-wide.
    ``condition_expr`` defaults to ``{"always": true}`` — the conservative
    gate (every matched call needs approval). A supplied expression is
    structurally validated (§7 grammar); a malformed one is a 422.
    """

    model_config = ConfigDict(extra="forbid")

    mcp_server_name: str = Field(min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=200)
    condition_expr: dict[str, object] = Field(default_factory=lambda: {"always": True})
    enabled: bool = True
    priority: int = 0

    _check_condition = field_validator("condition_expr")(
        _validate_condition_expr_field
    )


class ApprovalPolicyUpdate(BaseModel):
    """``PATCH /api/v1/approval-policies/{id}`` body — every field optional.

    Absent fields are left alone; present fields are written. ``condition_expr``
    cannot be cleared to ``null`` (the column is ``NOT NULL``) — omit it to
    leave it, or send a new valid expression to replace it.
    """

    model_config = ConfigDict(extra="forbid")

    mcp_server_name: str | None = Field(default=None, min_length=1, max_length=200)
    tool_name: str | None = Field(default=None, min_length=1, max_length=200)
    condition_expr: dict[str, object] | None = None
    enabled: bool | None = None
    priority: int | None = None

    _check_condition = field_validator("condition_expr")(
        _validate_condition_expr_field
    )


class ApprovalTriggeredBy(BaseModel):
    """Provenance of an approval that did not come from a business policy.

    Set by the safety pipeline when a check returns
    ``Verdict(action="require_approval")`` (spec §1.1, §3.10). Null on a
    normal business-policy approval. The SPA renders an amber "paused by a
    safety rule" banner on the chat card and a small shield on the inbox
    row when ``kind == "safety_rule"``; unknown kinds degrade to no banner.

    ``kind`` is an open tag so future provenances (e.g. ``scheduled_task``)
    extend the union without a breaking change; V1 only emits
    ``safety_rule``. ``stage`` is a ``SafetyStage`` value (typed ``str``
    here because this model is defined before the ``SafetyStage`` literal;
    the OpenAPI schema ``$ref``\\s the enum so the typed client narrows it).

    NOTE: no producer populates this yet — the safety→approval bridge that
    would set it is a separate, unbuilt backend effort. The field exists so
    the contract and SPA are ready; today it is always null end-to-end.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["safety_rule"]
    rule_id: str
    check_id: str
    stage: str


class ApprovalRequest(BaseModel):
    """Wire projection of an ``approval_requests`` row (§4.2).

    Two reads return these: the tenant-wide inbox read
    (``GET /api/v1/approval-requests``) yields only ``pending`` rows; the
    conversation sub-resource
    (``GET /api/v1/conversations/{id}/approval-requests``) yields every state so
    a reconnecting browser re-renders both pending and resolved cards inline in
    chat history. ``status`` distinguishes them.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    conversation_id: str | None = None
    mcp_server_name: str
    tool_name: str
    action_summary: str | None = None
    requested_at: str
    expires_at: str
    # §1.2: the decision input (raw params), the requesting coworker, and the
    # agent's nullable rationale — so a reconnecting browser re-renders the same
    # informative card the live WS push carried.
    params: dict[str, object] | None = None
    coworker_id: str | None = None
    rationale: str | None = None
    # pending|approved|rejected|expired|cancelled — 'pending' on the inbox read.
    status: str
    decided_at: str | None = None
    note: str | None = None
    # §1.1/§3.10 provenance: present (kind="safety_rule") when the approval was
    # raised by a safety check's require_approval verdict; null for business
    # policy approvals. No producer sets it yet (see ApprovalTriggeredBy).
    triggered_by: ApprovalTriggeredBy | None = None


# ---------------------------------------------------------------------------
# Coworker <-> MCP server bindings (design §2.1)
# ---------------------------------------------------------------------------


class CoworkerMCPBindingResponse(BaseModel):
    """One ``coworker_mcp_servers`` row, wire-side.

    Tri-state ``enabled_tools``: ``None`` means all tools enabled
    (the common case), ``[]`` means all disabled, and a non-empty
    list is a whitelist. The SPA distinguishes these states; the
    schema must preserve them.
    """

    model_config = ConfigDict(extra="forbid")

    coworker_id: str
    mcp_server_id: str
    enabled_tools: list[str] | None = None


class CoworkerMCPBindingCreate(BaseModel):
    """``POST /api/v1/coworkers/{id}/mcp-servers`` body.

    ``enabled_tools`` is optional; omitting it means "all tools
    enabled". A caller that wants the all-disabled state passes
    an explicit ``[]``.
    """

    model_config = ConfigDict(extra="forbid")

    mcp_server_id: str = Field(min_length=1)
    enabled_tools: list[str] | None = None


class CoworkerMCPBindingUpdate(BaseModel):
    """``PATCH /api/v1/coworkers/{id}/mcp-servers/{mcp_id}`` body.

    The only mutable field is ``enabled_tools``. ``None`` is a real
    value (= all enabled), so the handler distinguishes
    "field absent" from "field=None" via
    :pyattr:`pydantic.BaseModel.model_fields_set`.
    """

    model_config = ConfigDict(extra="forbid")

    enabled_tools: list[str] | None = None


# ---------------------------------------------------------------------------
# Skills (design §3 Phase 3 / docs/19-skills-architecture.md)
# ---------------------------------------------------------------------------


# Lowercase-kebab to match the DB CHECK and the runtime requirement
# that skill names be safe to use as filesystem directory names on the
# agent side.
_SKILL_NAME_PATTERN_V1 = r"^[a-z0-9][a-z0-9-]{0,63}$"

# Names the Claude runtime treats as built-in; surface as a Pydantic
# error so the wire boundary rejects them before they hit the DB.
_RESERVED_SKILL_NAMES_V1: frozenset[str] = frozenset({"anthropic", "claude"})


class SkillFile(BaseModel):
    """Wire projection of a ``skill_files`` row."""

    model_config = ConfigDict(extra="forbid")

    path: str
    content: str
    mime_type: str = "text/plain"
    updated_at: str = ""


class SkillFileUpsert(BaseModel):
    """``PUT /api/v1/skills/{id}/files/{path}`` body.

    Path lives on the URL, content lives in the body. ``mime_type``
    optional; defaults to ``text/plain`` to match the DB column
    default.
    """

    model_config = ConfigDict(extra="forbid")

    content: str
    mime_type: str = "text/plain"


# Embedded file shape inside ``SkillCreate.files`` — accepts either a
# bare content string (common case) or a richer ``SkillFileUpsert``
# payload. Kept distinct from ``SkillFile`` (the response shape) so
# the wire surface for creates stays narrow.
SkillCreateFile = str | SkillFileUpsert


class Skill(BaseModel):
    """Wire projection of a ``skills`` row plus its file map.

    ``created_by_user_id`` is the (renamed in 00b PR2 / 03b PR 2)
    audit FK; the wire field tracks the column name post-rename. The
    catalog is per-tenant — coworker association lives in
    ``coworker_skills``, queried via the relation endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    name: str
    enabled: bool
    frontmatter_common: dict[str, object] = Field(default_factory=dict)
    frontmatter_backend: dict[str, dict[str, object]] = Field(default_factory=dict)
    files: dict[str, SkillFile] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    created_by_user_id: str | None = None
    # Required response field (DB column is NOT NULL) — see Coworker.
    visibility: Visibility


class SkillSummary(BaseModel):
    """List-view projection — file map and frontmatter dropped to
    keep the list payload small.

    ``bound_coworker_count`` is the relation-layer projection the
    list page renders to spot orphaned vs heavily-shared skills.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    name: str
    description: str
    enabled: bool
    bound_coworker_count: int
    visibility: Visibility
    created_at: str
    updated_at: str


class SkillCreate(BaseModel):
    """``POST /api/v1/skills`` body.

    The ``files`` map must contain ``SKILL.md`` — the application
    invariant the handler enforces before the DB write. The
    frontmatter overrides win over keys parsed from the inline
    ``SKILL.md`` frontmatter when present.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_SKILL_NAME_PATTERN_V1)
    enabled: bool = True
    # ``files`` has no default — the handler always rejects payloads
    # without ``SKILL.md`` anyway, so making this a required wire
    # field surfaces the empty-payload case as 422 at the validator
    # rather than 400 from the manifest check downstream.
    files: dict[str, SkillCreateFile]
    frontmatter_common: dict[str, object] | None = None
    frontmatter_backend: dict[str, dict[str, object]] | None = None

    @field_validator("name")
    @classmethod
    def _check_not_reserved(cls, v: str) -> str:
        if v in _RESERVED_SKILL_NAMES_V1:
            raise ValueError(
                f"skill name {v!r} is reserved by the Claude runtime"
            )
        return v


class SkillUpdate(BaseModel):
    """``PATCH /api/v1/skills/{id}`` body.

    Two modes:
    * Metadata-only — set ``enabled`` or one of the frontmatter dicts.
    * Full file-set replacement — set ``files`` and the handler swaps
      the entire ``skill_files`` map atomically (matching the create
      path's semantics). Useful for the dialog's edit flow where the
      user has edited SKILL.md body + added / removed extras and the
      one-shot replace is simpler than diffing client-side.

    ``model_fields_set`` discriminates "field omitted" from "field
    explicitly set to None" so ``frontmatter_backend: {}`` clears the
    dict rather than silently leaving the existing one in place.

    ``name`` is intentionally NOT updatable: it's also a filesystem
    directory on the agent side and the catalog UNIQUE (tenant_id,
    name) constraint would make rename a multi-step migration. The
    edit dialog disables the input; if a caller sends ``name``
    anyway, the handler rejects unless it matches the existing value.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    enabled: bool | None = None
    files: dict[str, SkillCreateFile] | None = None
    frontmatter_common: dict[str, object] | None = None
    frontmatter_backend: dict[str, dict[str, object]] | None = None


class CoworkerSkillBinding(BaseModel):
    """One ``coworker_skills`` row, wire-side.

    Double-AND projection: this skill is projection-eligible only
    when ``enabled`` here is True AND the parent catalog skill's
    own ``enabled`` flag is True. The list endpoint returns the
    binding's flag; the catalog flag lives on the ``Skill`` payload.
    """

    model_config = ConfigDict(extra="forbid")

    coworker_id: str
    skill_id: str
    enabled: bool


class Run(BaseModel):
    """Wire projection of a ``runs`` row.

    Matches the lifecycle helper's snapshot shape (id /
    conversation_id / status / started_at / completed_at / usage /
    error). The SPA's reconnect path calls ``GET /api/v1/runs/{id}``
    to decide whether to re-subscribe — see design §4 "reconnect".
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    conversation_id: str
    status: RunStatus
    usage: dict[str, object] | None = None
    error: dict[str, object] | None = None
    started_at: str | None = None
    completed_at: str | None = None


# ---------------------------------------------------------------------------
# Safety (design §3 Phase 4 — GET-only on v1; admin keeps writes)
# ---------------------------------------------------------------------------


SafetyStage = Literal[
    "input_prompt",
    "pre_tool_call",
    "post_tool_result",
    "model_output",
    "pre_compaction",
    "egress_request",
]
SafetyVerdictAction = Literal[
    "allow",
    "block",
    "redact",
    "warn",
    "require_approval",
]
SafetyCheckCostClass = Literal["cheap", "slow"]
SafetyCheckActionModel = Literal["fixed", "config_routed", "aggregated"]
SafetyFindingSeverity = Literal["info", "low", "medium", "high", "critical"]
SafetyRuleAuditAction = Literal["created", "updated", "deleted"]


class SafetyRule(BaseModel):
    """Wire projection of a ``safety_rules`` row.

    Returned by the v1 read endpoints and the create/update write
    endpoints alike (``POST``/``PATCH /api/v1/safety/rules`` —
    ``SafetyRuleCreate`` / ``SafetyRuleUpdate`` are the request bodies).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    coworker_id: str | None = None
    stage: SafetyStage
    check_id: str
    config: dict[str, object] = Field(default_factory=dict)
    priority: int
    enabled: bool
    description: str
    created_at: str
    updated_at: str
    # Platform Safety Rules: ``source`` distinguishes tenant-owned rules
    # (editable on the admin surface) from platform-owned ones (read-only,
    # cross-tenant). ``tier`` is set only for platform rules. ``editable``
    # is a convenience flag the SPA uses to show/hide edit / "tighten"
    # controls without re-deriving from source.
    source: Literal["tenant", "platform"] = "tenant"
    tier: Literal["floor", "transparent_floor", "default"] | None = None
    editable: bool = True


class SafetyCheck(BaseModel):
    """One registered check, surfaced for the rule-editor UI.

    ``config_schema`` is the JSON Schema the check declared (None
    for legacy checks that accept arbitrary dicts). The UI uses it
    to render a config form without a second round-trip.

    ``action_model`` / ``natural_actions`` / ``supported_actions`` are
    descriptive action metadata (see the SafetyCheck Protocol). The
    rule editor uses them to show the default action and grey out
    actions that cannot be carried out for a given (check, stage).
    ``supported_actions`` serialises each stage's frozenset as a sorted
    list so dashboard caches stay byte-stable.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    stages: list[SafetyStage]
    cost_class: SafetyCheckCostClass
    supported_codes: list[str] = Field(default_factory=list)
    config_schema: dict[str, object] | None = None
    action_model: SafetyCheckActionModel
    natural_actions: dict[SafetyStage, SafetyVerdictAction] = Field(
        default_factory=dict
    )
    supported_actions: dict[SafetyStage, list[SafetyVerdictAction]] = Field(
        default_factory=dict
    )


class SafetyFinding(BaseModel):
    """One finding inside a ``SafetyDecision.findings`` array."""

    model_config = ConfigDict(extra="forbid")

    code: str
    severity: SafetyFindingSeverity
    message: str
    metadata: dict[str, object] | None = None


class SafetyDecision(BaseModel):
    """Wire projection of a ``safety_decisions`` row."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    coworker_id: str | None = None
    conversation_id: str | None = None
    job_id: str | None = None
    stage: SafetyStage
    verdict_action: SafetyVerdictAction
    triggered_rule_ids: list[str] = Field(default_factory=list)
    findings: list[SafetyFinding] = Field(default_factory=list)
    context_digest: str
    context_summary: str
    # 'platform' when any triggered rule is a platform-owned rule, else
    # 'tenant'. Lets the SPA / ops filter platform-rule hits at a glance.
    source: Literal["tenant", "platform"] = "tenant"
    created_at: str


class SafetyDecisionPage(BaseModel):
    """``GET /api/v1/safety/decisions`` response envelope.

    The standard offset/limit page shape shared across the v1 list
    surface: ``items`` plus ``total`` (so the SPA renders pagination
    without a second count call) and an echo of the effective
    ``limit``/``offset`` (so the caller doesn't have to track them).
    """

    model_config = ConfigDict(extra="forbid")

    items: list[SafetyDecision]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class SafetyRuleAuditEntry(BaseModel):
    """One row of ``safety_rules_audit`` for the rule-change timeline.

    Surfaced raw so operators can answer "when was this rule disabled
    and by whom". ``before_state`` / ``after_state`` are the full
    snapshots the trigger captured — small enough (no large blobs
    live on safety_rules) to ship over the wire.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    rule_id: str
    tenant_id: str
    action: SafetyRuleAuditAction
    actor_user_id: str | None = None
    before_state: dict[str, object] | None = None
    after_state: dict[str, object] | None = None
    created_at: str


# ---------------------------------------------------------------------------
# WebSocket frame models (PR23 — contracts/openapi.yaml WsServerEvent /
# WsClientFrame). Discriminated on the literal ``type`` field; Pydantic
# narrows the union member from that one tag.
#
# These Pydantic models are NOT used to serialize outbound frames on
# the hot path (``ws_stream.py`` still emits plain dicts because the
# control flow is simpler that way). They exist for:
#   1. Contract drift validation — the test suite parses the yaml,
#      finds these models, and asserts the field shape matches.
#   2. Documentation — the wire shape lives in one importable place
#      that an unfamiliar contributor can grep for.
#   3. Future hot-path use — when ws_stream.py grows enough that
#      "construct frame as Pydantic, .model_dump_json()" beats the
#      current dict-literal approach, the models are ready.
# ---------------------------------------------------------------------------


class WsServerEventRunStarted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["event.run.started"]
    run_id: str
    idempotent: bool


class WsServerEventRunToken(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["event.run.token"]
    run_id: str
    delta: str


class WsServerEventRunCompleted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["event.run.completed"]
    run_id: str


class WsServerEventRunError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["event.run.error"]
    run_id: str | None = None
    code: str
    message: str
    details: dict[str, object] | None = None


class WsServerEventRunProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["event.run.progress"]
    run_id: str
    # Mirrors the orchestrator's progress status set: running,
    # tool_use, queued, container_starting. Kept as a free string
    # rather than an enum here so a new orch progress kind doesn't
    # immediately bounce off pydantic validation in production while
    # the FE is rolled out separately. The SPA's renderer falls back
    # to a generic label for unknown values.
    status: str
    tool: str | None = None
    input_preview: str | None = None


class WsServerEventMessageAppended(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["event.message.appended"]
    content: str
    source: Literal["scheduled_task"]
    timestamp: str


class WsServerEventApprovalRequested(BaseModel):
    """HITL approval card push (docs/21-hitl-approval-plan.md §10 S4).

    Out-of-band, like ``event.message.appended``: an agent's blocked MCP tool
    call needs a human ✅/❌, so the orchestrator pushes this independent of any
    ``run_id``. ``request_id`` is what the SPA echoes back in a
    ``request.approval_decision`` frame; the browser never sees or supplies the
    approver identity (resolved server-side from the WS ticket — IDOR guard).
    """

    model_config = ConfigDict(extra="forbid")
    type: Literal["event.approval.requested"]
    request_id: str
    # Decision-relevant additions (§1.1). All additive/optional so existing
    # clients ignore them; the SPA renders an informative card from this push
    # alone, before any REST read. ``params`` is the raw tool input — the
    # decision input. ``rationale``/``conversation_id`` are nullable.
    mcp_server_name: str | None = None
    tool_name: str | None = None
    params: dict[str, object] | None = None
    coworker_id: str | None = None
    conversation_id: str | None = None
    requested_at: str | None = None
    rationale: str | None = None
    action_summary: str | None = None
    expires_at: str | None = None
    # §1.1/§3.10 provenance for a safety-triggered approval; null for a
    # business-policy approval. No producer sets it yet (see
    # ApprovalTriggeredBy) — the SPA renders nothing when absent.
    triggered_by: ApprovalTriggeredBy | None = None


class WsServerEventApprovalResolved(BaseModel):
    """Hard-channel result: the card's deterministic terminal state.

    ``outcome`` is the orchestrator's authoritative transition, set with no LLM
    in the loop (approve via the decision funnel; reject/expire via the
    coordinator's hard hook). The SPA edits the card in place.
    """

    model_config = ConfigDict(extra="forbid")
    type: Literal["event.approval.resolved"]
    request_id: str
    # ``cancelled`` = the coworker's container withdrew the call (Stop / hook
    # exception); distinct from ``expired`` (no decision in time). §1.5.
    outcome: Literal["approved", "rejected", "expired", "cancelled"]


# Tagged union over ``type``. Pydantic v2's Field discriminator picks
# the right member based on the literal value, giving validation
# errors that name the offending field (rather than the generic
# "doesn't match any variant" you'd get without it).
WsServerEventModel = (
    WsServerEventRunStarted
    | WsServerEventRunToken
    | WsServerEventRunCompleted
    | WsServerEventRunError
    | WsServerEventRunProgress
    | WsServerEventMessageAppended
    | WsServerEventApprovalRequested
    | WsServerEventApprovalResolved
)


class WsClientFrameRequestStop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["request.stop"]
    # Advisory only — see openapi description for the IDOR rationale.
    run_id: str | None = None


class WsClientFrameRequestRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["request.run"]
    input: str
    idempotency_key: str


class WsClientFrameRequestCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["request.cancel"]
    run_id: str


class WsClientFrameApprovalDecision(BaseModel):
    """A human ✅/❌ on a pending HITL approval (docs §10 S4).

    Carries only ``request_id`` + verb (+ optional note). The approver identity
    is NOT in the frame — the WS handler stamps the authenticated
    ``user_id``/``tenant_id`` from the verified ticket when it relays to the
    orchestrator (same IDOR posture as ``request.run`` / ``request.stop``).
    """

    model_config = ConfigDict(extra="forbid")
    type: Literal["request.approval_decision"]
    request_id: str
    decision: Literal["approve", "reject"]
    note: str | None = None


WsClientFrameModel = (
    WsClientFrameRequestRun
    | WsClientFrameRequestCancel
    | WsClientFrameRequestStop
    | WsClientFrameApprovalDecision
)


# ---------------------------------------------------------------------------
# Scheduled tasks (PR24). Read-only surface over the existing
# ``scheduled_tasks`` table — the orchestrator owns creation /
# mutation (cron-style triggers fire from inside the agent process);
# the UI just needs to render "what's scheduled".
# ---------------------------------------------------------------------------


ScheduleType = Literal["cron", "interval", "once"]
ScheduleStatus = Literal["active", "paused", "completed", "cancelled"]
ScheduleContextMode = Literal["group", "isolated"]


class ScheduledTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    coworker_id: str
    conversation_id: str | None = None
    prompt: str
    schedule_type: ScheduleType
    schedule_value: str
    context_mode: ScheduleContextMode
    next_run: str | None = None
    last_run: str | None = None
    last_result: str | None = None
    status: ScheduleStatus
    created_at: str


# ---------------------------------------------------------------------------
# Channel bindings (PR24). Migrated from /api/admin/agents/{id}/bindings
# to /api/v1/coworkers/{id}/bindings — the underlying DB table +
# helpers already exist in rolemesh.db.chat.
# ---------------------------------------------------------------------------


ChannelTypeName = Literal["slack", "telegram", "web"]


class ChannelBinding(BaseModel):
    """Wire projection of a ``channel_bindings`` row.

    ``credentials`` is intentionally NOT exposed on the GET path —
    they're write-only from the wire (PUT/POST/PATCH only). The
    response shape mirrors what's safe to ship back: identity +
    display name + status, never the tokens.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    coworker_id: str
    tenant_id: str
    channel_type: ChannelTypeName
    bot_display_name: str | None = None
    status: str
    created_at: str | None = None


class ChannelBindingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_type: ChannelTypeName
    credentials: dict[str, str]
    bot_display_name: str | None = None


class ChannelLinkToken(BaseModel):
    """Issued by ``POST /api/v1/me/channel-links/telegram``.

    The caller (Web SPA) hands the user EITHER ``deep_link`` (which
    auto-fills ``/start <token>`` when opened in Telegram) OR the
    raw ``token`` for copy-paste. ``deep_link`` is ``null`` when the
    tenant has no Telegram bot binding (in which case the POST
    actually 4xxs); a non-null value carries the bot's @username
    persisted by the gateway on connect.
    """

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=22, description="One-shot link token; URL-safe ASCII.")
    expires_at: str = Field(description="ISO 8601 UTC; ~10 min from issuance.")
    deep_link: str | None = Field(
        default=None,
        description="https://t.me/<bot>?start=<token> if a bot @handle is known.",
    )


class ChannelLinkIdentity(BaseModel):
    """Wire projection of one ``user_channel_identities`` row."""

    model_config = ConfigDict(extra="forbid")

    id: str
    platform: Literal["telegram"]
    channel_id: str = Field(description="Platform-native sender id; opaque.")
    created_at: str | None = None


class ChannelBindingUpdate(BaseModel):
    """Partial update. Omit any field to leave it unchanged.

    ``credentials`` semantics: when present (even as ``{}``) it
    REPLACES the entire credentials map. Merge-by-key would let a
    typo'd key silently coexist with a stale value.
    """

    model_config = ConfigDict(extra="forbid")

    credentials: dict[str, str] | None = None
    bot_display_name: str | None = None


# ---------------------------------------------------------------------------
# Platform model-catalog writes. The /api/v1/models GET path stays
# tenant-readable; the /api/v1/platform/models writes require the
# platform-only model.manage capability (platform_admin).
# ---------------------------------------------------------------------------


class ModelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ModelProvider
    model_id: str = Field(min_length=1, max_length=200)
    model_family: ModelFamily
    display_name: str = Field(min_length=1, max_length=200)
    is_active: bool = True


class ModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Pagination envelopes (offset/limit). One named class per resource keeps the
# generated TS type names clean; all share the {items, total, limit, offset}
# shape established by SafetyDecisionPage. See webui.v1._pagination.
# ---------------------------------------------------------------------------


class SafetyRulePage(BaseModel):
    """Offset/limit page of safety rules."""

    model_config = ConfigDict(extra="forbid")

    items: list[SafetyRule]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class ScheduledTaskPage(BaseModel):
    """Offset/limit page of scheduled tasks."""

    model_config = ConfigDict(extra="forbid")

    items: list[ScheduledTask]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class ApprovalPolicyPage(BaseModel):
    """Offset/limit page of approval policies."""

    model_config = ConfigDict(extra="forbid")

    items: list[ApprovalPolicy]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class ApprovalRequestPage(BaseModel):
    """Offset/limit page of approval requests."""

    model_config = ConfigDict(extra="forbid")

    items: list[ApprovalRequest]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class CoworkerPage(BaseModel):
    """Offset/limit page of coworkers."""

    model_config = ConfigDict(extra="forbid")

    items: list[Coworker]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class SkillSummaryPage(BaseModel):
    """Offset/limit page of skill summaries."""

    model_config = ConfigDict(extra="forbid")

    items: list[SkillSummary]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class MCPServerPage(BaseModel):
    """Offset/limit page of MCP servers."""

    model_config = ConfigDict(extra="forbid")

    items: list[MCPServer]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class ConversationPage(BaseModel):
    """Offset/limit page of conversations."""

    model_config = ConfigDict(extra="forbid")

    items: list[Conversation]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class SafetyRuleAuditPage(BaseModel):
    """Offset/limit page of safety rule audit entries."""

    model_config = ConfigDict(extra="forbid")

    items: list[SafetyRuleAuditEntry]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Platform safety rules (cross-tenant, platform_admin only)
# ---------------------------------------------------------------------------


PlatformSafetyTier = Literal["floor", "transparent_floor", "default"]


class PlatformSafetyRule(BaseModel):
    """Wire projection of a ``platform_safety_rules`` row (all tiers).

    The platform-admin surface, unlike the tenant read, exposes ALL tiers
    (floor included) and the ``is_seeded`` flag. ``is_seeded`` rules are the
    shipped factory defaults: editable / disablable but never hard-deletable
    (a delete would be undone by the next build-time seed).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tier: PlatformSafetyTier
    stage: SafetyStage
    check_id: str
    config: dict[str, object] = Field(default_factory=dict)
    priority: int
    enabled: bool
    description: str
    is_seeded: bool
    created_at: str
    updated_at: str


class PlatformSafetyRuleCreate(BaseModel):
    """``POST /api/v1/platform/safety/rules`` body.

    ``tier`` / ``stage`` / ``check_id`` form the rule identity. The handler
    additionally validates ``check_id`` against the safety check registry and
    that the check supports ``stage`` (400 ``INVALID_RULE`` otherwise).
    """

    model_config = ConfigDict(extra="forbid")

    tier: PlatformSafetyTier
    stage: SafetyStage
    check_id: str = Field(min_length=1, max_length=128)
    config: dict[str, object] = Field(default_factory=dict)
    priority: int = Field(1000, ge=-1000, le=1000)
    description: str = Field("", max_length=500)


class PlatformSafetyRuleUpdate(BaseModel):
    """``PATCH /api/v1/platform/safety/rules/{id}`` body.

    Only the mutable fields — ``tier`` / ``stage`` / ``check_id`` are the
    immutable identity and cannot be patched (create a new rule instead).
    Every field is optional; omitted fields are left untouched.
    """

    model_config = ConfigDict(extra="forbid")

    config: dict[str, object] | None = None
    priority: int | None = Field(None, ge=-1000, le=1000)
    description: str | None = Field(None, max_length=500)
    enabled: bool | None = None


class MessagePage(BaseModel):
    """Cursor page of conversation messages.

    Messages use cursor pagination, NOT offset/limit like the other
    collections: chat history is append-only and read newest-first
    ("load older"), so offset paging would shift or duplicate rows as
    new messages arrive mid-scroll. The server seeks on ``(timestamp,
    id)``. ``items`` are returned oldest-first (natural display order);
    when ``has_more`` is true, ``next_cursor`` is passed back as the
    ``before`` query param to fetch the next older page.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[Message]
    has_more: bool
    next_cursor: str | None = None
