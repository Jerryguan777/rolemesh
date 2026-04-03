"""Short-lived JWT issuance for MCP user identity forwarding.

When an agent calls an external MCP server on behalf of a user,
the credential proxy uses this service to issue a short-lived token
that identifies the user — without forwarding the original SaaS JWT
(which may expire during agent execution).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import jwt


@dataclass(frozen=True)
class RoleMeshToken:
    """Parsed contents of a RoleMesh-issued short-lived JWT."""

    user_id: str
    tenant_id: str
    coworker_id: str
    conversation_id: str
    issued_at: float
    expires_at: float


class TokenService:
    """Issue and verify short-lived JWTs for MCP identity forwarding."""

    def __init__(self, secret: str, ttl_seconds: int = 300) -> None:
        if not secret:
            raise ValueError("ROLEMESH_TOKEN_SECRET must not be empty")
        self._secret = secret
        self._ttl = ttl_seconds

    def issue(
        self,
        user_id: str,
        tenant_id: str,
        coworker_id: str,
        conversation_id: str,
    ) -> str:
        """Issue a short-lived JWT carrying user identity."""
        now = time.time()
        payload: dict[str, object] = {
            "sub": user_id,
            "tid": tenant_id,
            "cid": coworker_id,
            "vid": conversation_id,
            "iat": int(now),
            "exp": int(now + self._ttl),
            "iss": "rolemesh",
        }
        return jwt.encode(payload, self._secret, algorithm="HS256")

    def verify(self, token: str) -> RoleMeshToken | None:
        """Verify and decode a RoleMesh-issued JWT. Returns None on failure."""
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                issuer="rolemesh",
            )
            return RoleMeshToken(
                user_id=payload["sub"],
                tenant_id=payload["tid"],
                coworker_id=payload["cid"],
                conversation_id=payload["vid"],
                issued_at=float(payload["iat"]),
                expires_at=float(payload["exp"]),
            )
        except (jwt.InvalidTokenError, KeyError):
            return None
