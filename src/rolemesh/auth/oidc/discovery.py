"""OIDC discovery document dataclass."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DiscoveryDocument:
    """OIDC discovery metadata cached from .well-known/openid-configuration."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    fetched_at: float
