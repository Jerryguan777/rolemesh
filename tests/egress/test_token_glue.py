"""Unit tests for the orchestrator-side token responder.

``start_token_responder`` subscribes ``egress.token.access.request``
and dispatches each incoming RPC into the local ``TokenVault``'s
``get_fresh_access_token``. Tests use a hand-rolled NATS stub +
duck-typed vault — no real NATS or DB.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from rolemesh.egress.orch_glue import start_token_responder
from rolemesh.egress.remote_token_vault import TOKEN_ACCESS_REQUEST_SUBJECT


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _Sub:
    subject: str
    cb: Any


class _FakeNats:
    def __init__(self) -> None:
        self.subs: list[_Sub] = []

    async def subscribe(self, subject: str, cb: Any = None) -> _Sub:
        sub = _Sub(subject=subject, cb=cb)
        self.subs.append(sub)
        return sub


class _FakeMsg:
    """Duck-typed NATS message — only ``data`` and ``respond``."""

    def __init__(self, body: bytes) -> None:
        self.data = body
        self.replies: list[bytes] = []

    async def respond(self, body: bytes) -> None:
        self.replies.append(body)


class _StubVault:
    """Minimal vault — records calls and returns canned values."""

    def __init__(self, returns: str | None | Exception = None) -> None:
        self._returns = returns
        self.calls: list[str] = []

    async def get_fresh_access_token(self, user_id: str) -> str | None:
        self.calls.append(user_id)
        if isinstance(self._returns, Exception):
            raise self._returns
        return self._returns


@pytest.fixture
def nc() -> _FakeNats:
    return _FakeNats()


# ---------------------------------------------------------------------------
# Subscribe wires the canonical subject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_responder_subscribes_canonical_subject(nc: _FakeNats) -> None:
    sub = await start_token_responder(nc, vault=_StubVault())
    assert sub is nc.subs[0]
    assert nc.subs[0].subject == TOKEN_ACCESS_REQUEST_SUBJECT


# ---------------------------------------------------------------------------
# Happy path — request → vault → response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_access_token_for_known_user(nc: _FakeNats) -> None:
    vault = _StubVault(returns="AT-known")
    await start_token_responder(nc, vault=vault)

    msg = _FakeMsg(json.dumps({"user_id": "u-1"}).encode())
    await nc.subs[0].cb(msg)

    assert vault.calls == ["u-1"]
    payload = json.loads(msg.replies[0])
    assert payload == {"access_token": "AT-known"}


@pytest.mark.asyncio
async def test_returns_null_when_vault_says_none(nc: _FakeNats) -> None:
    vault = _StubVault(returns=None)
    await start_token_responder(nc, vault=vault)

    msg = _FakeMsg(json.dumps({"user_id": "u-1"}).encode())
    await nc.subs[0].cb(msg)

    payload = json.loads(msg.replies[0])
    assert payload == {"access_token": None}


# ---------------------------------------------------------------------------
# Bad request payloads — never crash the subscriber loop, always reply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_json_request_replies_with_error(nc: _FakeNats) -> None:
    vault = _StubVault(returns="should-not-reach-here")
    await start_token_responder(nc, vault=vault)

    msg = _FakeMsg(b"not-json")
    await nc.subs[0].cb(msg)

    # vault must NOT have been called
    assert vault.calls == []
    payload = json.loads(msg.replies[0])
    assert payload["access_token"] is None
    assert payload["error"] == "bad_json"


@pytest.mark.asyncio
async def test_payload_not_a_dict_replies_with_error(nc: _FakeNats) -> None:
    vault = _StubVault()
    await start_token_responder(nc, vault=vault)

    msg = _FakeMsg(json.dumps(["u-1"]).encode())
    await nc.subs[0].cb(msg)

    payload = json.loads(msg.replies[0])
    assert payload["access_token"] is None
    assert payload["error"] == "bad_payload"


@pytest.mark.asyncio
async def test_missing_user_id_replies_with_error(nc: _FakeNats) -> None:
    vault = _StubVault()
    await start_token_responder(nc, vault=vault)

    msg = _FakeMsg(json.dumps({"foo": "bar"}).encode())
    await nc.subs[0].cb(msg)

    payload = json.loads(msg.replies[0])
    assert payload["access_token"] is None
    assert payload["error"] == "missing_user_id"


@pytest.mark.asyncio
async def test_empty_user_id_replies_with_error(nc: _FakeNats) -> None:
    # ``""`` passes ``.get("user_id")`` but the responder rejects it
    # to mirror RemoteTokenVault's defensive short-circuit.
    vault = _StubVault()
    await start_token_responder(nc, vault=vault)

    msg = _FakeMsg(json.dumps({"user_id": ""}).encode())
    await nc.subs[0].cb(msg)

    payload = json.loads(msg.replies[0])
    assert payload["error"] == "missing_user_id"


# ---------------------------------------------------------------------------
# Vault raises — subscriber loop must survive, reply with error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vault_exception_does_not_crash_loop(nc: _FakeNats) -> None:
    # vault.get_fresh_access_token already swallows transport / IdP
    # errors and returns None — but if it raises (programming bug),
    # the responder must still reply (don't leave the gateway hanging
    # on its NATS request) and stay alive for the next call.
    vault = _StubVault(returns=RuntimeError("boom"))
    await start_token_responder(nc, vault=vault)

    msg = _FakeMsg(json.dumps({"user_id": "u-1"}).encode())
    await nc.subs[0].cb(msg)  # must not propagate the exception

    payload = json.loads(msg.replies[0])
    assert payload["access_token"] is None
    assert payload["error"] == "vault_error"


# ---------------------------------------------------------------------------
# End-to-end shape check via RemoteTokenVault
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_with_real_remote_vault() -> None:
    """Wire RemoteTokenVault → in-process responder dispatcher.

    Validates wire format compatibility — if the publisher schema
    drifts from the responder schema, this test catches it without
    needing a live NATS broker.
    """
    from rolemesh.egress.remote_token_vault import RemoteTokenVault

    vault = _StubVault(returns="AT-roundtrip")
    nc = _FakeNats()
    await start_token_responder(nc, vault=vault)

    # Build a NATS double whose .request invokes the registered
    # callback synchronously and returns a Reply with the response.
    class _LoopBackNats:
        async def request(
            self, subject: str, data: bytes, timeout: float
        ) -> Any:
            sub = next(s for s in nc.subs if s.subject == subject)
            msg = _FakeMsg(data)
            await sub.cb(msg)
            return type("R", (), {"data": msg.replies[0]})()

    rv = RemoteTokenVault(_LoopBackNats())
    token = await rv.get_fresh_access_token("u-1")
    assert token == "AT-roundtrip"
    assert vault.calls == ["u-1"]
