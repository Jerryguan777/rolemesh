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
AgentRole = Literal["super_agent", "agent"]
CoworkerStatus = Literal["active", "paused", "disabled"]
AuthMode = Literal["external", "oidc", "builtin", "bootstrap"]
UserRole = Literal["owner", "admin", "member"]


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
    requires_trigger: bool = True
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


class CredentialResponse(BaseModel):
    """Tenant credential metadata WITHOUT the secret.

    The plaintext API key never appears on this surface. No
    ``credential_data`` field is declared — even setting Pydantic's
    ``model_dump(exclude=...)`` would still leave the path open for
    a future refactor to start serialising it. The defence here is
    structural: a curious developer cannot ask the wire type for the
    field because it does not exist.
    """

    model_config = ConfigDict(extra="forbid")

    provider: ModelProvider
    created_at: str
    updated_at: str


class CredentialUpsert(BaseModel):
    """``PUT /api/v1/tenant/credentials/{provider}`` body.

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
    credential_ref: str | None = None
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
    credential_ref: str | None = None
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
    credential_ref: str | None = None
    extra_headers: dict[str, str] | None = None
    tool_reversibility: dict[str, bool] | None = None
    description: str | None = None


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
    to decide whether to re-subscribe — see design §4 "重连".
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
# Approvals (design §3 Phase 3 / §11 INV-4 + INV-7)
# ---------------------------------------------------------------------------


ApprovalPostExecMode = Literal["report"]
ApprovalRequestStatus = Literal[
    "pending",
    "approved",
    "rejected",
    "expired",
    "cancelled",
    "skipped",
    "executing",
    "executed",
    "execution_failed",
    "execution_stale",
]
ApprovalRequestSource = Literal[
    "proposal", "auto_intercept", "safety_require_approval"
]
ApprovalListScope = Literal["mine", "all"]
ApprovalDecideAction = Literal["approve", "reject"]


class ApprovalPolicy(BaseModel):
    """Wire projection of an ``approval_policies`` row."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    coworker_id: str | None = None
    mcp_server_name: str
    tool_name: str
    condition_expr: dict[str, object]
    approver_user_ids: list[str] = Field(default_factory=list)
    notify_conversation_id: str | None = None
    auto_expire_minutes: int = Field(ge=1, le=10080)
    post_exec_mode: ApprovalPostExecMode
    enabled: bool
    priority: int = Field(ge=-1000, le=1000)
    created_at: str
    updated_at: str


class ApprovalPolicyCreate(BaseModel):
    """``POST /api/v1/approval-policies`` body."""

    model_config = ConfigDict(extra="forbid")

    mcp_server_name: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    condition_expr: dict[str, object]
    coworker_id: str | None = None
    approver_user_ids: list[str] = Field(default_factory=list)
    notify_conversation_id: str | None = None
    auto_expire_minutes: int = Field(default=60, ge=1, le=10080)
    post_exec_mode: ApprovalPostExecMode = "report"
    enabled: bool = True
    priority: int = Field(default=0, ge=-1000, le=1000)


class ApprovalPolicyUpdate(BaseModel):
    """``PATCH /api/v1/approval-policies/{id}`` body.

    Every field is optional; ``model_fields_set`` discriminates
    "leave alone" from "explicit clear". The DB helper accepts the
    same shape and treats explicit ``None`` for nullable columns as
    "clear".
    """

    model_config = ConfigDict(extra="forbid")

    mcp_server_name: str | None = Field(default=None, min_length=1)
    tool_name: str | None = Field(default=None, min_length=1)
    condition_expr: dict[str, object] | None = None
    approver_user_ids: list[str] | None = None
    notify_conversation_id: str | None = None
    auto_expire_minutes: int | None = Field(default=None, ge=1, le=10080)
    post_exec_mode: ApprovalPostExecMode | None = None
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=-1000, le=1000)


class ApprovalRequest(BaseModel):
    """Wire projection of an ``approval_requests`` row.

    ``policy_id`` is nullable because (a) the proposal default-mode
    path stores no policy, and (b) deleting a policy
    ``SET NULL``-cascades into pending requests so they survive
    a policy retraction (design §3 DELETE 语义).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    coworker_id: str
    conversation_id: str | None = None
    policy_id: str | None = None
    user_id: str
    job_id: str
    mcp_server_name: str
    actions: list[dict[str, object]] = Field(default_factory=list)
    action_hashes: list[str] = Field(default_factory=list)
    rationale: str | None = None
    source: ApprovalRequestSource
    status: ApprovalRequestStatus
    post_exec_mode: ApprovalPostExecMode
    resolved_approvers: list[str] = Field(default_factory=list)
    requested_at: str
    expires_at: str
    created_at: str
    updated_at: str


class ApprovalAuditEntry(BaseModel):
    """Wire projection of an ``approval_audit_log`` row."""

    model_config = ConfigDict(extra="forbid")

    id: str
    request_id: str
    action: str
    actor_user_id: str | None = None
    note: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: str


class ApprovalRequestDetail(ApprovalRequest):
    """``GET /api/v1/approvals/{id}`` body — request + inline audit_log."""

    audit_log: list[ApprovalAuditEntry] = Field(default_factory=list)


class ApprovalDecide(BaseModel):
    """``POST /api/v1/approvals/{id}/decide`` body.

    ``action`` is the HTTP wire enum (``approve``/``reject``).
    The handler translates it via INV-7's
    :func:`rolemesh.approval.enum_translate.http_action_to_outcome`;
    engine code never sees the wire string.
    """

    model_config = ConfigDict(extra="forbid")

    action: ApprovalDecideAction
    note: str | None = Field(default=None, max_length=1000)


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
SafetyFindingSeverity = Literal["info", "low", "medium", "high", "critical"]
SafetyRuleAuditAction = Literal["created", "updated", "deleted"]


class SafetyRule(BaseModel):
    """Wire projection of a ``safety_rules`` row.

    Read-only on the v1 surface: writes (create/update/delete) stay
    on ``/api/admin/safety/rules`` per design §3 Phase 4. The schema
    intentionally mirrors the admin response shape verbatim so a
    future switch to v1 writes is additive.
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


class SafetyCheck(BaseModel):
    """One registered check, surfaced for the rule-editor UI.

    ``config_schema`` is the JSON Schema the check declared (None
    for legacy checks that accept arbitrary dicts). The UI uses it
    to render a config form without a second round-trip.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    stages: list[SafetyStage]
    cost_class: SafetyCheckCostClass
    supported_codes: list[str] = Field(default_factory=list)
    config_schema: dict[str, object] | None = None


class SafetyFinding(BaseModel):
    """One finding inside a ``SafetyDecision.findings`` array."""

    model_config = ConfigDict(extra="forbid")

    code: str
    severity: SafetyFindingSeverity
    message: str
    metadata: dict[str, object] | None = None


class SafetyDecision(BaseModel):
    """Wire projection of a ``safety_decisions`` row.

    The list endpoint returns the same shape with ``approval_context``
    elided (always ``None``) so list payloads stay small. The detail
    endpoint surfaces ``approval_context`` for require_approval rows
    within the 24-hour retention window (cleared by the retention
    sweep — see ``cleanup_old_safety_approval_contexts``).
    """

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
    approval_context: dict[str, object] | None = None
    created_at: str


class SafetyDecisionPage(BaseModel):
    """``GET /api/v1/safety/decisions`` response envelope.

    Mirrors the admin shape ``{total, items}`` so the SPA renders
    pagination without a second count call. The two-field envelope
    is intentional even when ``total == len(items)`` — keeps the
    list and count concerns coupled in one round-trip.
    """

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    items: list[SafetyDecision] = Field(default_factory=list)


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
