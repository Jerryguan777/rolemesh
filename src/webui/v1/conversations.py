"""``/api/v1/conversations`` and ``/api/v1/coworkers/{id}/conversations``.

Web-chat conversations are server-driven: the SPA does not know
about ``channel_bindings`` or ``channel_chat_id``. POST auto-creates
the coworker's ``web`` channel binding (idempotent) and mints a
fresh ``channel_chat_id`` so the client only sees the
``conversation_id`` it needs to open the WS stream.

Tenant scoping uses the INV-1 belt-and-braces pattern: the
underlying DB helpers already set the RLS GUC on their connection
and include an explicit ``tenant_id`` predicate. Handler code adds
no second filter; instead it asks the DB helper to scope, and
404s when the helper returns ``None``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import asyncpg
from fastapi import APIRouter, Depends, Response

from rolemesh.db import (
    create_channel_binding,
    create_conversation,
    delete_conversation,
    get_channel_binding_for_coworker,
    get_conversation,
    get_conversations_for_coworker,
    list_requests_for_conversation,
    tenant_conn,
)
from webui.dependencies import get_current_user, require_action
from webui.schemas_v1 import (
    ApprovalRequest,
    Conversation,
    ConversationCreate,
    Message,
)
from webui.v1.approvals import _request_to_response
from webui.v1.coworkers import _get_coworker_or_404
from webui.v1.errors import raise_error_response

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser

coworker_conversations_router = APIRouter(
    prefix="/coworkers", tags=["Conversations"]
)
conversations_router = APIRouter(
    prefix="/conversations", tags=["Conversations"]
)


def _conversation_to_response(conv: object) -> Conversation:
    """Project a ``rolemesh.core.types.Conversation`` to the wire model."""
    return Conversation(
        id=conv.id,  # type: ignore[attr-defined]
        tenant_id=conv.tenant_id,  # type: ignore[attr-defined]
        coworker_id=conv.coworker_id,  # type: ignore[attr-defined]
        channel_binding_id=conv.channel_binding_id,  # type: ignore[attr-defined]
        channel_chat_id=conv.channel_chat_id,  # type: ignore[attr-defined]
        name=conv.name,  # type: ignore[attr-defined]
        created_at=conv.created_at,  # type: ignore[attr-defined]
    )


async def _ensure_web_binding(coworker_id: str, tenant_id: str) -> str:
    """Return the binding id for the coworker's ``web`` channel.

    Idempotent: returns the existing binding when present, creates
    one when missing. The SPA never sees this id directly — the WS
    stream uses ``conversation_id`` — but the conversations row
    needs a binding to satisfy the NOT NULL FK.
    """
    existing = await get_channel_binding_for_coworker(
        coworker_id, "web", tenant_id=tenant_id
    )
    if existing is not None:
        return existing.id
    binding = await create_channel_binding(
        coworker_id=coworker_id,
        tenant_id=tenant_id,
        channel_type="web",
    )
    return binding.id


async def _get_conversation_or_404(
    conversation_id: str, tenant_id: str
) -> object:
    try:
        conv = await get_conversation(conversation_id, tenant_id=tenant_id)
    except asyncpg.DataError:
        conv = None
    if conv is None:
        raise_error_response(
            "NOT_FOUND",
            "Conversation not found.",
            status_code=404,
            details={"conversation_id": conversation_id},
        )
    return conv


# ---------------------------------------------------------------------------
# /api/v1/coworkers/{id}/conversations
# ---------------------------------------------------------------------------


@coworker_conversations_router.get(
    "/{coworker_id}/conversations",
    response_model=list[Conversation],
)
async def list_coworker_conversations(
    coworker_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[Conversation]:
    """List conversations for a coworker, ordered by ``created_at``."""
    # USE/SEE enforcement: a member may not enumerate conversations of a
    # coworker they cannot see (another member's private one) — 404.
    await _get_coworker_or_404(coworker_id, user.tenant_id, user=user)
    convs = await get_conversations_for_coworker(
        coworker_id, tenant_id=user.tenant_id
    )
    return [_conversation_to_response(c) for c in convs]


@coworker_conversations_router.post(
    "/{coworker_id}/conversations",
    response_model=Conversation,
    status_code=201,
)
async def create_coworker_conversation(
    coworker_id: str,
    body: ConversationCreate,
    user: AuthenticatedUser = Depends(require_action("agent.use")),
) -> Conversation:
    """Create a new web conversation under a coworker.

    The SPA passes at most a display ``name``. The server auto-
    provisions the ``web`` channel binding (if not already present)
    and generates a unique ``channel_chat_id``. All conversations are
    1:1 — the agent always responds.
    """
    # USE enforcement (feat/roles PR3 feed-forward): a member must NOT be
    # able to open a conversation against another member's PRIVATE
    # coworker. ``_get_coworker_or_404(user=...)`` collapses not-visible
    # to 404 so existence is not leaked; a shared coworker or the
    # member's own private one passes.
    cw = await _get_coworker_or_404(coworker_id, user.tenant_id, user=user)
    binding_id = await _ensure_web_binding(cw.id, user.tenant_id)  # type: ignore[attr-defined]
    chat_id = str(uuid.uuid4())
    conv = await create_conversation(
        tenant_id=user.tenant_id,
        coworker_id=cw.id,  # type: ignore[attr-defined]
        channel_binding_id=binding_id,
        channel_chat_id=chat_id,
        name=body.name,
        user_id=(
            user.user_id if _looks_like_uuid(user.user_id) else None
        ),
    )
    return _conversation_to_response(conv)


# ---------------------------------------------------------------------------
# /api/v1/conversations/{id}
# ---------------------------------------------------------------------------


@conversations_router.get(
    "/{conversation_id}", response_model=Conversation
)
async def get_conversation_endpoint(
    conversation_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Conversation:
    conv = await _get_conversation_or_404(conversation_id, user.tenant_id)
    return _conversation_to_response(conv)


@conversations_router.delete("/{conversation_id}", status_code=204)
async def delete_conversation_endpoint(
    conversation_id: str,
    user: AuthenticatedUser = Depends(require_action("agent.use")),
) -> Response:
    """Delete a conversation; FK ON DELETE CASCADE removes messages and runs.

    Per design §3 "DELETE 语义" table, conversations are owned by
    coworkers and their children (messages, runs) cascade. The
    pre-check 404 keeps the response idempotent — a second DELETE
    of the same id surfaces as 404 rather than 204, which matches
    the SPA's expectation when retrying.
    """
    await _get_conversation_or_404(conversation_id, user.tenant_id)
    await delete_conversation(conversation_id, tenant_id=user.tenant_id)
    return Response(status_code=204)


@conversations_router.get(
    "/{conversation_id}/messages",
    response_model=list[Message],
)
async def list_conversation_messages(
    conversation_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[Message]:
    """Return persisted messages for a conversation, ordered by timestamp.

    The wire-level ``role`` projects ``is_from_me`` / ``is_bot_message``
    onto ``user`` / ``assistant``. Token usage and sender metadata are
    intentionally omitted from the wire schema — the WS event stream
    surfaces live usage; persisted history only needs role / content /
    timestamp for re-render.
    """
    await _get_conversation_or_404(conversation_id, user.tenant_id)
    async with tenant_conn(user.tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT id,
                   content,
                   timestamp,
                   is_from_me,
                   is_bot_message,
                   run_id::text AS run_id
              FROM messages
             WHERE conversation_id = $1::uuid
               AND tenant_id       = $2::uuid
             ORDER BY timestamp
            """,
            conversation_id,
            user.tenant_id,
        )
    out: list[Message] = []
    for row in rows:
        role = "assistant" if (row["is_from_me"] or row["is_bot_message"]) else "user"
        out.append(
            Message(
                id=row["id"],
                role=role,  # type: ignore[arg-type]
                content=row["content"] or "",
                timestamp=row["timestamp"].isoformat() if row["timestamp"] else "",
                run_id=row["run_id"],
            )
        )
    return out


@conversations_router.get(
    "/{conversation_id}/approval-requests",
    response_model=list[ApprovalRequest],
)
async def list_conversation_approval_requests(
    conversation_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[ApprovalRequest]:
    """Return the conversation's full HITL approval record (all states).

    Unlike the tenant-wide ``GET /api/v1/approval-requests`` (pending only, for
    the inbox), this returns pending AND resolved requests oldest-first so the
    chat re-renders resolved ✅/❌ cards inline on reload, not just in-flight
    ones. Tenant-scoped: a conversation outside the caller's tenant is 404.
    """
    await _get_conversation_or_404(conversation_id, user.tenant_id)
    rows = await list_requests_for_conversation(
        conversation_id, tenant_id=user.tenant_id
    )
    return [_request_to_response(r) for r in rows]


def _looks_like_uuid(value: str) -> bool:
    """Duplicate of the guard in :mod:`webui.v1.coworkers` (same purpose)."""
    if len(value) != 36:
        return False
    parts = value.split("-")
    return len(parts) == 5 and all(
        all(c in "0123456789abcdefABCDEF" for c in p) for p in parts
    )
