"""Admin API endpoints for tenant, user, agent, binding, conversation, and task management."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from rolemesh.approval.engine import ApprovalEngine, ConflictError, ForbiddenError
from rolemesh.auth.permissions import AgentPermissions
from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.core.group_folder import is_valid_group_folder
from rolemesh.core.types import McpServerConfig
from rolemesh.db import pg
from webui.dependencies import (
    get_current_user,
    require_manage_agents,
    require_manage_tenant,
    require_manage_users,
)
from webui.schemas import (
    AgentCreate,
    AgentDetailResponse,
    AgentResponse,
    AgentSummary,
    AgentUpdate,
    ApprovalAuditEntryResponse,
    ApprovalDecisionRequest,
    ApprovalPolicyCreate,
    ApprovalPolicyResponse,
    ApprovalPolicyUpdate,
    ApprovalRequestDetailResponse,
    ApprovalRequestResponse,
    AssignRequest,
    BindingCreate,
    BindingResponse,
    BindingUpdate,
    ConversationCreate,
    ConversationResponse,
    SafetyRuleCreate,
    SafetyRuleResponse,
    SafetyRuleUpdate,
    TaskResponse,
    TenantResponse,
    TenantUpdate,
    UserCreate,
    UserDetailResponse,
    UserResponse,
    UserUpdate,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from rolemesh.approval.types import ApprovalAuditEntry, ApprovalPolicy, ApprovalRequest
    from rolemesh.core.types import ChannelBinding, Conversation, Coworker, ScheduledTask, Tenant, User
    from rolemesh.safety.types import Rule as SafetyRule

# Annotated dependency types (avoids B008 lint warnings)
OwnerUser = Annotated[AuthenticatedUser, Depends(require_manage_tenant)]
AdminUser = Annotated[AuthenticatedUser, Depends(require_manage_agents)]
UserManager = Annotated[AuthenticatedUser, Depends(require_manage_users)]
AuthedUser = Annotated[AuthenticatedUser, Depends(get_current_user)]

# Module-level ApprovalEngine handle — set from the WebUI bootstrap when
# approvals are wired up. None means the approval feature is not active
# in this process; decision endpoints will 503 rather than silently
# no-op, and list/get continue to work because they only touch the DB.
_approval_engine: ApprovalEngine | None = None


def set_approval_engine(engine: ApprovalEngine | None) -> None:
    """Attach or detach the process-wide ApprovalEngine."""
    global _approval_engine
    _approval_engine = engine


def _require_engine() -> ApprovalEngine:
    if _approval_engine is None:
        raise HTTPException(status_code=503, detail="Approval engine not configured")
    return _approval_engine


# Module-level NATS client used to publish ``egress.mcp.changed``
# deltas after an admin edits a coworker's MCP tools. Set from the
# WebUI bootstrap; ``None`` means MCP hot-reload broadcasts are off
# (the gateway still gets a current snapshot at orchestrator boot,
# so functionality degrades gracefully — operators just need to wait
# for a gateway restart for tool edits to land).
_mcp_publisher: Any = None


def set_mcp_publisher(nc: Any) -> None:
    """Attach or detach the process-wide NATS client used for MCP
    registry-change broadcasts.

    Type stays ``Any`` here to avoid pulling ``nats`` types into the
    admin module's import surface; the caller in ``webui.main``
    already has the typed handle.
    """
    global _mcp_publisher
    _mcp_publisher = nc


async def _publish_mcp_for_coworker(action: str, cw: Coworker) -> None:
    """Broadcast one ``egress.mcp.changed`` event per tool on the
    coworker. Best-effort — a NATS hiccup must not break the admin
    REST response, so the publisher itself swallows errors.

    Used after create / update of a coworker's tools list. We send
    one event per tool (not a single batched event) so the
    gateway-side ``apply_change_event`` stays trivial — the same
    handler shape works for boot snapshot + live deltas.

    NAMING-COLLISION CAVEAT: the gateway / orchestrator MCP registry
    is keyed by tool ``name`` alone, NOT by ``(coworker_id, name)``.
    If two coworkers configure a tool with the same ``name`` but
    different URLs, every PATCH against either coworker republishes
    its own URL and overwrites the other's entry on every gateway
    that receives the event. Pre-PR-2 this only collided once at
    orchestrator boot ("last writer wins"); after PR-2 it collides
    on every admin edit. The proper fix is to re-key the registry on
    ``(tenant_id, name)`` (operator scope) or ``(coworker_id, name)``
    (per-agent scope) — tracked as a follow-up. Until then, document
    in the operator playbook that tool names should be unique within
    a tenant.
    """
    if _mcp_publisher is None:
        return
    from urllib.parse import urlparse

    from rolemesh.egress.mcp_cache import McpEntry
    from rolemesh.egress.orch_glue import publish_mcp_registry_changed

    for tool in cw.tools:
        # Same origin computation the orchestrator's bootstrap
        # register_mcp_server walk uses (rolemesh/main.py); keep the
        # two paths aligned so the gateway never sees a tool URL the
        # snapshot would have stored differently.
        parsed = urlparse(tool.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        entry = McpEntry(
            name=tool.name,
            url=origin,
            headers=dict(tool.headers or {}),
            auth_mode=tool.auth_mode,
        )
        await publish_mcp_registry_changed(
            _mcp_publisher, action=action, entry=entry
        )


router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Dataclass -> Pydantic converters
# ---------------------------------------------------------------------------


def _tenant_to_response(t: Tenant) -> TenantResponse:
    return TenantResponse(
        id=t.id,
        name=t.name,
        slug=t.slug,
        plan=t.plan,
        max_concurrent_containers=t.max_concurrent_containers,
        created_at=t.created_at,
    )


def _user_to_response(u: User) -> UserResponse:
    return UserResponse(
        id=u.id,
        tenant_id=u.tenant_id,
        name=u.name,
        email=u.email,
        role=u.role,
        channel_ids=u.channel_ids,
        created_at=u.created_at,
    )


def _coworker_to_response(cw: Coworker) -> AgentResponse:
    return AgentResponse(
        id=cw.id,
        tenant_id=cw.tenant_id,
        name=cw.name,
        folder=cw.folder,
        agent_backend=cw.agent_backend,
        system_prompt=cw.system_prompt,
        tools=[
            {
                "name": t.name,
                "type": t.type,
                "url": t.url,
                "headers": t.headers,
                "auth_mode": t.auth_mode,
            }
            for t in cw.tools
        ],
        skills=cw.skills,
        max_concurrent=cw.max_concurrent,
        status=cw.status,
        agent_role=cw.agent_role,
        permissions=cw.permissions.to_dict() if cw.permissions else {},
        created_at=cw.created_at,
    )


def _coworker_to_summary(cw: Coworker) -> AgentSummary:
    return AgentSummary(
        id=cw.id,
        name=cw.name,
        folder=cw.folder,
        status=cw.status,
        agent_role=cw.agent_role,
    )


def _binding_to_response(b: ChannelBinding) -> BindingResponse:
    return BindingResponse(
        id=b.id,
        coworker_id=b.coworker_id,
        tenant_id=b.tenant_id,
        channel_type=b.channel_type,
        credentials=b.credentials,
        bot_display_name=b.bot_display_name,
        status=b.status,
        created_at=b.created_at,
    )


def _conversation_to_response(c: Conversation) -> ConversationResponse:
    return ConversationResponse(
        id=c.id,
        tenant_id=c.tenant_id,
        coworker_id=c.coworker_id,
        channel_binding_id=c.channel_binding_id,
        channel_chat_id=c.channel_chat_id,
        name=c.name,
        requires_trigger=c.requires_trigger,
        created_at=c.created_at,
    )


def _task_to_response(t: ScheduledTask) -> TaskResponse:
    return TaskResponse(
        id=t.id,
        tenant_id=t.tenant_id,
        coworker_id=t.coworker_id,
        prompt=t.prompt,
        schedule_type=t.schedule_type,
        schedule_value=t.schedule_value,
        context_mode=t.context_mode,
        conversation_id=t.conversation_id,
        next_run=t.next_run,
        last_run=t.last_run,
        status=t.status,
        created_at=t.created_at,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_agent_or_404(agent_id: str, tenant_id: str) -> Coworker:
    """Fetch a coworker, raising 404 if not found or cross-tenant."""
    cw = await pg.get_coworker(agent_id, tenant_id=tenant_id)
    if cw is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return cw


def _parse_tools(tools_dicts: list[dict[str, object]]) -> list[McpServerConfig]:
    """Convert a list of tool dicts to McpServerConfig objects."""
    result: list[McpServerConfig] = []
    for t in tools_dicts:
        auth_mode = str(t.get("auth_mode") or "user")
        if auth_mode not in ("user", "service", "both"):
            auth_mode = "user"
        result.append(
            McpServerConfig(
                name=str(t["name"]),
                type=str(t.get("type", "http")),
                url=str(t["url"]),
                headers=dict(t.get("headers") or {}),  # type: ignore[arg-type]
                auth_mode=auth_mode,
            )
        )
    return result


def _parse_permissions(perms_dict: dict[str, object] | None) -> AgentPermissions | None:
    """Convert a permissions dict to AgentPermissions, or None."""
    if not perms_dict:
        return None
    return AgentPermissions.from_dict(perms_dict)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tenant endpoints (owner only)
# ---------------------------------------------------------------------------


@router.get("/tenant", response_model=TenantResponse)
async def get_tenant(user: OwnerUser) -> TenantResponse:
    tenant = await pg.get_tenant(user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return _tenant_to_response(tenant)


@router.patch("/tenant", response_model=TenantResponse)
async def update_tenant(
    body: TenantUpdate,
    user: OwnerUser,
) -> TenantResponse:
    tenant = await pg.update_tenant(
        user.tenant_id,
        name=body.name,
        max_concurrent_containers=body.max_concurrent_containers,
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return _tenant_to_response(tenant)


# ---------------------------------------------------------------------------
# User endpoints (admin+)
# ---------------------------------------------------------------------------


@router.get("/users", response_model=list[UserResponse])
async def list_users(user: UserManager) -> list[UserResponse]:
    users = await pg.get_users_for_tenant(user.tenant_id)
    return [_user_to_response(u) for u in users]


@router.post("/users", response_model=UserResponse, status_code=201)
async def create_user(
    body: UserCreate,
    user: UserManager,
) -> UserResponse:
    if body.role == "owner" and user.role != "owner":
        raise HTTPException(status_code=403, detail="Only owners can create owner-role users")
    new_user = await pg.create_user(
        tenant_id=user.tenant_id,
        name=body.name,
        email=body.email,
        role=body.role,
        channel_ids=body.channel_ids or None,
    )
    return _user_to_response(new_user)


@router.get("/users/{user_id}", response_model=UserDetailResponse)
async def get_user_detail(
    user_id: str,
    user: UserManager,
) -> UserDetailResponse:
    target = await pg.get_user(user_id, tenant_id=user.tenant_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    agents = await pg.get_agents_for_user(user_id, tenant_id=user.tenant_id)
    resp = UserDetailResponse(
        **_user_to_response(target).model_dump(),
        assigned_agents=[_coworker_to_summary(a) for a in agents],
    )
    return resp


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    body: UserUpdate,
    user: UserManager,
) -> UserResponse:
    target = await pg.get_user(user_id, tenant_id=user.tenant_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if body.role == "owner" and user.role != "owner":
        raise HTTPException(status_code=403, detail="Only owners can assign owner role")
    updated = await pg.update_user(
        user_id,
        tenant_id=user.tenant_id,
        name=body.name,
        email=body.email,
        role=body.role,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found")
    return _user_to_response(updated)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    user: UserManager,
) -> None:
    if user_id == user.user_id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    target = await pg.get_user(user_id, tenant_id=user.tenant_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    await pg.delete_user(user_id, tenant_id=user.tenant_id)


# ---------------------------------------------------------------------------
# Agent endpoints (admin+)
# ---------------------------------------------------------------------------


@router.get("/agents", response_model=list[AgentResponse])
async def list_agents(user: AdminUser) -> list[AgentResponse]:
    agents = await pg.get_coworkers_for_tenant(user.tenant_id)
    return [_coworker_to_response(a) for a in agents]


@router.post("/agents", response_model=AgentResponse, status_code=201)
async def create_agent(
    body: AgentCreate,
    user: AdminUser,
) -> AgentResponse:
    if not is_valid_group_folder(body.folder):
        raise HTTPException(status_code=400, detail=f"Invalid folder name: {body.folder!r}")
    tools = _parse_tools(body.tools) if body.tools else None
    permissions = _parse_permissions(body.permissions)
    try:
        cw = await pg.create_coworker(
            tenant_id=user.tenant_id,
            name=body.name,
            folder=body.folder,
            agent_backend=body.agent_backend,
            system_prompt=body.system_prompt,
            tools=tools,
            skills=body.skills or None,
            max_concurrent=body.max_concurrent,
            agent_role=body.agent_role,
            permissions=permissions,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Agent with this folder already exists in tenant") from exc
    # Notify the egress gateway of the freshly-registered MCP tools
    # so it doesn't have to wait for an orchestrator restart to route
    # them. Best-effort; the snapshot path on gateway boot still
    # recovers full state if the broadcast misses.
    await _publish_mcp_for_coworker("created", cw)
    return _coworker_to_response(cw)


@router.get("/agents/{agent_id}", response_model=AgentDetailResponse)
async def get_agent_detail(
    agent_id: str,
    user: AdminUser,
) -> AgentDetailResponse:
    cw = await _get_agent_or_404(agent_id, user.tenant_id)
    bindings = await pg.get_channel_bindings_for_coworker(agent_id, tenant_id=user.tenant_id)
    conversations = await pg.get_conversations_for_coworker(agent_id, tenant_id=user.tenant_id)
    return AgentDetailResponse(
        **_coworker_to_response(cw).model_dump(),
        bindings=[_binding_to_response(b) for b in bindings],
        conversations=[_conversation_to_response(c) for c in conversations],
    )


@router.patch("/agents/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: str,
    body: AgentUpdate,
    user: AdminUser,
) -> AgentResponse:
    await _get_agent_or_404(agent_id, user.tenant_id)
    tools = _parse_tools(body.tools) if body.tools is not None else None
    permissions = _parse_permissions(body.permissions)
    updated = await pg.update_coworker(
        agent_id,
        tenant_id=user.tenant_id,
        name=body.name,
        system_prompt=body.system_prompt,
        tools=tools,
        skills=body.skills,
        max_concurrent=body.max_concurrent,
        status=body.status,
        agent_role=body.agent_role,
        permissions=permissions,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    # Hot-reload MCP routes only when tools were actually included
    # in the PATCH (``body.tools is not None``). A PATCH that only
    # touches name/permissions/etc. shouldn't generate spurious
    # mcp.changed traffic.
    #
    # NOTE: if any of the new tools share a ``name`` with a tool on
    # another coworker, this broadcast will overwrite that other
    # coworker's URL on the gateway because the registry is name-
    # keyed. See ``_publish_mcp_for_coworker`` docstring for the
    # follow-up plan.
    if body.tools is not None:
        await _publish_mcp_for_coworker("updated", updated)
    return _coworker_to_response(updated)


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str,
    user: AdminUser,
) -> None:
    await _get_agent_or_404(agent_id, user.tenant_id)
    await pg.delete_coworker(agent_id, tenant_id=user.tenant_id)


# ---------------------------------------------------------------------------
# Assignment endpoints (admin+)
# ---------------------------------------------------------------------------


@router.get("/agents/{agent_id}/users", response_model=list[UserResponse])
async def list_assigned_users(
    agent_id: str,
    user: AdminUser,
) -> list[UserResponse]:
    await _get_agent_or_404(agent_id, user.tenant_id)
    users = await pg.get_users_for_agent(agent_id, tenant_id=user.tenant_id)
    return [_user_to_response(u) for u in users]


@router.post("/agents/{agent_id}/assign", status_code=204)
async def assign_agent(
    agent_id: str,
    body: AssignRequest,
    user: AdminUser,
) -> None:
    await _get_agent_or_404(agent_id, user.tenant_id)
    target = await pg.get_user(body.user_id, tenant_id=user.tenant_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    await pg.assign_agent_to_user(body.user_id, agent_id, user.tenant_id)


@router.delete("/agents/{agent_id}/assign/{user_id}", status_code=204)
async def unassign_agent(
    agent_id: str,
    user_id: str,
    user: AdminUser,
) -> None:
    await _get_agent_or_404(agent_id, user.tenant_id)
    target = await pg.get_user(user_id, tenant_id=user.tenant_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    await pg.unassign_agent_from_user(user_id, agent_id, tenant_id=user.tenant_id)


# ---------------------------------------------------------------------------
# Binding endpoints (admin+)
# ---------------------------------------------------------------------------


@router.get("/agents/{agent_id}/bindings", response_model=list[BindingResponse])
async def list_bindings(
    agent_id: str,
    user: AdminUser,
) -> list[BindingResponse]:
    await _get_agent_or_404(agent_id, user.tenant_id)
    bindings = await pg.get_channel_bindings_for_coworker(agent_id, tenant_id=user.tenant_id)
    return [_binding_to_response(b) for b in bindings]


@router.post("/agents/{agent_id}/bindings", response_model=BindingResponse, status_code=201)
async def create_binding(
    agent_id: str,
    body: BindingCreate,
    user: AdminUser,
) -> BindingResponse:
    await _get_agent_or_404(agent_id, user.tenant_id)
    try:
        binding = await pg.create_channel_binding(
            coworker_id=agent_id,
            tenant_id=user.tenant_id,
            channel_type=body.channel_type,
            credentials=body.credentials or None,
            bot_display_name=body.bot_display_name,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Binding for this channel type already exists") from exc
    return _binding_to_response(binding)


@router.patch("/agents/{agent_id}/bindings/{binding_id}", response_model=BindingResponse)
async def update_binding(
    agent_id: str,
    binding_id: str,
    body: BindingUpdate,
    user: AdminUser,
) -> BindingResponse:
    await _get_agent_or_404(agent_id, user.tenant_id)
    binding = await pg.get_channel_binding(binding_id, tenant_id=user.tenant_id)
    if binding is None or binding.coworker_id != agent_id:
        raise HTTPException(status_code=404, detail="Binding not found")
    updated = await pg.update_channel_binding(
        binding_id,
        tenant_id=user.tenant_id,
        credentials=body.credentials,
        bot_display_name=body.bot_display_name,
        status=body.status,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Binding not found")
    return _binding_to_response(updated)


@router.delete("/agents/{agent_id}/bindings/{binding_id}", status_code=204)
async def delete_binding(
    agent_id: str,
    binding_id: str,
    user: AdminUser,
) -> None:
    await _get_agent_or_404(agent_id, user.tenant_id)
    binding = await pg.get_channel_binding(binding_id, tenant_id=user.tenant_id)
    if binding is None or binding.coworker_id != agent_id:
        raise HTTPException(status_code=404, detail="Binding not found")
    await pg.delete_channel_binding(binding_id, tenant_id=user.tenant_id)


# ---------------------------------------------------------------------------
# Conversation endpoints (admin+)
# ---------------------------------------------------------------------------


@router.get("/agents/{agent_id}/conversations", response_model=list[ConversationResponse])
async def list_conversations(
    agent_id: str,
    user: AdminUser,
) -> list[ConversationResponse]:
    await _get_agent_or_404(agent_id, user.tenant_id)
    conversations = await pg.get_conversations_for_coworker(agent_id, tenant_id=user.tenant_id)
    return [_conversation_to_response(c) for c in conversations]


@router.post("/agents/{agent_id}/conversations", response_model=ConversationResponse, status_code=201)
async def create_conversation(
    agent_id: str,
    body: ConversationCreate,
    user: AdminUser,
) -> ConversationResponse:
    await _get_agent_or_404(agent_id, user.tenant_id)
    # Verify the binding belongs to this agent
    binding = await pg.get_channel_binding(body.channel_binding_id, tenant_id=user.tenant_id)
    if binding is None or binding.coworker_id != agent_id:
        raise HTTPException(status_code=400, detail="Binding does not belong to this agent")
    conv = await pg.create_conversation(
        tenant_id=user.tenant_id,
        coworker_id=agent_id,
        channel_binding_id=body.channel_binding_id,
        channel_chat_id=body.channel_chat_id,
        name=body.name,
        requires_trigger=body.requires_trigger,
    )
    return _conversation_to_response(conv)


@router.delete("/agents/{agent_id}/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    agent_id: str,
    conversation_id: str,
    user: AdminUser,
) -> None:
    await _get_agent_or_404(agent_id, user.tenant_id)
    conv = await pg.get_conversation(conversation_id, tenant_id=user.tenant_id)
    if conv is None or conv.coworker_id != agent_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await pg.delete_conversation(conversation_id, tenant_id=user.tenant_id)


# ---------------------------------------------------------------------------
# Task endpoints (admin+)
# ---------------------------------------------------------------------------


@router.get("/tasks", response_model=list[TaskResponse])
async def list_all_tasks(user: AdminUser) -> list[TaskResponse]:
    tasks = await pg.get_all_tasks(tenant_id=user.tenant_id)
    return [_task_to_response(t) for t in tasks]


@router.get("/agents/{agent_id}/tasks", response_model=list[TaskResponse])
async def list_agent_tasks(
    agent_id: str,
    user: AdminUser,
) -> list[TaskResponse]:
    await _get_agent_or_404(agent_id, user.tenant_id)
    tasks = await pg.get_tasks_for_coworker(agent_id, tenant_id=user.tenant_id)
    return [_task_to_response(t) for t in tasks]


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(
    task_id: str,
    user: AdminUser,
) -> None:
    task = await pg.get_task_by_id(task_id, tenant_id=user.tenant_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await pg.delete_task(task_id, tenant_id=user.tenant_id)


# ---------------------------------------------------------------------------
# Approval: policies (admin+)
# ---------------------------------------------------------------------------


def _policy_to_response(p: ApprovalPolicy) -> ApprovalPolicyResponse:
    return ApprovalPolicyResponse(
        id=p.id,
        tenant_id=p.tenant_id,
        coworker_id=p.coworker_id,
        mcp_server_name=p.mcp_server_name,
        tool_name=p.tool_name,
        condition_expr=p.condition_expr,
        approver_user_ids=p.approver_user_ids,
        notify_conversation_id=p.notify_conversation_id,
        auto_expire_minutes=p.auto_expire_minutes,
        post_exec_mode=p.post_exec_mode,
        enabled=p.enabled,
        priority=p.priority,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


@router.get("/approval-policies", response_model=list[ApprovalPolicyResponse])
async def list_approval_policies_ep(
    user: AdminUser,
    coworker_id: str | None = None,
    enabled: bool | None = None,
) -> list[ApprovalPolicyResponse]:
    rows = await pg.list_approval_policies(
        user.tenant_id, coworker_id=coworker_id, enabled=enabled
    )
    return [_policy_to_response(p) for p in rows]


@router.post(
    "/approval-policies",
    response_model=ApprovalPolicyResponse,
    status_code=201,
)
async def create_approval_policy_ep(
    body: ApprovalPolicyCreate,
    user: AdminUser,
) -> ApprovalPolicyResponse:
    if body.coworker_id is not None:
        # Guard against cross-tenant policy creation: a tenant admin must
        # not be able to attach a policy to a coworker they don't own.
        await _get_agent_or_404(body.coworker_id, user.tenant_id)
    p = await pg.create_approval_policy(
        tenant_id=user.tenant_id,
        coworker_id=body.coworker_id,
        mcp_server_name=body.mcp_server_name,
        tool_name=body.tool_name,
        condition_expr=body.condition_expr,
        approver_user_ids=body.approver_user_ids,
        notify_conversation_id=body.notify_conversation_id,
        auto_expire_minutes=body.auto_expire_minutes,
        post_exec_mode=body.post_exec_mode,
        enabled=body.enabled,
        priority=body.priority,
    )
    return _policy_to_response(p)


@router.get("/approval-policies/{policy_id}", response_model=ApprovalPolicyResponse)
async def get_approval_policy_ep(
    policy_id: str,
    user: AdminUser,
) -> ApprovalPolicyResponse:
    p = await pg.get_approval_policy(policy_id, tenant_id=user.tenant_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    return _policy_to_response(p)


@router.patch(
    "/approval-policies/{policy_id}", response_model=ApprovalPolicyResponse
)
async def update_approval_policy_ep(
    policy_id: str,
    body: ApprovalPolicyUpdate,
    user: AdminUser,
) -> ApprovalPolicyResponse:
    existing = await pg.get_approval_policy(policy_id, tenant_id=user.tenant_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    updated = await pg.update_approval_policy(
        policy_id,
        tenant_id=user.tenant_id,
        mcp_server_name=body.mcp_server_name,
        tool_name=body.tool_name,
        condition_expr=body.condition_expr,
        approver_user_ids=body.approver_user_ids,
        notify_conversation_id=body.notify_conversation_id,
        auto_expire_minutes=body.auto_expire_minutes,
        post_exec_mode=body.post_exec_mode,
        enabled=body.enabled,
        priority=body.priority,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    return _policy_to_response(updated)


@router.delete("/approval-policies/{policy_id}", status_code=204)
async def delete_approval_policy_ep(
    policy_id: str,
    user: AdminUser,
) -> None:
    existing = await pg.get_approval_policy(policy_id, tenant_id=user.tenant_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    await pg.delete_approval_policy(policy_id, tenant_id=user.tenant_id)


# ---------------------------------------------------------------------------
# Approval: requests (any authenticated user can list their own;
# admins see the full tenant)
# ---------------------------------------------------------------------------


def _request_to_response(r: ApprovalRequest) -> ApprovalRequestResponse:
    return ApprovalRequestResponse(
        id=r.id,
        tenant_id=r.tenant_id,
        coworker_id=r.coworker_id,
        conversation_id=r.conversation_id,
        policy_id=r.policy_id,
        user_id=r.user_id,
        job_id=r.job_id,
        mcp_server_name=r.mcp_server_name,
        actions=r.actions,
        action_hashes=r.action_hashes,
        rationale=r.rationale,
        source=r.source,
        status=r.status,
        post_exec_mode=r.post_exec_mode,
        resolved_approvers=r.resolved_approvers,
        requested_at=r.requested_at,
        expires_at=r.expires_at,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _audit_to_response(e: ApprovalAuditEntry) -> ApprovalAuditEntryResponse:
    return ApprovalAuditEntryResponse(
        id=e.id,
        request_id=e.request_id,
        action=e.action,
        actor_user_id=e.actor_user_id,
        note=e.note,
        metadata=e.metadata,
        created_at=e.created_at,
    )


@router.get("/approvals", response_model=list[ApprovalRequestResponse])
async def list_approvals_ep(
    user: AuthedUser,
    status: str | None = None,
    coworker_id: str | None = None,
) -> list[ApprovalRequestResponse]:
    rows = await pg.list_approval_requests(
        user.tenant_id, status=status, coworker_id=coworker_id
    )
    return [_request_to_response(r) for r in rows]


@router.get(
    "/approvals/{request_id}", response_model=ApprovalRequestDetailResponse
)
async def get_approval_ep(
    request_id: str,
    user: AuthedUser,
) -> ApprovalRequestDetailResponse:
    req = await pg.get_approval_request(request_id, tenant_id=user.tenant_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    audit = await pg.list_approval_audit(request_id, tenant_id=user.tenant_id)
    return ApprovalRequestDetailResponse(
        **_request_to_response(req).model_dump(),
        audit_log=[_audit_to_response(e) for e in audit],
    )


@router.get(
    "/approvals/{request_id}/audit-log",
    response_model=list[ApprovalAuditEntryResponse],
)
async def get_approval_audit_ep(
    request_id: str,
    user: AuthedUser,
) -> list[ApprovalAuditEntryResponse]:
    req = await pg.get_approval_request(request_id, tenant_id=user.tenant_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    rows = await pg.list_approval_audit(request_id, tenant_id=user.tenant_id)
    return [_audit_to_response(r) for r in rows]


def _sanitize_note(note: str | None) -> str | None:
    """Trim whitespace and strip ASCII/C1 control characters.

    Pydantic already enforces max_length=1000; we still strip control
    characters here because a future Markdown-rendering channel could
    interpret e.g. \\r\\n as a heading break or \\x1b as an escape
    sequence. Keeping the filter at the REST boundary means stored
    notes are clean without a downstream channel-by-channel sanitizer.
    """
    if note is None:
        return None
    cleaned = "".join(
        c for c in note if c == "\n" or c == "\t" or (0x20 <= ord(c) < 0x7F) or ord(c) > 0xA0
    ).strip()
    return cleaned or None


@router.post(
    "/approvals/{request_id}/decide",
    response_model=ApprovalRequestResponse,
)
async def decide_approval_ep(
    request_id: str,
    body: ApprovalDecisionRequest,
    user: AuthedUser,
) -> ApprovalRequestResponse:
    engine = _require_engine()
    req = await pg.get_approval_request(request_id, tenant_id=user.tenant_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    try:
        updated = await engine.handle_decision(
            request_id=request_id,
            tenant_id=user.tenant_id,
            action=body.action,
            user_id=user.user_id,
            note=_sanitize_note(body.note),
        )
    except ForbiddenError as exc:
        raise HTTPException(
            status_code=403, detail="User is not an authorised approver"
        ) from exc
    except ConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Request already {exc.current_status}",
        ) from exc
    return _request_to_response(updated)


# ---------------------------------------------------------------------------
# Safety: rules (admin+)
# ---------------------------------------------------------------------------


def _safety_rule_to_response(r: SafetyRule) -> SafetyRuleResponse:
    return SafetyRuleResponse(
        id=r.id,
        tenant_id=r.tenant_id,
        coworker_id=r.coworker_id,
        stage=r.stage.value,
        check_id=r.check_id,
        config=r.config,
        priority=r.priority,
        enabled=r.enabled,
        description=r.description,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


async def _validate_safety_rule_body(
    check_id: str,
    stage: str,
    config: dict[str, object],
    *,
    tenant_id: str,
    coworker_id: str | None,
) -> None:
    """Raise HTTPException(400) if the rule cannot be satisfied at run-time.

    REST is the strict boundary: misconfigured rules are rejected here
    before they land in the DB. The container-side pipeline is
    permissive on stale snapshots (log + skip), but a fresh admin
    action must fail loud so typos surface immediately rather than
    subtly acting wrong at run-time.

    Extra V2 P0.4 check: if the check is ``cost_class=slow`` AND the
    stage is PRE_TOOL_CALL, we verify the rule's scope does not include
    any reversible tool. Reversible tools have a 100 ms budget at
    PRE_TOOL_CALL that slow checks can't meet; the runtime guard
    (pipeline_core) will skip the rule when it fires, but admin-time
    rejection gives operators the feedback immediately instead of
    silent skips at runtime.

    V2 P1.1: ``action_override`` is validated against the closed set
    {block, warn, require_approval}. ``redact`` is refused because an
    override cannot synthesize a modified_payload.
    """
    # Lazy import avoids a WebUI → rolemesh.safety cycle at module load.
    from pydantic import ValidationError

    from rolemesh.safety.registry import get_orchestrator_registry
    from rolemesh.safety.tool_reversibility import get_tool_reversibility
    from rolemesh.safety.types import Stage

    registry = get_orchestrator_registry()
    if not registry.has(check_id):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown safety check_id: {check_id}",
        )
    check = registry.get(check_id)
    try:
        stage_enum = Stage(stage)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"Unknown stage: {stage}"
        ) from exc
    if stage_enum not in check.stages:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Check {check_id} does not support stage {stage}; "
                f"valid stages: {sorted(s.value for s in check.stages)}"
            ),
        )
    if not isinstance(config, dict):
        raise HTTPException(
            status_code=400, detail="config must be a JSON object"
        )
    # Pydantic validation (unknown keys, wrong types) — the check's
    # declared config_model is the source of truth. Older checks
    # without a model are tolerated, matching the permissive run-time
    # contract.
    config_model = getattr(check, "config_model", None)
    if config_model is not None:
        try:
            config_model.model_validate(config)
        except ValidationError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid config for {check_id}: {exc.errors()}",
            ) from exc

    # V2 P1.1: action_override whitelist. redact is explicitly refused
    # because the check did not produce a modified_payload.
    override = config.get("action_override") if isinstance(config, dict) else None
    if override is not None:
        valid = {"block", "warn", "require_approval"}
        if override not in valid:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid action_override {override!r}; "
                    f"must be one of {sorted(valid)} "
                    f"(redact cannot be synthesized via override)"
                ),
            )

    # V2 P0.4: reversibility guard at admin time. Only runs when the
    # check is slow AND the stage is PRE_TOOL_CALL — other combinations
    # have no budget conflict. Scope expansion:
    #   coworker_id is None → tenant-wide rule → union of every
    #     coworker's tools
    #   coworker_id is set  → single coworker's tools
    if (
        getattr(check, "cost_class", "cheap") == "slow"
        and stage_enum == Stage.PRE_TOOL_CALL
    ):
        coworkers: list[object] = []
        if coworker_id is not None:
            cw = await pg.get_coworker(coworker_id, tenant_id=tenant_id)
            if cw is not None:
                coworkers.append(cw)
        else:
            coworkers.extend(await pg.get_coworkers_for_tenant(tenant_id))
        for cw_any in coworkers:
            tools = getattr(cw_any, "tools", None) or []
            for mcp in tools:
                overrides = getattr(mcp, "tool_reversibility", {}) or {}
                # Builtins win — operator can't flip a stock tool's
                # reversibility via MCP override. The helper does that
                # layering for us; we just scan the declared bare
                # names per MCP server.
                for bare_name in overrides:
                    if get_tool_reversibility(bare_name, overrides):
                        name = getattr(cw_any, "name", "?")
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"Rule with slow check {check_id!r} at "
                                f"PRE_TOOL_CALL is blocked: coworker "
                                f"{name!r} configures reversible tool "
                                f"{bare_name!r} which exceeds the "
                                "100 ms budget. Narrow the rule scope "
                                "or use a different stage."
                            ),
                        )


@router.post(
    "/safety/rules",
    response_model=SafetyRuleResponse,
    status_code=201,
)
async def create_safety_rule_ep(
    body: SafetyRuleCreate,
    user: AdminUser,
) -> SafetyRuleResponse:
    if body.coworker_id is not None:
        # Guard against cross-tenant rule creation: a tenant admin MUST
        # NOT be able to attach a rule to a coworker they don't own.
        await _get_agent_or_404(body.coworker_id, user.tenant_id)
    # Run tenant-scoped validation after the coworker cross-tenant
    # guard so a cross-tenant coworker_id surfaces as 404 rather than
    # leaking through the scope-expansion query.
    await _validate_safety_rule_body(
        body.check_id,
        body.stage,
        dict(body.config),
        tenant_id=user.tenant_id,
        coworker_id=body.coworker_id,
    )
    rule = await pg.create_safety_rule(
        tenant_id=user.tenant_id,
        coworker_id=body.coworker_id,
        stage=body.stage,
        check_id=body.check_id,
        config=body.config,
        priority=body.priority,
        enabled=body.enabled,
        description=body.description,
        actor_user_id=user.user_id,
    )
    await _publish_rule_changed("created", rule)
    return _safety_rule_to_response(rule)


@router.get(
    "/safety/rules", response_model=list[SafetyRuleResponse]
)
async def list_safety_rules_ep(
    user: AdminUser,
    coworker_id: str | None = None,
    stage: str | None = None,
    enabled: bool | None = None,
) -> list[SafetyRuleResponse]:
    rows = await pg.list_safety_rules(
        user.tenant_id,
        coworker_id=coworker_id,
        stage=stage,
        enabled=enabled,
    )
    return [_safety_rule_to_response(r) for r in rows]


@router.get(
    "/safety/rules/{rule_id}", response_model=SafetyRuleResponse
)
async def get_safety_rule_ep(
    rule_id: str,
    user: AdminUser,
) -> SafetyRuleResponse:
    r = await pg.get_safety_rule(rule_id, tenant_id=user.tenant_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    return _safety_rule_to_response(r)


@router.patch(
    "/safety/rules/{rule_id}", response_model=SafetyRuleResponse
)
async def update_safety_rule_ep(
    rule_id: str,
    body: SafetyRuleUpdate,
    user: AdminUser,
) -> SafetyRuleResponse:
    existing = await pg.get_safety_rule(rule_id, tenant_id=user.tenant_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Rule not found")

    # Re-validate when either check_id or stage changes: the stage must
    # remain valid for the (possibly new) check_id.
    eff_check = body.check_id if body.check_id is not None else existing.check_id
    eff_stage = body.stage if body.stage is not None else existing.stage.value
    eff_config = (
        body.config if body.config is not None else existing.config
    )
    if body.check_id is not None or body.stage is not None or body.config is not None:
        await _validate_safety_rule_body(
            eff_check,
            eff_stage,
            dict(eff_config),
            tenant_id=user.tenant_id,
            coworker_id=existing.coworker_id,
        )

    updated = await pg.update_safety_rule(
        rule_id,
        tenant_id=user.tenant_id,
        stage=body.stage,
        check_id=body.check_id,
        config=body.config,
        priority=body.priority,
        enabled=body.enabled,
        description=body.description,
        actor_user_id=user.user_id,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    await _publish_rule_changed("updated", updated)
    return _safety_rule_to_response(updated)


@router.delete("/safety/rules/{rule_id}", status_code=204)
async def delete_safety_rule_ep(
    rule_id: str,
    user: AdminUser,
) -> None:
    existing = await pg.get_safety_rule(rule_id, tenant_id=user.tenant_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    await pg.delete_safety_rule(
        rule_id, tenant_id=user.tenant_id, actor_user_id=user.user_id
    )
    await _publish_rule_changed("deleted", existing)


async def _publish_rule_changed(
    action: str, rule: SafetyRule
) -> None:
    """Publish a ``safety.rule.changed`` event to the egress gateway.

    Best-effort — a NATS outage here must NOT fail the REST call. The
    caller's DB row is already committed; the gateway recovers on its
    next full snapshot.
    """
    # EC-2: egress gateway subscribes to this subject for hot-reload
    # of its in-memory policy cache. Import lazily so a webui process
    # without the egress extras still works (it just doesn't publish).
    try:
        from rolemesh.egress.orch_glue import publish_rule_changed
        from webui import main as webui_main
    except ImportError:
        return
    nc = getattr(webui_main, "_nc", None)
    if nc is None:
        return
    import contextlib

    with contextlib.suppress(Exception):
        await publish_rule_changed(nc, action=action, rule=rule.to_snapshot_dict())


# ---------------------------------------------------------------------------
# V2 P1.5: check discovery and decisions read endpoints.
# The admin UI (deferred) will consume these. Exposing them as REST now
# lets operators build dashboards or curl-driven introspection without
# waiting on the UI work.
# ---------------------------------------------------------------------------


@router.get("/safety/checks")
async def list_safety_checks_ep(_user: AdminUser) -> list[dict[str, object]]:
    """Return metadata for every check registered on the orchestrator.

    Includes the pydantic JSON schema so clients (admin UI, API
    explorers) can render a config form without a second call. Checks
    without a config_model report ``None`` — legacy checks that accept
    arbitrary dicts (new checks are expected to declare a model).

    Ordering is stable-alphabetical on check_id so dashboards that
    cache results don't see phantom ordering changes.
    """
    from rolemesh.safety.registry import get_orchestrator_registry

    checks = sorted(
        get_orchestrator_registry().all(), key=lambda c: c.id
    )
    out: list[dict[str, object]] = []
    for c in checks:
        model = c.config_model
        schema: object | None = (
            model.model_json_schema()
            if model is not None and hasattr(model, "model_json_schema")
            else None
        )
        out.append(
            {
                "id": c.id,
                "version": c.version,
                "stages": sorted(s.value for s in c.stages),
                "cost_class": c.cost_class,
                "supported_codes": sorted(c.supported_codes),
                "config_schema": schema,
            }
        )
    return out


@router.get("/tenants/{tid}/safety/decisions")
async def list_safety_decisions_ep(
    tid: str,
    user: AdminUser,
    verdict_action: str | None = None,
    coworker_id: str | None = None,
    stage: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, object]:
    """Paginated decisions list.

    ``limit`` is capped at 200 so a misbehaving client can't scan the
    whole table in one call — CSV export exists for bulk retrieval.
    Returns ``{total, items}`` so the UI can render pagination without
    a second call.
    """
    if tid != user.tenant_id:
        raise HTTPException(status_code=403, detail="tenant scope mismatch")
    capped = max(1, min(int(limit), 200))
    safe_offset = max(0, int(offset))
    total = await pg.count_safety_decisions(
        tid,
        verdict_action=verdict_action,
        coworker_id=coworker_id,
        stage=stage,
        from_ts=from_ts,
        to_ts=to_ts,
    )
    items = await pg.list_safety_decisions(
        tid,
        verdict_action=verdict_action,
        coworker_id=coworker_id,
        stage=stage,
        from_ts=from_ts,
        to_ts=to_ts,
        limit=capped,
        offset=safe_offset,
    )
    return {"total": total, "items": items}


@router.get("/tenants/{tid}/safety/decisions/{decision_id}")
async def get_safety_decision_ep(
    tid: str,
    decision_id: str,
    user: AdminUser,
) -> dict[str, object]:
    """Full decision detail including approval_context when present.

    Returns 404 for cross-tenant lookup (not 403) — we don't leak UUID
    existence across tenants.
    """
    if tid != user.tenant_id:
        raise HTTPException(status_code=404, detail="decision not found")
    row = await pg.get_safety_decision(decision_id, tenant_id=tid)
    if row is None:
        raise HTTPException(status_code=404, detail="decision not found")
    return row


@router.get("/tenants/{tid}/safety/rules/{rule_id}/audit")
async def list_safety_rule_audit_ep(
    tid: str,
    rule_id: str,
    user: AdminUser,
    limit: int = 200,
) -> list[dict[str, object]]:
    """Rule-change timeline for the admin UI.

    Shows who changed what and when for one rule — the data source for
    compliance questions like "when was the SSN rule disabled?". The
    underlying ``safety_rules_audit`` table is append-only, written by
    the DB trigger; this endpoint is pure read.
    """
    if tid != user.tenant_id:
        raise HTTPException(status_code=403, detail="tenant scope mismatch")
    capped = max(1, min(int(limit), 500))
    return await pg.list_safety_rules_audit(
        tenant_id=tid, rule_id=rule_id, limit=capped
    )


# ---------------------------------------------------------------------------
# V2 P2.3: streaming CSV export of safety decisions.
# ---------------------------------------------------------------------------


# Compact, operator-friendly column set. Deliberately NOT the full
# audit row — ``findings`` is flattened to parallel code/severity
# lists so the CSV is a pivot table, not a nested JSON blob per
# cell. Operators who want the full row hit ``GET /safety/decisions/{id}``.
_CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "created_at",
    "tenant_id",
    "coworker_id",
    "conversation_id",
    "job_id",
    "stage",
    "verdict_action",
    "triggered_rule_ids",
    "finding_codes",
    "finding_severities",
    "context_summary",
)


_FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


def _csv_escape(value: object) -> str:
    """RFC 4180 quoting + CSV-formula-injection guard.

    Two jobs:

    1. **RFC 4180** — wrap fields containing comma / quote / newline
       in double quotes, doubling interior quotes.
    2. **Formula injection** — Excel and Google Sheets interpret a
       cell whose text starts with ``=``, ``+``, ``-``, ``@``, tab
       or CR as a formula (including network-fetching functions like
       ``HYPERLINK`` / ``WEBSERVICE``). The audit data we export
       contains operator-influenced or agent-influenced text
       (``context_summary`` includes ``tool=<name>`` where name
       originated in a tool call). Without this guard, an agent
       that was tricked into calling a tool named
       ``=HYPERLINK("evil.com","click")`` would produce a CSV that
       phishes anyone opening it in Excel. Prefix those characters
       with a single-quote so the cell renders as literal text.

    Writing this inline beats pulling in csv.writer because we're
    streaming strings, not writing to a file.
    """
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in _FORMULA_PREFIXES:
        s = "'" + s
    if any(c in s for c in (",", '"', "\n", "\r")):
        return '"' + s.replace('"', '""') + '"'
    return s


def _format_row(row: dict[str, object]) -> str:
    triggered = row.get("triggered_rule_ids") or []
    findings = row.get("findings") or []
    # Flatten parallel arrays: codes joined with ``|`` so a single
    # audit row stays a single CSV row, and operators can still split.
    codes = "|".join(
        str(f.get("code", ""))
        for f in findings
        if isinstance(f, dict)
    )
    sevs = "|".join(
        str(f.get("severity", ""))
        for f in findings
        if isinstance(f, dict)
    )
    triggered_flat = "|".join(str(t) for t in triggered)
    fields = [
        _csv_escape(row.get("id")),
        _csv_escape(row.get("created_at")),
        _csv_escape(row.get("tenant_id")),
        _csv_escape(row.get("coworker_id")),
        _csv_escape(row.get("conversation_id")),
        _csv_escape(row.get("job_id")),
        _csv_escape(row.get("stage")),
        _csv_escape(row.get("verdict_action")),
        _csv_escape(triggered_flat),
        _csv_escape(codes),
        _csv_escape(sevs),
        _csv_escape(row.get("context_summary")),
    ]
    return ",".join(fields) + "\n"


@router.get("/tenants/{tid}/safety/decisions.csv")
async def export_safety_decisions_csv(
    tid: str,
    user: AdminUser,
    from_ts: str | None = None,
    to_ts: str | None = None,
    verdict_action: str | None = None,
    coworker_id: str | None = None,
    stage: str | None = None,
) -> StreamingResponse:
    """Stream safety_decisions as CSV.

    Cross-tenant access → 403. Uses a Postgres cursor so a 100k-row
    export stays in constant memory; the response starts flowing to
    the client as soon as the first chunk is ready, without waiting
    for the full query.

    Columns are the flat view defined in ``_CSV_COLUMNS``; full JSON
    for any row is available via ``GET /safety/decisions/{id}``
    (not yet implemented, planned for P1.5 admin UI).
    """
    if tid != user.tenant_id:
        raise HTTPException(status_code=403, detail="tenant scope mismatch")

    async def _generate() -> AsyncIterator[bytes]:
        yield (",".join(_CSV_COLUMNS) + "\n").encode("utf-8")
        async for chunk in pg.stream_safety_decisions(
            tid,
            from_ts=from_ts,
            to_ts=to_ts,
            verdict_action=verdict_action,
            coworker_id=coworker_id,
            stage=stage,
        ):
            for row in chunk:
                yield _format_row(row).encode("utf-8")

    today = datetime.now(UTC).strftime("%Y%m%d")
    filename = f"safety-decisions-{tid}-{today}.csv"
    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
