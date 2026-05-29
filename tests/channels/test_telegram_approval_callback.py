"""v6.1 §P2b.1 — Telegram CallbackQuery → engine.handle_decision.

These tests exercise the gateway's inbound decision path against the
real Postgres pool (so the tenant/identity lookups go through the
production queries) but a fake engine. We deliberately keep PTB at
arm's length: :func:`dispatch_telegram_callback_decision` is the
pure-ish surface the PTB-bound handler sits on top of, and is where
all the policy decisions live (tenant resolution, identity reverse-
lookup, exception mapping). Touching the PTB-handler shape directly
would force every test to construct fake CallbackQuery / Message /
Bot objects without buying anything.

Mutation thinking (CLAUDE.md test philosophy):

* If S5 regresses (tenant taken from sender instead of bot_token), the
  cross-tenant test in :mod:`tests.security.test_callback_isolation`
  catches it. Here we pin "tenant comes from bot_token" so a refactor
  that drops the bot_token lookup is caught even without the
  cross-tenant fixture.
* If ``_parse_callback_data`` regressed by accepting a leading ``apr``
  without colon, an unknown CMD would route to engine.decide → an
  audit row for a phantom request_id. The parse boundary test pins
  this.
* If the exception map (ConflictError → ALREADY) was replaced with a
  silent re-raise, the user would see a stuck spinner forever. The
  exception-mapping tests guard the visible wire text.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from rolemesh.channels.telegram_gateway import (
    _parse_callback_data,
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


# ---------------------------------------------------------------------------
# Engine doubles
# ---------------------------------------------------------------------------


@dataclass
class _DecideCall:
    request_id: str
    tenant_id: str
    outcome: str
    user_id: str


class _ConflictError(Exception):
    """Stand-in for :class:`ApprovalEngine.ConflictError`.

    The gateway recognises the engine exceptions by class **name**
    (avoids a module-import cycle telegram_gateway → approval.engine);
    a same-name fake therefore exercises the same code path.
    """

    pass


class _ForbiddenError(Exception):
    pass


# Bind the class names the gateway looks for so the mapping logic
# treats these as the real engine errors. Without this rebind, the
# fakes carry their underscore-prefixed names and the gateway falls
# through to the generic "failed" branch.
_ConflictError.__name__ = "ConflictError"
_ForbiddenError.__name__ = "ForbiddenError"


class _FakeEngine:
    """An engine that records :meth:`handle_decision` calls."""

    def __init__(self) -> None:
        self.calls: list[_DecideCall] = []
        self.raise_exc: Exception | None = None

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
        if self.raise_exc is not None:
            raise self.raise_exc
        return object()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_tenant_with_telegram_bot(
    *,
    bot_token: str,
    linked_telegram_id: str | None = None,
    slug_tag: str = "p2b",
) -> tuple[str, str, str]:
    """Create tenant + user + coworker + Telegram channel binding +
    (optionally) a linked Telegram identity. Returns
    ``(tenant_id, user_id, coworker_id)``.
    """
    t = await create_tenant(name="T", slug=f"{slug_tag}-{uuid.uuid4().hex[:6]}")
    u = await create_user(
        tenant_id=t.id, name="Alice",
        email=f"alice-{uuid.uuid4().hex[:6]}@x.com",
        role="owner",
    )
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:6]}"
    )
    await create_channel_binding(
        coworker_id=cw.id,
        tenant_id=t.id,
        channel_type="telegram",
        credentials={"bot_token": bot_token},
        bot_display_name="bot",
    )
    if linked_telegram_id is not None:
        await create_channel_identity(
            t.id, "telegram", linked_telegram_id, u.id
        )
    return t.id, u.id, cw.id


# ---------------------------------------------------------------------------
# Unit: parse boundary (mutation-style negatives)
# ---------------------------------------------------------------------------


class TestParseCallbackData:
    def test_apr_prefix_extracts_request_id(self) -> None:
        assert _parse_callback_data("apr:abc-123") == ("approved", "abc-123")

    def test_rej_prefix_extracts_request_id(self) -> None:
        assert _parse_callback_data("rej:abc-123") == ("rejected", "abc-123")

    def test_missing_colon_is_rejected(self) -> None:
        """A bare ``apr`` (no colon, no request_id) must be ignored.
        Otherwise a stray button (e.g. from an unrelated bot the user
        also talks to) could phantom-decide a row with empty id."""
        assert _parse_callback_data("apr") is None

    def test_other_prefixes_ignored(self) -> None:
        assert _parse_callback_data("approve:1") is None
        assert _parse_callback_data("foo:bar") is None
        assert _parse_callback_data("") is None


# ---------------------------------------------------------------------------
# Unit: dispatch flow
# ---------------------------------------------------------------------------


class TestDispatchHappyPath:
    async def test_approve_resolves_tenant_via_bot_token_and_calls_engine(
        self,
    ) -> None:
        """T2b.2 — happy path: bot A's token resolves to tenant A; the
        linked sender_id maps to a user; the engine decides
        ``approved``; the user-facing edit text is the approve glyph."""
        token = f"tkn-{uuid.uuid4().hex[:8]}"
        tenant_id, user_id, _cw = await _seed_tenant_with_telegram_bot(
            bot_token=token, linked_telegram_id="700700"
        )
        engine = _FakeEngine()
        set_approval_decision_router(engine)
        try:
            result = await dispatch_telegram_callback_decision(
                bot_token=token,
                sender_id="700700",
                callback_data=f"apr:{uuid.uuid4()}",
            )
        finally:
            set_approval_decision_router(None)
        assert result.kind == "approved"
        assert "Approved" in result.edit_text
        assert len(engine.calls) == 1
        call = engine.calls[0]
        assert call.tenant_id == tenant_id
        assert call.user_id == user_id
        assert call.outcome == "approved"

    async def test_reject_calls_engine_with_rejected_outcome(self) -> None:
        """T2b.3 — symmetric reject path. Pinning both outcomes
        independently catches a mutation that swaps the if/else
        branches in the gateway."""
        token = f"tkn-{uuid.uuid4().hex[:8]}"
        tenant_id, user_id, _cw = await _seed_tenant_with_telegram_bot(
            bot_token=token, linked_telegram_id="700701"
        )
        engine = _FakeEngine()
        set_approval_decision_router(engine)
        try:
            result = await dispatch_telegram_callback_decision(
                bot_token=token,
                sender_id="700701",
                callback_data=f"rej:{uuid.uuid4()}",
            )
        finally:
            set_approval_decision_router(None)
        assert result.kind == "rejected"
        assert "Rejected" in result.edit_text
        assert engine.calls[0].outcome == "rejected"
        assert engine.calls[0].tenant_id == tenant_id
        assert engine.calls[0].user_id == user_id


class TestDispatchErrorMapping:
    async def test_conflict_error_renders_already_processed(self) -> None:
        """T2b.4 — engine raises ConflictError → edit text reads
        "already decided". The user must NOT see a generic "failed"
        message, or they will retry and get the same outcome."""
        token = f"tkn-{uuid.uuid4().hex[:8]}"
        _t, _u, _cw = await _seed_tenant_with_telegram_bot(
            bot_token=token, linked_telegram_id="700702"
        )
        engine = _FakeEngine()
        engine.raise_exc = _ConflictError()
        set_approval_decision_router(engine)
        try:
            result = await dispatch_telegram_callback_decision(
                bot_token=token,
                sender_id="700702",
                callback_data=f"apr:{uuid.uuid4()}",
            )
        finally:
            set_approval_decision_router(None)
        assert result.kind == "conflict"
        assert "already" in result.edit_text.lower()

    async def test_forbidden_error_renders_unauthorised(self) -> None:
        token = f"tkn-{uuid.uuid4().hex[:8]}"
        _t, _u, _cw = await _seed_tenant_with_telegram_bot(
            bot_token=token, linked_telegram_id="700703"
        )
        engine = _FakeEngine()
        engine.raise_exc = _ForbiddenError()
        set_approval_decision_router(engine)
        try:
            result = await dispatch_telegram_callback_decision(
                bot_token=token,
                sender_id="700703",
                callback_data=f"apr:{uuid.uuid4()}",
            )
        finally:
            set_approval_decision_router(None)
        assert result.kind == "forbidden"
        assert "authoris" in result.edit_text.lower()

    async def test_generic_exception_renders_failed(self) -> None:
        token = f"tkn-{uuid.uuid4().hex[:8]}"
        _t, _u, _cw = await _seed_tenant_with_telegram_bot(
            bot_token=token, linked_telegram_id="700704"
        )
        engine = _FakeEngine()
        engine.raise_exc = RuntimeError("boom")
        set_approval_decision_router(engine)
        try:
            result = await dispatch_telegram_callback_decision(
                bot_token=token,
                sender_id="700704",
                callback_data=f"apr:{uuid.uuid4()}",
            )
        finally:
            set_approval_decision_router(None)
        assert result.kind == "failed"
        assert "retry" in result.edit_text.lower()


class TestDispatchEdgeCases:
    async def test_unknown_callback_data_is_silent(self) -> None:
        """Stray buttons from non-approval flows must NOT trigger an
        engine call AND must not produce wire text; the handler will
        leave the existing card visible. Catches a mutation that
        treats every CallbackQuery as an approval."""
        engine = _FakeEngine()
        set_approval_decision_router(engine)
        try:
            result = await dispatch_telegram_callback_decision(
                bot_token="anything",
                sender_id="x",
                callback_data="not-an-approval",
            )
        finally:
            set_approval_decision_router(None)
        assert result.kind == "ignored"
        assert result.edit_text == ""
        assert engine.calls == []

    async def test_unlinked_sender_does_not_decide(self) -> None:
        """T2b.5 — sender_id has no linkage in the tenant: the edit
        text guides them to link; the engine is never called. This is
        the security-critical "no admission, no decision" path."""
        token = f"tkn-{uuid.uuid4().hex[:8]}"
        await _seed_tenant_with_telegram_bot(
            bot_token=token, linked_telegram_id=None
        )
        engine = _FakeEngine()
        set_approval_decision_router(engine)
        try:
            result = await dispatch_telegram_callback_decision(
                bot_token=token,
                sender_id="700705",  # not linked
                callback_data=f"apr:{uuid.uuid4()}",
            )
        finally:
            set_approval_decision_router(None)
        assert result.kind == "not_linked"
        assert engine.calls == [], (
            "engine.handle_decision MUST NOT be called for an unlinked "
            "sender — that would let any Telegram user spoof a decision "
            "by spamming click events"
        )

    async def test_unknown_bot_token_rejects_without_engine_call(
        self,
    ) -> None:
        """A callback that arrives bearing a token we have no binding
        for must not call the engine (we have no tenant). Catches a
        mutation that defaults tenant_id to a fixed value, which
        would be a cross-tenant escalation."""
        engine = _FakeEngine()
        set_approval_decision_router(engine)
        try:
            result = await dispatch_telegram_callback_decision(
                bot_token=f"tkn-not-in-db-{uuid.uuid4().hex[:8]}",
                sender_id="700706",
                callback_data=f"apr:{uuid.uuid4()}",
            )
        finally:
            set_approval_decision_router(None)
        assert result.kind == "no_tenant"
        assert engine.calls == []

    async def test_engine_unset_returns_no_engine_text(self) -> None:
        """Startup race: gateway polling begins before the engine is
        wired. The user gets a "retry on Web" message instead of a
        crashed handler."""
        token = f"tkn-{uuid.uuid4().hex[:8]}"
        await _seed_tenant_with_telegram_bot(
            bot_token=token, linked_telegram_id="700707"
        )
        # Ensure the router is unset.
        set_approval_decision_router(None)
        result = await dispatch_telegram_callback_decision(
            bot_token=token,
            sender_id="700707",
            callback_data=f"apr:{uuid.uuid4()}",
        )
        assert result.kind == "no_engine"
