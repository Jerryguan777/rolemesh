"""v6.1 §P2b.1 / decision S5 — cross-tenant safety for Telegram
CallbackQuery routing.

The invariant this file pins:

    The tenant that owns an approval decision MUST come from the bot
    token receiving the callback, not from the Telegram sender_id.

A user is allowed to link the same Telegram account in two RoleMesh
tenants (decision #13). If tenant resolution rode on sender_id,
clicking a button on bot A (tenant 1) would cross over to tenant 2,
because we'd find sender_id 12345 in BOTH tenants and pick the wrong
one. The bot is the per-tenant operating credential; it cannot be
spoofed by the click.

We exercise the dispatcher directly because that is where tenant
resolution actually happens:
:func:`dispatch_telegram_callback_decision` calls
``get_channel_binding_for_bot_token`` *before*
``resolve_user_from_channel_sender``, and scopes the latter by the
tenant the former returned. A regression that swaps these two lookups
or hard-codes a tenant would be caught here.

This is **the** load-bearing security property of Phase 2b; per
CLAUDE.md test philosophy it gets its own file and its own mutation
notes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from rolemesh.channels.telegram_gateway import (
    dispatch_telegram_callback_decision,
    set_approval_decision_router,
)
from rolemesh.db import (
    create_channel_binding,
    create_channel_identity,
    create_coworker,
    create_tenant,
    create_user,
)

pytestmark = pytest.mark.usefixtures("test_db")


@dataclass
class _DecideCall:
    request_id: str
    tenant_id: str
    outcome: str
    user_id: str


class _FakeEngine:
    def __init__(self) -> None:
        self.calls: list[_DecideCall] = []

    async def handle_decision(
        self,
        *,
        request_id: str,
        tenant_id: str,
        outcome: str,
        user_id: str,
        note: str | None = None,
    ) -> object:
        self.calls.append(
            _DecideCall(
                request_id=request_id,
                tenant_id=tenant_id,
                outcome=outcome,
                user_id=user_id,
            )
        )
        return object()


async def _seed_two_tenants_sharing_telegram_id(
    *, shared_telegram_id: str
) -> tuple[
    tuple[str, str, str],  # (tenant_a_id, user_a_id, bot_token_a)
    tuple[str, str, str],  # (tenant_b_id, user_b_id, bot_token_b)
]:
    """Set up two tenants whose own bots are separate, but both have
    linked the SAME Telegram user_id to DIFFERENT internal RoleMesh
    users. This is the configuration the cross-tenant invariant must
    survive."""
    suffix = uuid.uuid4().hex[:6]
    bot_token_a = f"tkn-A-{suffix}"
    bot_token_b = f"tkn-B-{suffix}"

    ta = await create_tenant(name="Tenant-A", slug=f"ta-{suffix}")
    ua = await create_user(
        tenant_id=ta.id,
        name="UserA",
        email=f"a-{suffix}@x.com",
        role="owner",
    )
    cwa = await create_coworker(
        tenant_id=ta.id, name="CW-A", folder=f"cwa-{suffix}"
    )
    await create_channel_binding(
        coworker_id=cwa.id,
        tenant_id=ta.id,
        channel_type="telegram",
        credentials={"bot_token": bot_token_a},
    )
    await create_channel_identity(
        ta.id, "telegram", shared_telegram_id, ua.id
    )

    tb = await create_tenant(name="Tenant-B", slug=f"tb-{suffix}")
    ub = await create_user(
        tenant_id=tb.id,
        name="UserB",
        email=f"b-{suffix}@x.com",
        role="owner",
    )
    cwb = await create_coworker(
        tenant_id=tb.id, name="CW-B", folder=f"cwb-{suffix}"
    )
    await create_channel_binding(
        coworker_id=cwb.id,
        tenant_id=tb.id,
        channel_type="telegram",
        credentials={"bot_token": bot_token_b},
    )
    await create_channel_identity(
        tb.id, "telegram", shared_telegram_id, ub.id
    )

    return (ta.id, ua.id, bot_token_a), (tb.id, ub.id, bot_token_b)


async def test_callback_on_bot_a_decides_in_tenant_a_only() -> None:
    """T2b.6 — clicking on bot A's card routes the engine call to
    tenant A's user, never tenant B's. If
    :func:`dispatch_telegram_callback_decision` ever resolves the
    sender_id without the bot-token scoping in place, this assertion
    flips."""
    (ta_id, ua_id, bot_token_a), (tb_id, ub_id, _bot_b) = (
        await _seed_two_tenants_sharing_telegram_id(
            shared_telegram_id="55555"
        )
    )
    engine = _FakeEngine()
    set_approval_decision_router(engine)
    request_id = str(uuid.uuid4())
    try:
        result = await dispatch_telegram_callback_decision(
            bot_token=bot_token_a,
            sender_id="55555",
            callback_data=f"apr:{request_id}",
        )
    finally:
        set_approval_decision_router(None)
    assert result.kind == "approved"
    assert len(engine.calls) == 1
    call = engine.calls[0]
    assert call.tenant_id == ta_id, (
        "tenant MUST come from bot_token's binding; got "
        f"{call.tenant_id!r}, expected tenant A {ta_id!r}"
    )
    assert call.user_id == ua_id, (
        "user_id MUST be the one linked under TENANT A; got "
        f"{call.user_id!r}. A cross-tenant leak would surface as "
        f"tenant B's user {ub_id!r}"
    )
    assert call.tenant_id != tb_id


async def test_callback_on_bot_b_decides_in_tenant_b_only() -> None:
    """Symmetric to the previous test. Together they pin the
    invariant from both sides, so a mutation that hard-codes one
    tenant is caught on one of the two runs."""
    (ta_id, ua_id, _bot_a), (tb_id, ub_id, bot_token_b) = (
        await _seed_two_tenants_sharing_telegram_id(
            shared_telegram_id="66666"
        )
    )
    engine = _FakeEngine()
    set_approval_decision_router(engine)
    request_id = str(uuid.uuid4())
    try:
        result = await dispatch_telegram_callback_decision(
            bot_token=bot_token_b,
            sender_id="66666",
            callback_data=f"apr:{request_id}",
        )
    finally:
        set_approval_decision_router(None)
    assert result.kind == "approved"
    call = engine.calls[0]
    assert call.tenant_id == tb_id
    assert call.user_id == ub_id
    assert call.tenant_id != ta_id
    assert call.user_id != ua_id


async def test_sender_linked_only_in_other_tenant_is_rejected() -> None:
    """A subtler regression: sender_id 77777 is linked ONLY in tenant
    B. A click on bot A must still be rejected as unlinked — the
    tenant-A admission gate fails for that sender, even though the
    "global" identity exists. Catches a mutation that falls back to a
    cross-tenant lookup when the tenant-scoped one misses."""
    suffix = uuid.uuid4().hex[:6]
    bot_token_a = f"tkn-A-{suffix}"

    ta = await create_tenant(name="Tenant-A", slug=f"ta-{suffix}")
    ua = await create_user(
        tenant_id=ta.id,
        name="UserA",
        email=f"a-{suffix}@x.com",
        role="owner",
    )
    cwa = await create_coworker(
        tenant_id=ta.id, name="CW-A", folder=f"cwa-{suffix}"
    )
    await create_channel_binding(
        coworker_id=cwa.id,
        tenant_id=ta.id,
        channel_type="telegram",
        credentials={"bot_token": bot_token_a},
    )
    # No identity in tenant A for sender 77777.

    tb = await create_tenant(name="Tenant-B", slug=f"tb-{suffix}")
    ub = await create_user(
        tenant_id=tb.id,
        name="UserB",
        email=f"b-{suffix}@x.com",
        role="owner",
    )
    await create_channel_identity(tb.id, "telegram", "77777", ub.id)

    engine = _FakeEngine()
    set_approval_decision_router(engine)
    try:
        result = await dispatch_telegram_callback_decision(
            bot_token=bot_token_a,
            sender_id="77777",
            callback_data=f"apr:{uuid.uuid4()}",
        )
    finally:
        set_approval_decision_router(None)
    assert result.kind == "not_linked"
    assert engine.calls == [], (
        "engine MUST NOT be called when the sender_id is unlinked in "
        "this tenant — a global lookup would let the wrong user "
        "decide cross-tenant"
    )
    # Belt + braces: the dispatcher should not have surfaced tenant B.
    _silence_unused = (ta.id, ua.id)
