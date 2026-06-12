"""Unit tests for signed identity tokens (token-identity refactor).

Covers the security-load-bearing properties: a valid token round-trips
to the exact identity, and every tampering / expiry / wrong-key / shape
failure returns None (fail-closed). Also pins ``from_env`` validation
and the dual-run ``reconcile`` policy.
"""

from __future__ import annotations

import pytest

from rolemesh.egress.identity import Identity
from rolemesh.egress.token_identity import (
    SECRET_ENV,
    TTL_ENV,
    TokenAuthority,
    mint,
    reconcile,
    verify,
)

_SECRET = "test-secret-at-least-16-chars"


def _identity(tenant: str = "ten", job: str = "job") -> Identity:
    return Identity(
        tenant_id=tenant,
        coworker_id="cow",
        user_id="usr",
        conversation_id="conv",
        job_id=job,
        container_name="rolemesh-x-1",
    )


def test_round_trip_recovers_exact_identity() -> None:
    tok = mint(_identity(), secret=_SECRET, ttl_seconds=3600, now=1000.0)
    got = verify(tok, secret=_SECRET, now=1000.0)
    assert got == _identity()


def test_token_is_url_and_userinfo_safe() -> None:
    """The token rides in a URL path segment and proxy userinfo — it
    must contain no '/', ':', '@', or '?'."""
    tok = mint(_identity(), secret=_SECRET, ttl_seconds=3600)
    assert not (set(tok) & set("/:@?#"))


def test_expired_token_rejected() -> None:
    tok = mint(_identity(), secret=_SECRET, ttl_seconds=60, now=1000.0)
    # Well past exp + skew.
    assert verify(tok, secret=_SECRET, now=1000.0 + 60 + 31) is None


def test_within_skew_still_valid() -> None:
    tok = mint(_identity(), secret=_SECRET, ttl_seconds=60, now=1000.0)
    assert verify(tok, secret=_SECRET, now=1000.0 + 60 + 5) is not None


def test_wrong_secret_rejected() -> None:
    tok = mint(_identity(), secret=_SECRET, ttl_seconds=3600)
    assert verify(tok, secret="another-secret-16chars", now=None) is None


def test_tampered_payload_rejected() -> None:
    tok = mint(_identity(), secret=_SECRET, ttl_seconds=3600, now=1000.0)
    payload, _, sig = tok.partition(".")
    # Flip a character in the payload; signature no longer matches.
    forged = payload[:-1] + ("A" if payload[-1] != "A" else "B") + "." + sig
    assert verify(forged, secret=_SECRET, now=1000.0) is None


@pytest.mark.parametrize("bad", ["", "noseparator", "a.b.c", "....", "onlyone."])
def test_malformed_tokens_rejected(bad: str) -> None:
    assert verify(bad, secret=_SECRET) is None


def test_authority_from_env_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, _SECRET)
    monkeypatch.setenv(TTL_ENV, "120")
    auth = TokenAuthority.from_env()
    assert auth.ttl_seconds == 120
    tok = auth.mint(_identity())
    assert auth.verify(tok) == _identity()


def test_authority_from_env_requires_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SECRET_ENV, raising=False)
    with pytest.raises(ValueError, match=SECRET_ENV):
        TokenAuthority.from_env()


def test_authority_from_env_rejects_short_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "tooshort")
    with pytest.raises(ValueError, match=SECRET_ENV):
        TokenAuthority.from_env()


def test_authority_from_env_rejects_bad_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, _SECRET)
    monkeypatch.setenv(TTL_ENV, "-5")
    with pytest.raises(ValueError, match=TTL_ENV):
        TokenAuthority.from_env()


# --- reconcile (dual-run policy) -------------------------------------


def test_reconcile_prefers_token() -> None:
    tok_id = _identity(tenant="from-token")
    ip_id = _identity(tenant="from-ip")
    assert reconcile(tok_id, ip_id, token_expected=True) is tok_id


def test_reconcile_falls_back_to_ip_when_no_token() -> None:
    ip_id = _identity()
    assert reconcile(None, ip_id, token_expected=True) is ip_id


def test_reconcile_none_when_neither() -> None:
    assert reconcile(None, None, token_expected=True) is None


def test_reconcile_mismatch_logs_but_trusts_token(caplog: pytest.LogCaptureFixture) -> None:
    tok_id = _identity(tenant="A", job="j1")
    ip_id = _identity(tenant="B", job="j2")
    # Token still wins; the mismatch is a logged signal, not a block.
    assert reconcile(tok_id, ip_id, token_expected=True) is tok_id
