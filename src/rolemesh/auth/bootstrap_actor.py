"""Resolve the actor user UUID used in audit-FK writes.

Audit tables (``approval_audit_log.actor_user_id``,
``safety_rules_audit.actor_user_id``) declare a UUID FK to
``users(id)``. The web layer's ``AuthenticatedUser.user_id`` can be:

* a real UUID (an authenticated, persisted user); or
* the literal string ``"bootstrap"`` — the in-memory pseudo-user the
  REST/WS layer hands out when only ``ADMIN_BOOTSTRAP_TOKEN`` is set.

Writing the bootstrap literal into the FK column would fail at the
type cast (and even if it did not, it would violate the FK invariant
because there is no ``users`` row for "bootstrap"). This module
provides a single resolver every audit-write site is expected to go
through:

* Real UUID → returned unchanged.
* Bootstrap literal → look up the tenant's first ``owner`` user and
  return that UUID.
* Bootstrap literal + tenant has no owner → raise
  ``BootstrapActorError`` (HTTP 503). Better to fail loudly than to
  silently lose audit provenance.

INV-4 is the contract: the bootstrap pseudo-user must never be
silently coerced into the audit FK; either a real owner stands in,
or the request is rejected with a deterministic error code.
"""

from __future__ import annotations

import uuid
from typing import Final

from rolemesh.db._pool import admin_conn

BOOTSTRAP_USER_LITERAL: Final[str] = "bootstrap"


class BootstrapActorError(Exception):
    """Raised when audit-write needs a real actor but only the bootstrap
    pseudo-user is available and the tenant has no owner.

    FastAPI handler maps this to HTTP 503 with the ``code`` field.
    """

    code: Final[str] = "BOOTSTRAP_NEEDS_TENANT_OWNER"
    status: Final[int] = 503

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        super().__init__(
            f"tenant {tenant_id!r} has no owner user; cannot resolve a "
            f"real actor for audit FK while running under the bootstrap "
            f"pseudo-user"
        )


def _is_real_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


async def resolve_actor_user_id(
    tenant_id: str, current_user_id: str
) -> str:
    """Return a UUID suitable for an audit FK write.

    ``tenant_id`` scopes the owner lookup; ``current_user_id`` is the
    value the web layer attached to the request.

    A real UUID is returned verbatim — no DB round-trip. The bootstrap
    fall-through path performs exactly one indexed query (oldest owner
    in this tenant). Repeated calls within the same request are cheap
    enough that we do not cache here; callers needing a single value
    across a multi-write transaction may resolve once and reuse.
    """
    if _is_real_uuid(current_user_id):
        return current_user_id
    # Anything that is not a real UUID is treated as the bootstrap
    # literal. We deliberately do NOT compare to ``"bootstrap"`` by
    # string; a future second pseudo-user (e.g. ``"system"``) should
    # land on the same fail-safe path rather than slip through as a
    # real UUID-string cast.
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            """
            SELECT id FROM users
            WHERE tenant_id = $1::uuid AND role = 'owner'
            ORDER BY created_at ASC
            LIMIT 1
            """,
            tenant_id,
        )
    if row is None:
        raise BootstrapActorError(tenant_id)
    return str(row["id"])
