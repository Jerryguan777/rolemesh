"""Whitelisted JWT signature algorithms for OIDC id_token verification."""

from __future__ import annotations

# Algorithms accepted for id_token signature verification.
# Whitelisted to prevent algorithm confusion attacks (e.g. tokens claiming
# alg=none or HS256 when the IdP signs with RS256).
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256", "PS384", "PS512")
