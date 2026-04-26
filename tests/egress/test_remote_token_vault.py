"""Unit tests for ``rolemesh.egress.remote_token_vault.RemoteTokenVault``.

Covers the gateway-side proxy that forwards token requests to the
orchestrator. The wire shape is fixed (``{"user_id": ...}`` →
``{"access_token": ... | null, "error": ...}``) and every error path
must degrade to ``None`` rather than raise — the gateway uses the
return value to decide whether to inject a Bearer header, and any
exception in this path would surface as an HTTP 500 inside the
reverse proxy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from rolemesh.egress.remote_token_vault import (
    TOKEN_ACCESS_REQUEST_SUBJECT,
    RemoteTokenVault,
)


@dataclass
class _Reply:
    data: bytes


class _StubNats:
    """Minimal duck-type for the ``nc.request`` method we use."""

    def __init__(self, reply: _Reply | Exception) -> None:
        self._reply = reply
        self.requests: list[tuple[str, bytes, float]] = []

    async def request(self, subject: str, data: bytes, timeout: float) -> _Reply:
        self.requests.append((subject, data, timeout))
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_access_token_on_success() -> None:
    nc = _StubNats(_Reply(json.dumps({"access_token": "AT-123"}).encode()))
    vault = RemoteTokenVault(nc, timeout_s=2.0)

    token = await vault.get_fresh_access_token("user-uuid")

    assert token == "AT-123"
    # Exactly one RPC, on the canonical subject, body is the user_id JSON.
    assert len(nc.requests) == 1
    subject, body, timeout = nc.requests[0]
    assert subject == TOKEN_ACCESS_REQUEST_SUBJECT
    assert json.loads(body) == {"user_id": "user-uuid"}
    assert timeout == 2.0


# ---------------------------------------------------------------------------
# Defensive empty input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_user_id_short_circuits_without_rpc() -> None:
    # Belt-and-braces: if the X-RoleMesh-User-Id header arrived blank
    # from a misconfigured agent, we skip the round-trip entirely.
    # Saves NATS traffic and matches the protocol's None contract.
    nc = _StubNats(_Reply(b"{}"))  # would respond if asked
    vault = RemoteTokenVault(nc)

    assert await vault.get_fresh_access_token("") is None
    assert nc.requests == []  # never asked


# ---------------------------------------------------------------------------
# Failure modes — every one must degrade to None, never raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nats_timeout_returns_none() -> None:
    # asyncio.TimeoutError is the canonical NATS request timeout
    # signature; test stand-in is any exception via the stub.
    nc = _StubNats(TimeoutError("nats timeout"))
    vault = RemoteTokenVault(nc, timeout_s=0.1)
    assert await vault.get_fresh_access_token("u") is None


@pytest.mark.asyncio
async def test_nats_arbitrary_transport_error_returns_none() -> None:
    nc = _StubNats(RuntimeError("nats not connected"))
    vault = RemoteTokenVault(nc)
    assert await vault.get_fresh_access_token("u") is None


@pytest.mark.asyncio
async def test_malformed_reply_returns_none() -> None:
    nc = _StubNats(_Reply(b"not-json"))
    vault = RemoteTokenVault(nc)
    assert await vault.get_fresh_access_token("u") is None


@pytest.mark.asyncio
async def test_reply_not_a_dict_returns_none() -> None:
    # JSON valid but wrong shape — e.g. someone published a list
    # instead of an object.
    nc = _StubNats(_Reply(json.dumps(["AT-123"]).encode()))
    vault = RemoteTokenVault(nc)
    assert await vault.get_fresh_access_token("u") is None


@pytest.mark.asyncio
async def test_reply_with_explicit_null_returns_none() -> None:
    # Orchestrator's normal "no token for this user" reply.
    nc = _StubNats(
        _Reply(json.dumps({"access_token": None, "error": "no_user"}).encode())
    )
    vault = RemoteTokenVault(nc)
    assert await vault.get_fresh_access_token("u") is None


@pytest.mark.asyncio
async def test_reply_with_non_string_token_returns_none() -> None:
    # Defends against an upstream regression that publishes a
    # numeric token by mistake — callers expect ``str | None``.
    nc = _StubNats(_Reply(json.dumps({"access_token": 12345}).encode()))
    vault = RemoteTokenVault(nc)
    assert await vault.get_fresh_access_token("u") is None


@pytest.mark.asyncio
async def test_reply_missing_access_token_key_returns_none() -> None:
    # ``{"error": "..."}`` without ``access_token`` is a legal
    # error-only reply; treat it as None.
    nc = _StubNats(_Reply(json.dumps({"error": "vault_error"}).encode()))
    vault = RemoteTokenVault(nc)
    assert await vault.get_fresh_access_token("u") is None
