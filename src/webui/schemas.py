"""Pydantic request/response models for the Admin API."""

from __future__ import annotations

from pydantic import BaseModel, Field

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
    agent_role: str


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
    skills: list[str] = Field(default_factory=list)
    max_concurrent: int
    status: str
    agent_role: str
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
    requires_trigger: bool
    created_at: str


class AgentDetailResponse(AgentResponse):
    bindings: list[BindingResponse] = Field(default_factory=list)
    conversations: list[ConversationResponse] = Field(default_factory=list)


class AgentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    folder: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    agent_backend: str = "claude-code"
    system_prompt: str | None = None
    tools: list[dict[str, object]] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    max_concurrent: int = Field(2, ge=1, le=20)
    agent_role: str = Field("agent", pattern=r"^(super_agent|agent)$")
    permissions: dict[str, object] | None = None


class AgentUpdate(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    tools: list[dict[str, object]] | None = None
    skills: list[str] | None = None
    max_concurrent: int | None = Field(None, ge=1, le=20)
    status: str | None = Field(None, pattern=r"^(active|paused|disabled)$")
    agent_role: str | None = Field(None, pattern=r"^(super_agent|agent)$")
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
    requires_trigger: bool = True


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
# Approval policies
# ---------------------------------------------------------------------------


class ApprovalPolicyResponse(BaseModel):
    id: str
    tenant_id: str
    coworker_id: str | None = None
    mcp_server_name: str
    tool_name: str
    condition_expr: dict[str, object]
    approver_user_ids: list[str] = Field(default_factory=list)
    notify_conversation_id: str | None = None
    auto_expire_minutes: int
    post_exec_mode: str
    enabled: bool
    priority: int
    created_at: str
    updated_at: str


class ApprovalPolicyCreate(BaseModel):
    mcp_server_name: str = Field(..., min_length=1)
    tool_name: str = Field(..., min_length=1)
    condition_expr: dict[str, object]
    coworker_id: str | None = None
    approver_user_ids: list[str] = Field(default_factory=list)
    notify_conversation_id: str | None = None
    auto_expire_minutes: int = Field(60, ge=1, le=10080)
    post_exec_mode: str = Field("report", pattern=r"^(report)$")
    enabled: bool = True
    priority: int = Field(0, ge=-1000, le=1000)


class ApprovalPolicyUpdate(BaseModel):
    mcp_server_name: str | None = Field(None, min_length=1)
    tool_name: str | None = Field(None, min_length=1)
    condition_expr: dict[str, object] | None = None
    approver_user_ids: list[str] | None = None
    notify_conversation_id: str | None = None
    auto_expire_minutes: int | None = Field(None, ge=1, le=10080)
    post_exec_mode: str | None = Field(None, pattern=r"^(report)$")
    enabled: bool | None = None
    priority: int | None = Field(None, ge=-1000, le=1000)


# ---------------------------------------------------------------------------
# Approval requests + audit
# ---------------------------------------------------------------------------


class ApprovalRequestResponse(BaseModel):
    id: str
    tenant_id: str
    coworker_id: str
    conversation_id: str | None = None
    policy_id: str
    user_id: str
    job_id: str
    mcp_server_name: str
    actions: list[dict[str, object]] = Field(default_factory=list)
    action_hashes: list[str] = Field(default_factory=list)
    rationale: str | None = None
    source: str
    status: str
    post_exec_mode: str
    resolved_approvers: list[str] = Field(default_factory=list)
    requested_at: str
    expires_at: str
    created_at: str
    updated_at: str


class ApprovalAuditEntryResponse(BaseModel):
    id: str
    request_id: str
    action: str
    actor_user_id: str | None = None
    note: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: str


class ApprovalRequestDetailResponse(ApprovalRequestResponse):
    audit_log: list[ApprovalAuditEntryResponse] = Field(default_factory=list)


class ApprovalDecisionRequest(BaseModel):
    action: str = Field(..., pattern=r"^(approve|reject)$")
    # Optional human-readable rationale from the approver. Length-capped
    # so a careless approver cannot store kilobytes of text per decision.
    # Downstream notification channels may render Markdown; we strip
    # control characters at the REST boundary (see admin.decide_approval_ep)
    # to reduce the surface for unintentional formatting injection.
    note: str | None = Field(None, max_length=1000)
