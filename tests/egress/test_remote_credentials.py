"""Tests for ``rolemesh.egress.remote_credentials.RemoteCredentialResolver``.

Covers the gateway-side proxy that forwards credential lookups to the
orchestrator. NATS is a system boundary; tests use a ``_StubNats``
duck-type matching the ``nc.request(subject, data, timeout)`` shape —
the same pattern as ``tests/egress/test_remote_token_vault.py``.

Each test names the mutation it pins.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from rolemesh.egress.credentials import MissingCredentialError
from rolemesh.egress.remote_credentials import (
    CREDENTIAL_REQUEST_SUBJECT,
    RemoteCredentialResolver,
)


@dataclass
class _Reply:
    data: bytes


class _StubNats:
    """Minimal duck-type for ``nc.request`` — same shape as the test
    fixture in ``test_remote_token_vault.py``."""

    def __init__(self, reply: _Reply | Exception) -> None:
        self._reply = reply
        self.requests: list[tuple[str, bytes, float]] = []

    async def request(
        self, subject: str, data: bytes, timeout: float,
    ) -> _Reply:
        self.requests.append((subject, data, timeout))
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


def _ok(credential: dict[str, Any]) -> _Reply:
    return _Reply(json.dumps({"credential": credential}).encode())


def _err(code: str) -> _Reply:
    return _Reply(
        json.dumps({"credential": None, "error": code}).encode(),
    )


# ---------------------------------------------------------------------------
# Test 1 — happy path: RPC reply -> dict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_returns_credential_from_rpc_reply() -> None:
    """Pin: resolver returns the orchestrator's credential payload.

    Mutation: dropping ``payload["credential"]`` and returning the
    raw payload would make the dict assertion fail (payload has
    ``credential`` key, not ``api_key``).
    """
    nc = _StubNats(_ok({"api_key": "sk-remote", "extras": {"x": 1}}))
    resolver = RemoteCredentialResolver(nc, timeout_s=2.0)

    result = await resolver.resolve("t-1", "anthropic")

    assert result == {"api_key": "sk-remote", "extras": {"x": 1}}
    # Exactly one RPC on the canonical subject + correct body.
    assert len(nc.requests) == 1
    subject, body, timeout = nc.requests[0]
    assert subject == CREDENTIAL_REQUEST_SUBJECT
    assert json.loads(body) == {"tenant_id": "t-1", "provider": "anthropic"}
    assert timeout == 2.0


# ---------------------------------------------------------------------------
# Test 2 — MISSING -> MissingCredentialError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_missing_raises_missing_credential_error() -> None:
    """Pin: ``{"error": "MISSING"}`` maps to MissingCredentialError.

    Mutation: collapsing all errors into RuntimeError (or returning
    None silently) would not satisfy ``pytest.raises``.
    """
    nc = _StubNats(_err("MISSING"))
    resolver = RemoteCredentialResolver(nc)

    with pytest.raises(MissingCredentialError) as exc_info:
        await resolver.resolve("t-1", "anthropic")

    assert exc_info.value.tenant_id == "t-1"
    assert exc_info.value.provider == "anthropic"


# ---------------------------------------------------------------------------
# Test 3 — transport / non-MISSING errors -> RuntimeError (not 401)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_rpc_timeout_raises_runtime_error() -> None:
    """Pin: NATS transport failure raises RuntimeError, NOT MissingCredentialError.

    The 401 vs 502 distinction matters: missing credential is an
    operator-config problem; RPC failure is an orchestrator-side
    fault. Mapping them to the same exception loses that signal.

    Mutation: swallowing the transport exception and raising
    MissingCredentialError instead would let the proxy return 401,
    misleading the operator into thinking they need to configure
    a credential when actually the orchestrator is unreachable.
    """
    nc = _StubNats(TimeoutError("nats request timed out"))
    resolver = RemoteCredentialResolver(nc, timeout_s=0.1)

    with pytest.raises(RuntimeError) as exc_info:
        await resolver.resolve("t-1", "anthropic")

    assert not isinstance(exc_info.value, MissingCredentialError)


@pytest.mark.asyncio
async def test_resolve_non_missing_error_raises_runtime_error() -> None:
    """Pin: any non-MISSING error code maps to RuntimeError (not silent None,
    not MissingCredentialError).
    """
    nc = _StubNats(_err("resolver_error"))
    resolver = RemoteCredentialResolver(nc)

    with pytest.raises(RuntimeError) as exc_info:
        await resolver.resolve("t-1", "anthropic")

    assert not isinstance(exc_info.value, MissingCredentialError)
    assert "resolver_error" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 4 — cache hit skips RPC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_skips_second_rpc() -> None:
    """Pin: same (tenant, provider) twice in a row -> one RPC.

    Mutation: removing the cache lookup would issue two RPCs.
    """
    nc = _StubNats(_ok({"api_key": "k"}))
    resolver = RemoteCredentialResolver(nc)

    await resolver.resolve("t-1", "anthropic")
    await resolver.resolve("t-1", "anthropic")

    assert len(nc.requests) == 1


# ---------------------------------------------------------------------------
# Test 5 — TTL=0 expires cache immediately
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_ttl_zero_fires_rpc_each_call() -> None:
    """Pin: ``ttl_seconds=0`` makes the cache a no-op.

    Mutation: ignoring TTL (always-cache) would keep the RPC count
    at 1 instead of 2.
    """
    nc = _StubNats(_ok({"api_key": "k"}))
    resolver = RemoteCredentialResolver(nc, ttl_seconds=0)

    await resolver.resolve("t-1", "anthropic")
    await resolver.resolve("t-1", "anthropic")

    assert len(nc.requests) == 2


# ---------------------------------------------------------------------------
# Test 6 — tenant isolation in the cache key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_key_includes_both_tenant_and_provider() -> None:
    """Pin: tenant_b never receives tenant_a's cached credential.

    Mutation: caching by provider alone (or tenant alone) would
    swap one tenant's key for the other's. Each tenant's reply is
    distinct here, so a wrong cache key surfaces as the wrong
    api_key value.
    """
    # We need DIFFERENT replies for the two distinct keys. Wrap _StubNats
    # with a small "answer by lookup" variant — still a system-boundary
    # stub, no internal mocking.
    replies = {
        ("t-a", "anthropic"): _ok({"api_key": "K_A"}),
        ("t-b", "anthropic"): _ok({"api_key": "K_B"}),
    }
    calls: list[tuple[str, dict[str, str], float]] = []

    class _RoutingNats:
        async def request(
            self, subject: str, data: bytes, timeout: float,
        ) -> _Reply:
            body = json.loads(data)
            calls.append((subject, body, timeout))
            return replies[(body["tenant_id"], body["provider"])]

    resolver = RemoteCredentialResolver(_RoutingNats())

    a1 = await resolver.resolve("t-a", "anthropic")
    b1 = await resolver.resolve("t-b", "anthropic")
    a2 = await resolver.resolve("t-a", "anthropic")
    b2 = await resolver.resolve("t-b", "anthropic")

    assert a1 == {"api_key": "K_A"}
    assert b1 == {"api_key": "K_B"}
    # Cache hits the second time around — no extra RPCs.
    assert a2 == {"api_key": "K_A"}
    assert b2 == {"api_key": "K_B"}
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Defensive: malformed replies are RuntimeError, not silent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_non_json_reply_raises_runtime_error() -> None:
    """A garbage NATS reply (non-JSON) must not silently coerce to None."""
    nc = _StubNats(_Reply(b"<<<not-json>>>"))
    resolver = RemoteCredentialResolver(nc)

    with pytest.raises(RuntimeError):
        await resolver.resolve("t-1", "anthropic")


@pytest.mark.asyncio
async def test_resolve_reply_not_dict_raises_runtime_error() -> None:
    """A JSON-but-not-an-object reply must not silently coerce."""
    nc = _StubNats(_Reply(b'["unexpected", "shape"]'))
    resolver = RemoteCredentialResolver(nc)

    with pytest.raises(RuntimeError):
        await resolver.resolve("t-1", "anthropic")


# ---------------------------------------------------------------------------
# Defensive: credential field type-check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_credential_not_dict_raises_runtime_error() -> None:
    """If the orchestrator side starts returning a string for credential,
    we must NOT silently propagate it — the reverse proxy expects a dict
    and a string would crash deeper in the stack.
    """
    nc = _StubNats(
        _Reply(json.dumps({"credential": "sk-x"}).encode()),
    )
    resolver = RemoteCredentialResolver(nc)

    with pytest.raises(RuntimeError) as exc_info:
        await resolver.resolve("t-1", "anthropic")

    assert "dict" in str(exc_info.value).lower()


# Silence the unused-import warning for ``patch`` — kept in case future
# tests need to spy on a real ``nats.aio.Client`` method.
_ = patch
