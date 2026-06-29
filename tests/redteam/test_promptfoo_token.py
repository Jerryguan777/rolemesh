"""Tests for the red-team provider's OIDC token acquisition / self-renewal.

The live ROPG call (_mint_token) hits Keycloak, but the renewal *decision*
(_should_refresh) and the static-vs-mint-vs-cache routing (_get_token) are pure
once the clock and the mint are injectable — so they unit-test with no IdP, no
network, no Docker. That is where the "long run 401s mid-flight" regression
lives, so it is pinned here.

``redteam/promptfoo`` is a standalone tool, not a package, so we put it on
``sys.path`` to import ``provider`` directly (websockets is imported lazily).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REDTEAM_PROMPTFOO = Path(__file__).resolve().parents[2] / "redteam" / "promptfoo"
sys.path.insert(0, str(_REDTEAM_PROMPTFOO))

import provider  # noqa: E402

# --- _should_refresh (pure renewal predicate) -------------------------------


def test_should_refresh_when_no_token_yet() -> None:
    # deadline 0 (never minted) -> always refresh.
    assert provider._should_refresh(now=1000.0, deadline=0.0)


def test_should_refresh_within_skew_of_expiry() -> None:
    # 100s left, skew is 300 -> refresh.
    assert provider._should_refresh(now=900.0, deadline=1000.0)


def test_no_refresh_when_plenty_of_life_left() -> None:
    # 600s left, skew is 300 -> keep.
    assert not provider._should_refresh(now=400.0, deadline=1000.0)


# --- _get_token (static / self-mint / cache / no-source) --------------------


@pytest.fixture(autouse=True)
def _reset_token_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts from a clean cache and no ambient config."""
    monkeypatch.setattr(provider, "OIDC_TOKEN", "")
    monkeypatch.setattr(provider, "KC_USERNAME", "")
    provider._token_cache["token"] = ""
    provider._token_cache["deadline"] = 0.0


def test_static_token_wins_and_never_mints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider, "OIDC_TOKEN", "static-tok")

    def _boom() -> tuple[str, float]:
        raise AssertionError("must not mint when a static token is set")

    monkeypatch.setattr(provider, "_mint_token", _boom)
    assert provider._get_token() == "static-tok"


def test_no_source_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # No static token, no ROPG username -> a clear configuration error.
    with pytest.raises(provider.ProviderError, match="no token"):
        provider._get_token()


def test_self_mint_then_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """First call mints; a second call within the token's life reuses the
    cache (mints exactly once) — the renewal path that keeps a long run alive
    without re-minting on every request."""
    monkeypatch.setattr(provider, "KC_USERNAME", "owner@t1")
    calls = {"n": 0}

    def _fake_mint() -> tuple[str, float]:
        calls["n"] += 1
        return f"tok-{calls['n']}", 1800.0

    monkeypatch.setattr(provider, "_mint_token", _fake_mint)

    assert provider._get_token() == "tok-1"
    assert provider._get_token() == "tok-1"  # cached, not re-minted
    assert calls["n"] == 1


def test_remint_after_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the cached token falls within the refresh skew, the next call
    re-mints — so a 30-min token can't strand a multi-hour run."""
    monkeypatch.setattr(provider, "KC_USERNAME", "owner@t1")
    seq = iter([("tok-1", 1800.0), ("tok-2", 1800.0)])
    monkeypatch.setattr(provider, "_mint_token", lambda: next(seq))

    assert provider._get_token() == "tok-1"
    # Force the cache to look near-expiry, then the next call must re-mint.
    provider._token_cache["deadline"] = provider.time.monotonic() + 10.0
    assert provider._get_token() == "tok-2"
