"""Pydantic request/response models for the Admin API."""

from __future__ import annotations

from typing import get_args

from pydantic import BaseModel, Field, field_validator

from webui.schemas_v1 import SafetyStage

# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------


class TenantResponse(BaseModel):
    id: str
    name: str
    slug: str | None = None
    plan: str | None = None
    max_concurrent_containers: int
    created_at: str


class TenantUpdate(BaseModel):
    name: str | None = None
    max_concurrent_containers: int | None = Field(None, ge=1, le=100)


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


class UserResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    email: str | None = None
    role: str
    channel_ids: dict[str, str]
    created_at: str


class UserCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    email: str | None = None
    role: str = Field("member", pattern=r"^(owner|admin|member)$")
    channel_ids: dict[str, str] = Field(default_factory=dict)


class UserUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    email: str | None = None  # send "" to clear; omit or null to leave unchanged
    role: str | None = Field(None, pattern=r"^(owner|admin|member)$")


class AgentSummary(BaseModel):
    id: str
    name: str
    folder: str
    status: str


class UserDetailResponse(UserResponse):
    assigned_agents: list[AgentSummary] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent (Coworker)
# ---------------------------------------------------------------------------


class AgentResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    folder: str
    agent_backend: str
    system_prompt: str | None = None
    tools: list[dict[str, object]] = Field(default_factory=list)
    max_concurrent: int
    status: str
    permissions: dict[str, object] = Field(default_factory=dict)
    created_at: str


class BindingResponse(BaseModel):
    id: str
    coworker_id: str
    tenant_id: str
    channel_type: str
    credentials: dict[str, str]
    bot_display_name: str | None = None
    status: str
    created_at: str


class ConversationResponse(BaseModel):
    id: str
    tenant_id: str
    coworker_id: str
    channel_binding_id: str
    channel_chat_id: str
    name: str | None = None
    created_at: str


class AgentDetailResponse(AgentResponse):
    bindings: list[BindingResponse] = Field(default_factory=list)
    conversations: list[ConversationResponse] = Field(default_factory=list)


class AgentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    folder: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    agent_backend: str = "claude"
    system_prompt: str | None = None
    tools: list[dict[str, object]] = Field(default_factory=list)
    max_concurrent: int = Field(2, ge=1, le=20)
    permissions: dict[str, object] | None = None


class AgentUpdate(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    tools: list[dict[str, object]] | None = None
    max_concurrent: int | None = Field(None, ge=1, le=20)
    status: str | None = Field(None, pattern=r"^(active|paused|disabled)$")
    permissions: dict[str, object] | None = None


# ---------------------------------------------------------------------------
# Channel Binding
# ---------------------------------------------------------------------------


class BindingCreate(BaseModel):
    channel_type: str = Field(..., pattern=r"^(telegram|slack|web)$")
    credentials: dict[str, str] = Field(default_factory=dict)
    bot_display_name: str | None = None


class BindingUpdate(BaseModel):
    credentials: dict[str, str] | None = None
    bot_display_name: str | None = None
    status: str | None = Field(None, pattern=r"^(active|paused|disabled)$")


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------


class ConversationCreate(BaseModel):
    channel_binding_id: str
    channel_chat_id: str
    name: str | None = None


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


class TaskResponse(BaseModel):
    id: str
    tenant_id: str
    coworker_id: str
    prompt: str
    schedule_type: str
    schedule_value: str
    context_mode: str
    conversation_id: str | None = None
    next_run: str | None = None
    last_run: str | None = None
    status: str
    created_at: str


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


class AssignRequest(BaseModel):
    user_id: str


# ---------------------------------------------------------------------------
# Safety rules
# ---------------------------------------------------------------------------


# V1 stages exposed through REST. Rule POST accepts any of these, but
# the server additionally checks the stage is within the selected
# check's ``stages`` set before writing to the DB.
# Derived from the canonical v1 ``SafetyStage`` literal (itself pinned to the
# engine ``Stage`` enum by test_v1_safety_stage_enum_matches_safety_types_stage)
# so this admin-write validator can never drift out of sync with the stage set.
# PR #50 added EGRESS_REQUEST to the engine, the v1 read schema, and every
# check's stages — but not this hardcoded regex, so editing an egress rule
# (stage=egress_request) was rejected with a 422. Deriving it closes that gap.
_SAFETY_STAGE_PATTERN = "^(" + "|".join(get_args(SafetyStage)) + ")$"


class SafetyRuleResponse(BaseModel):
    id: str
    tenant_id: str
    coworker_id: str | None = None
    stage: str
    check_id: str
    config: dict[str, object] = Field(default_factory=dict)
    priority: int
    enabled: bool
    description: str
    created_at: str
    updated_at: str


class SafetyRuleCreate(BaseModel):
    stage: str = Field(..., pattern=_SAFETY_STAGE_PATTERN)
    check_id: str = Field(..., min_length=1, max_length=128)
    config: dict[str, object] = Field(default_factory=dict)
    coworker_id: str | None = None
    priority: int = Field(100, ge=-1000, le=1000)
    enabled: bool = True
    description: str = Field("", max_length=500)


class SafetyRuleUpdate(BaseModel):
    stage: str | None = Field(None, pattern=_SAFETY_STAGE_PATTERN)
    check_id: str | None = Field(None, min_length=1, max_length=128)
    config: dict[str, object] | None = None
    priority: int | None = Field(None, ge=-1000, le=1000)
    enabled: bool | None = None
    description: str | None = Field(None, max_length=500)


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


_SKILL_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
_RESERVED_SKILL_NAMES_ADMIN: frozenset[str] = frozenset({"anthropic", "claude"})


class SkillFileInPayload(BaseModel):
    """One file in the wire format. The ``files`` map on ``SkillCreate``
    accepts ``str`` for SKILL.md content directly or this model for
    files that need an explicit mime type. Keeping it as a Pydantic
    model rather than a bare str makes future binary support a clean
    schema extension.
    """

    content: str
    mime_type: str = "text/plain"


class SkillResponse(BaseModel):
    id: str
    coworker_id: str
    name: str
    frontmatter_common: dict[str, object] = Field(default_factory=dict)
    frontmatter_backend: dict[str, dict[str, object]] = Field(default_factory=dict)
    enabled: bool
    created_at: str
    updated_at: str
    created_by_user_id: str | None = None
    files: dict[str, SkillFileInPayload] = Field(default_factory=dict)


class SkillSummary(BaseModel):
    """List-view shape — drops file content to keep responses small."""

    id: str
    coworker_id: str
    name: str
    description: str
    enabled: bool
    created_at: str
    updated_at: str


class SkillCreate(BaseModel):
    name: str = Field(..., pattern=_SKILL_NAME_PATTERN)
    enabled: bool = True
    # ``files`` accepts either a flat ``{path: content}`` map (the
    # common case) or ``{path: {"content": ..., "mime_type": ...}}``
    # for richer metadata. The handler normalizes both shapes.
    files: dict[str, str | SkillFileInPayload] = Field(default_factory=dict)
    # Optional structured frontmatter overrides; when present they
    # win over keys parsed from the inline SKILL.md frontmatter.
    frontmatter_common: dict[str, object] | None = None
    frontmatter_backend: dict[str, dict[str, object]] | None = None

    @field_validator("name")
    @classmethod
    def _check_not_reserved(cls, v: str) -> str:
        if v in _RESERVED_SKILL_NAMES_ADMIN:
            raise ValueError(
                f"skill name {v!r} is reserved by the Claude runtime"
            )
        return v


class SkillUpdate(BaseModel):
    """Partial update. ``files`` is treated as a full replacement of
    the skill's file set when provided (for surgical edits, use the
    per-file PATCH endpoint).
    """

    enabled: bool | None = None
    files: dict[str, str | SkillFileInPayload] | None = None
    frontmatter_common: dict[str, object] | None = None
    frontmatter_backend: dict[str, dict[str, object]] | None = None


class SkillFileUpdate(BaseModel):
    content: str
    mime_type: str = "text/plain"
