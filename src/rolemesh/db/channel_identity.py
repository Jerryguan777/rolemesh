"""User ↔ IM channel identity linkage (v6.1 §P1.2 / §P1.4).

Two tables, four operations:

- ``link_tokens`` — short-lived one-shot tokens signed by WebUI and
  consumed atomically by an IM gateway. ``consume_link_token`` is the
  single check-and-mark statement; the row never carries a "claimed
  but not yet linked" intermediate state.

- ``user_channel_identities`` — the resulting (user, platform,
  channel_id) record. ``create_channel_identity`` raises
  ``UniqueViolationError`` on a collision so the caller can map it to
  the user-facing "this Telegram account is already linked elsewhere"
  reply. ``list_channel_identities_for_user`` and
  ``delete_channel_identity`` cover the WebUI status / unbind paths.

All callers should use these helpers; the raw tables are private to
the link-flow + admission code paths.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from rolemesh.core.types import ChannelIdentity
from rolemesh.db._pool import admin_conn, tenant_conn

if TYPE_CHECKING:
    import asyncpg

__all__ = [
    "consume_link_token",
    "create_channel_identity",
    "create_link_token",
    "delete_channel_identity",
    "list_channel_identities_for_user",
]


# ---------------------------------------------------------------------------
# Link tokens
# ---------------------------------------------------------------------------


# 22 URL-safe characters ≈ 132 bits of entropy. The design requires
# ≥ 22 chars; ``token_urlsafe(16)`` yields 22 chars from 16 random
# bytes (24 chars after base64 padding, then minus the trailing ``==``
# the URL-safe variant strips). Sufficient against guessing in the
# ~10 min validity window.
_TOKEN_NBYTES = 16


async def create_link_token(
    user_id: str,
    tenant_id: str,
    platform: str,
    ttl_seconds: int = 600,
) -> tuple[str, datetime]:
    """Mint a single-use linking token for the (user, platform) pair.

    Returns ``(token, expires_at)``. The caller hands the token to the
    user as either a deep-link payload (``t.me/<bot>?start=<token>``)
    or a copy-paste short code; the IM gateway consumes it via
    ``consume_link_token`` on the next inbound ``/start <token>``.

    Multiple in-flight tokens for the same user are deliberately
    allowed (the user might restart the flow); only the one the
    gateway actually receives is consumed, expired ones are inert.
    """
    token = secrets.token_urlsafe(_TOKEN_NBYTES)
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    async with admin_conn() as conn:
        # admin_conn (cross-tenant by design): link_tokens has no
        # current_tenant_id RLS predicate; the row's tenant_id is the
        # one we're inserting, written from the caller's authenticated
        # user context. Using tenant_conn would be a no-op here and
        # adds an unnecessary GUC round-trip.
        await conn.execute(
            """
            INSERT INTO link_tokens
                (token, user_id, tenant_id, platform, expires_at)
            VALUES ($1, $2::uuid, $3::uuid, $4, $5)
            """,
            token, user_id, tenant_id, platform, expires_at,
        )
    return token, expires_at


async def consume_link_token(token: str) -> tuple[str, str, str] | None:
    """Atomically mark a token used and return ``(user_id, tenant_id,
    platform)`` if it was unused AND unexpired at this exact moment.

    ``UPDATE ... WHERE used_at IS NULL AND expires_at > now()
    RETURNING ...`` is one statement; two concurrent ``/start
    <token>`` deliveries either land on the same row (one wins, the
    other gets an empty result) or on different connections that
    serialise through MVCC. Either way ``RETURNING`` only yields a row
    for the first successful caller.
    """
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            """
            UPDATE link_tokens
               SET used_at = now()
             WHERE token = $1
               AND used_at IS NULL
               AND expires_at > now()
            RETURNING user_id, tenant_id, platform
            """,
            token,
        )
    if row is None:
        return None
    return str(row["user_id"]), str(row["tenant_id"]), row["platform"]


# ---------------------------------------------------------------------------
# Channel identities
# ---------------------------------------------------------------------------


async def create_channel_identity(
    tenant_id: str,
    platform: str,
    channel_id: str,
    user_id: str,
) -> ChannelIdentity:
    """Insert one (user, platform, channel_id) link.

    Raises ``asyncpg.UniqueViolationError`` if a row already exists for
    ``(tenant_id, platform, channel_id)`` — the caller is expected to
    map that into the "this Telegram account is already linked to
    another RoleMesh account" user reply. The DB-level UNIQUE is the
    race guard against two concurrent ``/start <token>`` flows binding
    the same Telegram account.
    """
    async with admin_conn() as conn:
        # admin_conn: this is the only writer of the table and the row
        # carries its own tenant_id; RLS on user_channel_identities is
        # unnecessary because every read goes through a tenant-scoped
        # SELECT (see ``resolve_user_from_channel_sender`` /
        # ``list_channel_identities_for_user``).
        row = await conn.fetchrow(
            """
            INSERT INTO user_channel_identities
                (tenant_id, platform, channel_id, user_id)
            VALUES ($1::uuid, $2, $3, $4::uuid)
            RETURNING id, tenant_id, platform, channel_id, user_id, created_at
            """,
            tenant_id, platform, channel_id, user_id,
        )
    assert row is not None
    return _record_to_identity(row)


async def list_channel_identities_for_user(
    user_id: str, tenant_id: str
) -> list[ChannelIdentity]:
    """All identities linked by ``user_id`` within ``tenant_id``.

    Used by the WebUI settings page so the user can see which IM
    accounts are already connected and unbind individual ones. The
    ``tenant_id`` filter is on the query — passing a foreign tenant
    yields zero rows (which is also what RLS would do).
    """
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT id, tenant_id, platform, channel_id, user_id, created_at
              FROM user_channel_identities
             WHERE user_id = $1::uuid AND tenant_id = $2::uuid
             ORDER BY created_at DESC
            """,
            user_id, tenant_id,
        )
    return [_record_to_identity(r) for r in rows]


async def delete_channel_identity(
    identity_id: str, user_id: str, tenant_id: str
) -> bool:
    """Unbind one identity row; returns True iff a row was deleted.

    Tenant + user filter on the query so a guess at someone else's
    identity_id 404s instead of leaking row existence. Caller side
    (WebUI handler) maps False to 404.
    """
    async with tenant_conn(tenant_id) as conn:
        result = await conn.execute(
            """
            DELETE FROM user_channel_identities
             WHERE id = $1::uuid
               AND user_id = $2::uuid
               AND tenant_id = $3::uuid
            """,
            identity_id, user_id, tenant_id,
        )
    return result == "DELETE 1"


def _record_to_identity(row: asyncpg.Record) -> ChannelIdentity:
    created = row["created_at"]
    return ChannelIdentity(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        platform=row["platform"],
        channel_id=row["channel_id"],
        created_at=created.isoformat() if created else "",
    )
