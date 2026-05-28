"""v6.1 §P1.4 — link_tokens / user_channel_identities behaviour.

Companion to ``test_channel_identity_schema.py`` (which nails column
shape + constraints). This file targets the *flow* the IM gateway and
WebUI handlers depend on:

- T1.1: atomic single-use consumption under concurrency.
- T1.2: expiry + already-used rejection.
- T1.4: unbind + re-link.
- Plus collision behaviour for ``create_channel_identity``.

No mocks; testcontainer Postgres. Concurrency uses
``asyncio.gather`` over independent admin pool connections so the
``UPDATE ... RETURNING`` race lives in the actual asyncpg + PG MVCC
stack we'll see in production.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from rolemesh.db import (
    _get_admin_pool,
    consume_link_token,
    create_channel_identity,
    create_link_token,
    create_tenant,
    create_user,
    delete_channel_identity,
    list_channel_identities_for_user,
    resolve_user_from_channel_sender,
)

pytestmark = pytest.mark.usefixtures("test_db")


async def _seed_user(slug_tag: str) -> tuple[str, str]:
    t = await create_tenant(name="T", slug=f"{slug_tag}-{uuid.uuid4().hex[:6]}")
    u = await create_user(
        tenant_id=t.id, name="U",
        email=f"u-{uuid.uuid4().hex[:6]}@x.com",
    )
    return t.id, u.id


# ---------------------------------------------------------------------------
# create_link_token — shape
# ---------------------------------------------------------------------------


async def test_create_link_token_returns_long_url_safe_string() -> None:
    """Tokens must be ≥ 22 URL-safe characters (design §P1.2). A
    weaker token would burn entropy on guessing during the 10-minute
    validity window.
    """
    tid, uid = await _seed_user("tok-shape")
    token, exp = await create_link_token(uid, tid, "telegram")
    # Length / character set probes — URL-safe base64 is [A-Za-z0-9_-].
    assert len(token) >= 22, f"token too short: {len(token)} chars"
    assert all(c.isalnum() or c in "_-" for c in token), token
    # expires_at is in the future (allow modest scheduler skew).
    assert exp > datetime.now(UTC)


async def test_create_link_token_uniqueness_under_burst() -> None:
    """Tokens are random; two consecutive calls must not collide.
    Without uniqueness one user could not reissue an in-flight token.
    """
    tid, uid = await _seed_user("tok-uniq")
    seen: set[str] = set()
    for _ in range(20):
        token, _ = await create_link_token(uid, tid, "telegram")
        assert token not in seen, "secrets.token_urlsafe collision — RNG broken?"
        seen.add(token)


# ---------------------------------------------------------------------------
# consume_link_token — T1.1, T1.2
# ---------------------------------------------------------------------------


async def test_consume_link_token_first_caller_wins_concurrent_race() -> None:
    """T1.1 — Two concurrent ``/start <token>`` deliveries on the
    same token result in **exactly one** successful consumer.

    The check-and-mark is one statement; MVCC + UNIQUE serialisation
    are what guarantee it. We exercise that by issuing both UPDATEs
    on independent connections through ``asyncio.gather`` so the
    race actually overlaps in time.
    """
    tid, uid = await _seed_user("tok-race")
    token, _ = await create_link_token(uid, tid, "telegram")

    # Two parallel consumers on independent pool acquisitions.
    results = await asyncio.gather(
        consume_link_token(token),
        consume_link_token(token),
    )
    successes = [r for r in results if r is not None]
    failures = [r for r in results if r is None]
    assert len(successes) == 1, f"expected 1 winner, got {len(successes)}: {results}"
    assert len(failures) == 1
    user_id, tenant_id, platform = successes[0]
    assert user_id == uid
    assert tenant_id == tid
    assert platform == "telegram"


async def test_consume_link_token_rejects_already_used() -> None:
    """T1.2a — A second consume on a previously consumed token returns
    None. Otherwise a leaked/replayed deep-link could re-bind."""
    tid, uid = await _seed_user("tok-used")
    token, _ = await create_link_token(uid, tid, "telegram")
    first = await consume_link_token(token)
    assert first is not None
    second = await consume_link_token(token)
    assert second is None, "replay must not yield a second binding"


async def test_consume_link_token_rejects_expired() -> None:
    """T1.2b — A token whose ``expires_at`` is in the past returns
    None. We sidestep the 10-minute default by issuing with a -1s TTL
    so ``expires_at`` lands strictly before ``now()`` at consume time.
    """
    tid, uid = await _seed_user("tok-exp")
    # ttl_seconds=-1 ⇒ expires_at = now() - 1s; row is born expired.
    token, _ = await create_link_token(uid, tid, "telegram", ttl_seconds=-1)
    result = await consume_link_token(token)
    assert result is None, "expired token must not consume"


async def test_consume_link_token_unknown_token_returns_none() -> None:
    """A bogus token returns None — never a row from a different
    user. Defends against typo + targeted guess in equal measure."""
    result = await consume_link_token("not-a-real-token-" + uuid.uuid4().hex)
    assert result is None


async def test_consume_link_token_boundary_at_expiry() -> None:
    """A token whose ``expires_at == now()`` must be rejected. Catches
    the off-by-one mutation ``expires_at > now() → expires_at >= now()``
    (the spec is strict-greater so a token expiring this instant is
    *not* consumable).
    """
    tid, uid = await _seed_user("tok-bnd")
    # ttl=0 ⇒ expires_at == now() at insert, which is ≤ now() at
    # consume time. The strict-> check must fail.
    token, _ = await create_link_token(uid, tid, "telegram", ttl_seconds=0)
    result = await consume_link_token(token)
    assert result is None, "token at the boundary must be rejected"


# ---------------------------------------------------------------------------
# create_channel_identity — collision and tenant scoping
# ---------------------------------------------------------------------------


async def test_create_channel_identity_collision_raises_unique_violation() -> None:
    """Two link attempts for the same Telegram account must collide.
    The caller (Telegram gateway) maps this to the user-facing
    "already linked" reply.
    """
    tid, uid_a = await _seed_user("ci-col-a")
    _, uid_b = await _seed_user("ci-col-b")
    # Same tenant so the UNIQUE bites; cross-tenant is covered by the
    # schema test file.
    await create_channel_identity(tid, "telegram", "777", uid_a)
    with pytest.raises(asyncpg.UniqueViolationError):
        await create_channel_identity(tid, "telegram", "777", uid_b)


# ---------------------------------------------------------------------------
# list / delete + unbind→re-link round trip (T1.4)
# ---------------------------------------------------------------------------


async def test_list_channel_identities_scoped_to_user_and_tenant() -> None:
    """The lister filters by both ``user_id`` and ``tenant_id`` so a
    Web caller cannot read another user's links. A future drift that
    drops the user predicate would surface as the second user seeing
    the first's link in the assertion below.
    """
    tid_a, uid_a = await _seed_user("list-a")
    tid_b, uid_b = await _seed_user("list-b")
    await create_channel_identity(tid_a, "telegram", "alpha", uid_a)
    await create_channel_identity(tid_b, "telegram", "beta", uid_b)
    a_links = await list_channel_identities_for_user(uid_a, tid_a)
    b_links = await list_channel_identities_for_user(uid_b, tid_b)
    assert {l.channel_id for l in a_links} == {"alpha"}
    assert {l.channel_id for l in b_links} == {"beta"}


async def test_unbind_then_relink_with_new_token_succeeds() -> None:
    """T1.4 — After DELETEing an identity, the user can mint a fresh
    link_token, consume it, and bind the same Telegram account again.
    Without this, a stray click on "disconnect" would soft-brick the
    user.
    """
    tid, uid = await _seed_user("relink")
    identity = await create_channel_identity(tid, "telegram", "12345", uid)
    # Unbind.
    assert await delete_channel_identity(identity.id, uid, tid) is True
    # Mint + consume a brand-new token and re-bind.
    token, _ = await create_link_token(uid, tid, "telegram")
    consumed = await consume_link_token(token)
    assert consumed is not None
    new_identity = await create_channel_identity(tid, "telegram", "12345", uid)
    assert new_identity.id != identity.id
    # And only the new row is live.
    links = await list_channel_identities_for_user(uid, tid)
    assert [l.id for l in links] == [new_identity.id]


async def test_delete_channel_identity_nulls_corresponding_conv_user_id() -> None:
    """v6.1 §P1.4 / F1 — unbind a Telegram identity → NULL the
    ``conv.user_id`` on every 1:1 conv pinned to that
    ``(tenant, channel_chat_id, telegram-binding)`` tuple that was
    carrying the now-unbound user.

    Without this, a different RoleMesh user who later relinks the
    same Telegram chat (employee handover / shared device) inherits
    the prior owner's stamp on the conv row; the admission layer's
    ``if conv.user_id is None`` short-circuit then never re-stamps,
    and the agent ends up running B's request under A's identity —
    breaks audit attribution and Phase-2 self-approval.
    """
    import json
    tid, uid = await _seed_user("nullify-conv")
    # Need real Coworker + channel_binding + conversation rows; the
    # raw inserts below mirror what the orchestrator/webui would
    # create. SQL not the high-level helpers to keep the test focused
    # on the DELETE branch only.
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        cw_id = await conn.fetchval(
            "INSERT INTO coworkers (tenant_id, name, folder) "
            "VALUES ($1::uuid, $2, $3) RETURNING id",
            tid, "CW", f"folder-{uuid.uuid4().hex[:6]}",
        )
        binding_id = await conn.fetchval(
            "INSERT INTO channel_bindings "
            "(coworker_id, tenant_id, channel_type, credentials) "
            "VALUES ($1::uuid, $2::uuid, $3, $4::jsonb) RETURNING id",
            cw_id, tid, "telegram", json.dumps({"bot_token": "t"}),
        )
        conv_id = await conn.fetchval(
            "INSERT INTO conversations "
            "(tenant_id, coworker_id, channel_binding_id, "
            " channel_chat_id, user_id) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5::uuid) "
            "RETURNING id",
            tid, cw_id, binding_id, "12345", uid,
        )
    # Also link the user → channel binding.
    identity = await create_channel_identity(tid, "telegram", "12345", uid)

    # Sanity: conv carries the user_id.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM conversations WHERE id = $1::uuid", conv_id,
        )
    assert row["user_id"] is not None
    assert str(row["user_id"]) == uid

    # Unbind.
    assert await delete_channel_identity(identity.id, uid, tid) is True

    # Conv survives; user_id is NULL (the spec — "不删会话，保留历史").
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM conversations WHERE id = $1::uuid", conv_id,
        )
    assert row is not None, "conv row must NOT be deleted"
    assert row["user_id"] is None, (
        "delete_channel_identity must NULL the corresponding conv.user_id; "
        "non-null leaks the unbound user's identity to the next relinker"
    )


async def test_delete_channel_identity_only_nulls_matching_conv() -> None:
    """The NULL UPDATE is scoped tight: only convs in the SAME tenant,
    the SAME channel_chat_id, AND pinned to a binding of the SAME
    platform get cleared. A regression that drops one predicate would
    over-clear (e.g. cross-channel) or leave a stale conv behind.

    We seed two convs: one matches the unbind triple, one differs by
    channel_chat_id. Only the first should be cleared.
    """
    import json
    tid, uid = await _seed_user("scope-null")
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        cw_id = await conn.fetchval(
            "INSERT INTO coworkers (tenant_id, name, folder) "
            "VALUES ($1::uuid, $2, $3) RETURNING id",
            tid, "CW", f"folder-{uuid.uuid4().hex[:6]}",
        )
        binding_id = await conn.fetchval(
            "INSERT INTO channel_bindings "
            "(coworker_id, tenant_id, channel_type, credentials) "
            "VALUES ($1::uuid, $2::uuid, $3, $4::jsonb) RETURNING id",
            cw_id, tid, "telegram", json.dumps({"bot_token": "t"}),
        )
        match_conv = await conn.fetchval(
            "INSERT INTO conversations "
            "(tenant_id, coworker_id, channel_binding_id, "
            " channel_chat_id, user_id) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5::uuid) "
            "RETURNING id",
            tid, cw_id, binding_id, "111", uid,
        )
        other_conv = await conn.fetchval(
            "INSERT INTO conversations "
            "(tenant_id, coworker_id, channel_binding_id, "
            " channel_chat_id, user_id) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5::uuid) "
            "RETURNING id",
            tid, cw_id, binding_id, "999", uid,
        )
    identity = await create_channel_identity(tid, "telegram", "111", uid)

    await delete_channel_identity(identity.id, uid, tid)

    async with pool.acquire() as conn:
        match_row = await conn.fetchrow(
            "SELECT user_id FROM conversations WHERE id = $1::uuid",
            match_conv,
        )
        other_row = await conn.fetchrow(
            "SELECT user_id FROM conversations WHERE id = $1::uuid",
            other_conv,
        )
    assert match_row["user_id"] is None
    assert str(other_row["user_id"]) == uid, (
        "conv with different channel_chat_id must NOT be cleared"
    )


async def test_delete_channel_identity_rejects_other_users_id() -> None:
    """Probing another user's identity_id from your own session must
    not delete — returns False so the WebUI handler 404s.
    """
    tid, uid_a = await _seed_user("del-a")
    _, uid_b = await _seed_user("del-b")
    identity = await create_channel_identity(tid, "telegram", "555", uid_a)
    deleted = await delete_channel_identity(identity.id, uid_b, tid)
    assert deleted is False, "user B must not be able to delete user A's link"
    # And the row really still exists.
    links = await list_channel_identities_for_user(uid_a, tid)
    assert any(l.id == identity.id for l in links)


# ---------------------------------------------------------------------------
# Sanity: an unexpired but used token does NOT come back to life on
# a near-miss WHERE-clause mutation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# resolve_user_from_channel_sender — T1.5, T1.6
# ---------------------------------------------------------------------------


async def test_resolve_returns_user_id_on_hit() -> None:
    """T1.5 — A row exists for (tenant, telegram, channel_id):
    the helper returns the user_id. The query is keyed by the
    indexed UNIQUE so a hit is one B-tree probe, not a scan; we
    don't assert plan shape here (EXPLAIN is brittle across PG
    versions) but the test does pin functional correctness.
    """
    tid, uid = await _seed_user("resolve-hit")
    await create_channel_identity(tid, "telegram", "1234", uid)
    assert (
        await resolve_user_from_channel_sender(tid, "telegram", "1234") == uid
    )


async def test_resolve_returns_none_on_miss() -> None:
    """T1.6 — A sender with no linkage returns None (the caller
    decides what to do; admission rejects with guidance text)."""
    tid, _ = await _seed_user("resolve-miss")
    assert (
        await resolve_user_from_channel_sender(tid, "telegram", "9999") is None
    )


async def test_resolve_scoped_by_tenant() -> None:
    """A sender linked under tenant A is NOT visible from tenant B.
    Catches a mutation that drops the ``tenant_id`` predicate
    (which would turn the lookup into a cross-tenant identity leak).
    """
    tid_a, uid_a = await _seed_user("resolve-scope-a")
    tid_b, _ = await _seed_user("resolve-scope-b")
    await create_channel_identity(tid_a, "telegram", "1010", uid_a)
    # Same channel_id, different tenant — must miss.
    assert (
        await resolve_user_from_channel_sender(tid_b, "telegram", "1010")
        is None
    )
    # Same call from the right tenant hits.
    assert (
        await resolve_user_from_channel_sender(tid_a, "telegram", "1010")
        == uid_a
    )


async def test_resolve_scoped_by_platform() -> None:
    """Linking on Telegram does NOT grant Slack admission (and vice
    versa). The platform predicate must be on the query — a future
    drop would let one platform's identity authorize the other.
    """
    tid, uid = await _seed_user("resolve-plat")
    await create_channel_identity(tid, "telegram", "1234", uid)
    # Same channel_id literal, different platform — must miss.
    assert (
        await resolve_user_from_channel_sender(tid, "slack", "1234") is None
    )


async def test_used_token_with_future_expiry_still_rejected() -> None:
    """Mutation defence: if a future hand-edit changes the consume
    WHERE clause from ``used_at IS NULL AND expires_at > now()`` to
    just ``expires_at > now()``, a previously-used but unexpired token
    would suddenly be re-consumable. This test pins that down.
    """
    tid, uid = await _seed_user("mut-defense")
    token, exp = await create_link_token(uid, tid, "telegram", ttl_seconds=600)
    # First consume succeeds.
    first = await consume_link_token(token)
    assert first is not None
    # Token still has plenty of expires_at runway:
    assert exp - datetime.now(UTC) > timedelta(seconds=30)
    # But replay still rejected because used_at is non-null.
    second = await consume_link_token(token)
    assert second is None
