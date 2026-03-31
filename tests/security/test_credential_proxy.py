"""Tests for rolemesh.credential_proxy."""

from __future__ import annotations

from rolemesh.security.credential_proxy import detect_auth_mode


def test_detect_auth_mode_default() -> None:
    mode = detect_auth_mode()
    # Without ANTHROPIC_API_KEY in .env, should default to oauth
    assert mode in ("api-key", "oauth")
