"""Admin API endpoints for tenant, user, agent, binding, conversation, and task management."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.core.group_folder import is_valid_group_folder
from rolemesh.core.types import McpServerConfig
from rolemesh.db import pg
from webui.dependencies import require_manage_agents, require_manage_tenant, require_manage_users
from webui.schemas import (
    AgentCreate,
    AgentDetailResponse,
    AgentResponse,
    AgentSummary,
    AgentUpdate,
    AssignRequest,
    BindingCreate,
    BindingResponse,
    BindingUpdate,
    ConversationCreate,
    ConversationResponse,
    TaskResponse,
    TenantResponse,
    TenantUpdate,
    UserCreate,
    UserDetailResponse,
    UserResponse,
    UserUpdate,
)

if TYPE_CHECKING:
    from rolemesh.core.types import ChannelBinding, Conversation, Coworker, ScheduledTask, Tenant, User

# Annotated dependency types (avoids B008 lint warnings)
OwnerUser = Annotated[AuthenticatedUser, Depends(require_manage_tenant)]
AdminUser = Annotated[AuthenticatedUser, Depends(require_manage_agents)]
UserManager = Annotated[AuthenticatedUser, Depends(require_manage_users)]

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
        tools=[{"name": t.name, "type": t.type, "url": t.url, "headers": t.headers} for t in cw.tools],
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
    cw = await pg.get_coworker(agent_id)
    if cw is None or cw.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    return cw


def _parse_tools(tools_dicts: list[dict[str, object]]) -> list[McpServerConfig]:
    """Convert a list of tool dicts to McpServerConfig objects."""
    return [
        McpServerConfig(
            name=str(t["name"]),
            type=str(t.get("type", "http")),
            url=str(t["url"]),
            headers=dict(t.get("headers") or {}),  # type: ignore[arg-type]
        )
        for t in tools_dicts
    ]


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
    target = await pg.get_user(user_id)
    if target is None or target.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="User not found")
    agents = await pg.get_agents_for_user(user_id)
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
    target = await pg.get_user(user_id)
    if target is None or target.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="User not found")
    if body.role == "owner" and user.role != "owner":
        raise HTTPException(status_code=403, detail="Only owners can assign owner role")
    updated = await pg.update_user(user_id, name=body.name, email=body.email, role=body.role)
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
    target = await pg.get_user(user_id)
    if target is None or target.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="User not found")
    await pg.delete_user(user_id)


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
    return _coworker_to_response(cw)


@router.get("/agents/{agent_id}", response_model=AgentDetailResponse)
async def get_agent_detail(
    agent_id: str,
    user: AdminUser,
) -> AgentDetailResponse:
    cw = await _get_agent_or_404(agent_id, user.tenant_id)
    bindings = await pg.get_channel_bindings_for_coworker(agent_id)
    conversations = await pg.get_conversations_for_coworker(agent_id)
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
    return _coworker_to_response(updated)


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str,
    user: AdminUser,
) -> None:
    await _get_agent_or_404(agent_id, user.tenant_id)
    await pg.delete_coworker(agent_id)


# ---------------------------------------------------------------------------
# Assignment endpoints (admin+)
# ---------------------------------------------------------------------------


@router.get("/agents/{agent_id}/users", response_model=list[UserResponse])
async def list_assigned_users(
    agent_id: str,
    user: AdminUser,
) -> list[UserResponse]:
    await _get_agent_or_404(agent_id, user.tenant_id)
    users = await pg.get_users_for_agent(agent_id)
    return [_user_to_response(u) for u in users]


@router.post("/agents/{agent_id}/assign", status_code=204)
async def assign_agent(
    agent_id: str,
    body: AssignRequest,
    user: AdminUser,
) -> None:
    await _get_agent_or_404(agent_id, user.tenant_id)
    target = await pg.get_user(body.user_id)
    if target is None or target.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="User not found")
    await pg.assign_agent_to_user(body.user_id, agent_id, user.tenant_id)


@router.delete("/agents/{agent_id}/assign/{user_id}", status_code=204)
async def unassign_agent(
    agent_id: str,
    user_id: str,
    user: AdminUser,
) -> None:
    await _get_agent_or_404(agent_id, user.tenant_id)
    target = await pg.get_user(user_id)
    if target is None or target.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="User not found")
    await pg.unassign_agent_from_user(user_id, agent_id)


# ---------------------------------------------------------------------------
# Binding endpoints (admin+)
# ---------------------------------------------------------------------------


@router.get("/agents/{agent_id}/bindings", response_model=list[BindingResponse])
async def list_bindings(
    agent_id: str,
    user: AdminUser,
) -> list[BindingResponse]:
    await _get_agent_or_404(agent_id, user.tenant_id)
    bindings = await pg.get_channel_bindings_for_coworker(agent_id)
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
    binding = await pg.get_channel_binding(binding_id)
    if binding is None or binding.coworker_id != agent_id:
        raise HTTPException(status_code=404, detail="Binding not found")
    updated = await pg.update_channel_binding(
        binding_id,
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
    binding = await pg.get_channel_binding(binding_id)
    if binding is None or binding.coworker_id != agent_id:
        raise HTTPException(status_code=404, detail="Binding not found")
    await pg.delete_channel_binding(binding_id)


# ---------------------------------------------------------------------------
# Conversation endpoints (admin+)
# ---------------------------------------------------------------------------


@router.get("/agents/{agent_id}/conversations", response_model=list[ConversationResponse])
async def list_conversations(
    agent_id: str,
    user: AdminUser,
) -> list[ConversationResponse]:
    await _get_agent_or_404(agent_id, user.tenant_id)
    conversations = await pg.get_conversations_for_coworker(agent_id)
    return [_conversation_to_response(c) for c in conversations]


@router.post("/agents/{agent_id}/conversations", response_model=ConversationResponse, status_code=201)
async def create_conversation(
    agent_id: str,
    body: ConversationCreate,
    user: AdminUser,
) -> ConversationResponse:
    await _get_agent_or_404(agent_id, user.tenant_id)
    # Verify the binding belongs to this agent
    binding = await pg.get_channel_binding(body.channel_binding_id)
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
    conv = await pg.get_conversation(conversation_id)
    if conv is None or conv.coworker_id != agent_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await pg.delete_conversation(conversation_id)


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
    tasks = await pg.get_tasks_for_coworker(agent_id)
    return [_task_to_response(t) for t in tasks]


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(
    task_id: str,
    user: AdminUser,
) -> None:
    task = await pg.get_task_by_id(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    # Verify the task's agent belongs to user's tenant
    cw = await pg.get_coworker(task.coworker_id)
    if cw is None or cw.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Task not found")
    await pg.delete_task(task_id)
