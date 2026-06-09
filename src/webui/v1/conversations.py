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

import base64
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import asyncpg
from fastapi import APIRouter, Depends, Query, Response

from rolemesh.db import (
    count_conversations_for_coworker,
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
    ConversationPage,
    Message,
    MessagePage,
)
from webui.v1._pagination import DEFAULT_PAGE_LIMIT, LimitParam, OffsetParam
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
    response_model=ConversationPage,
)
async def list_coworker_conversations(
    coworker_id: str,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
    offset: OffsetParam = 0,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ConversationPage:
    """List conversations for a coworker, ordered by ``created_at`` (paged)."""
    # USE/SEE enforcement: a member may not enumerate conversations of a
    # coworker they cannot see (another member's private one) — 404.
    await _get_coworker_or_404(coworker_id, user.tenant_id, user=user)
    convs = await get_conversations_for_coworker(
        coworker_id, tenant_id=user.tenant_id, limit=limit, offset=offset,
    )
    total = await count_conversations_for_coworker(
        coworker_id, tenant_id=user.tenant_id,
    )
    return ConversationPage(
        items=[_conversation_to_response(c) for c in convs],
        total=total,
        limit=limit,
        offset=offset,
    )


@coworker_conversations_router.post(
    "/{coworker_id}/conversations",
    response_model=Conversation,
    status_code=201,
)
async def create_coworker_conversation(
    coworker_id: str,
    body: ConversationCreate,
    user: AuthenticatedUser = Depends(require_action("coworker.use")),
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
        user_id=user.user_id,
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
    user: AuthenticatedUser = Depends(require_action("coworker.use")),
) -> Response:
    """Delete a conversation; FK ON DELETE CASCADE removes messages and runs.

    Per design §3 "DELETE semantics" table, conversations are owned by
    coworkers and their children (messages, runs) cascade. The
    pre-check 404 keeps the response idempotent — a second DELETE
    of the same id surfaces as 404 rather than 204, which matches
    the SPA's expectation when retrying.
    """
    await _get_conversation_or_404(conversation_id, user.tenant_id)
    await delete_conversation(conversation_id, tenant_id=user.tenant_id)
    return Response(status_code=204)


def _encode_message_cursor(ts_iso: str, msg_id: str) -> str:
    """Opaque cursor = base64("<timestamp_iso>|<message_id>")."""
    return base64.urlsafe_b64encode(f"{ts_iso}|{msg_id}".encode()).decode()


def _decode_message_cursor(cursor: str) -> tuple[str, str]:
    """Inverse of :func:`_encode_message_cursor`. Raises ValueError on junk."""
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    ts_iso, sep, msg_id = raw.partition("|")
    if not sep or not ts_iso or not msg_id:
        raise ValueError("malformed cursor")
    return ts_iso, msg_id


@conversations_router.get(
    "/{conversation_id}/messages",
    response_model=MessagePage,
)
async def list_conversation_messages(
    conversation_id: str,
    before: str | None = Query(
        default=None,
        description=(
            "Opaque cursor from a previous page's next_cursor; returns the "
            "page of messages immediately OLDER than it."
        ),
    ),
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MessagePage:
    """Return persisted messages for a conversation (cursor-paginated).

    Messages page with a ``(timestamp, id)`` cursor rather than
    offset/limit — chat history is append-only and read "load older",
    so an offset would shift or duplicate rows as new messages arrive
    mid-scroll. ``items`` come back oldest-first (display order); when
    ``has_more`` is true, pass ``next_cursor`` as ``before`` to fetch the
    next older page.

    The wire-level ``role`` projects ``is_from_me`` / ``is_bot_message``
    onto ``user`` / ``assistant``. Token usage and sender metadata are
    intentionally omitted — the WS event stream surfaces live usage;
    persisted history only needs role / content / timestamp for re-render.
    """
    await _get_conversation_or_404(conversation_id, user.tenant_id)

    where = "conversation_id = $1::uuid AND tenant_id = $2::uuid"
    params: list[object] = [conversation_id, user.tenant_id]
    if before is not None:
        try:
            cur_ts, cur_id = _decode_message_cursor(before)
            # Bind the cursor timestamp as a datetime: asyncpg rejects a
            # plain str for a timestamptz parameter (DataError), which 400'd
            # a page's own valid next_cursor when walking older on page 2.
            cur_dt = datetime.fromisoformat(cur_ts)
        except ValueError:
            raise_error_response(
                "INVALID_CURSOR",
                "Malformed pagination cursor.",
                status_code=400,
            )
        # Keyset seek on (timestamp, id), newest-first: rows strictly older
        # than the cursor, with id as the tiebreak at an equal timestamp.
        params.extend((cur_dt, cur_id))
        where += (
            " AND (timestamp < $3::timestamptz"
            " OR (timestamp = $3::timestamptz AND id < $4::text))"
        )
    params.append(limit + 1)  # over-fetch one to detect has_more
    sql = (
        "SELECT id, content, timestamp, is_from_me, is_bot_message, "
        "run_id::text AS run_id FROM messages WHERE "
        + where
        + f" ORDER BY timestamp DESC, id DESC LIMIT ${len(params)}"
    )
    try:
        async with tenant_conn(user.tenant_id) as conn:
            rows_desc = await conn.fetch(sql, *params)
    except asyncpg.DataError:
        # Defensive belt: a malformed cursor is normally rejected above when
        # its timestamp fails to parse; this keeps any residual bad-data query
        # error a 400 rather than a 500.
        raise_error_response(
            "INVALID_CURSOR",
            "Malformed pagination cursor.",
            status_code=400,
        )

    has_more = len(rows_desc) > limit
    rows_desc = rows_desc[:limit]
    next_cursor: str | None = None
    if has_more and rows_desc:
        oldest = rows_desc[-1]
        next_cursor = _encode_message_cursor(
            oldest["timestamp"].isoformat() if oldest["timestamp"] else "",
            str(oldest["id"]),
        )

    items: list[Message] = []
    for row in reversed(rows_desc):  # oldest-first for display
        role = "assistant" if (row["is_from_me"] or row["is_bot_message"]) else "user"
        items.append(
            Message(
                id=row["id"],
                role=role,  # type: ignore[arg-type]
                content=row["content"] or "",
                timestamp=row["timestamp"].isoformat() if row["timestamp"] else "",
                run_id=row["run_id"],
            )
        )
    return MessagePage(items=items, has_more=has_more, next_cursor=next_cursor)


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


