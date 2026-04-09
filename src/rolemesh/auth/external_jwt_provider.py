"""External JWT auth provider — validates JWTs issued by an external SaaS system.

Configuration via environment variables:
  EXTERNAL_JWT_SECRET       — symmetric secret (for HS256/HS384/HS512)
  EXTERNAL_JWT_PUBLIC_KEY   — PEM public key (for RS256 / ES256 etc.)
  EXTERNAL_JWT_ISSUER       — expected "iss" claim (optional)
  EXTERNAL_JWT_ALGORITHMS   — comma-separated algorithm list (default: HS256)
  EXTERNAL_JWT_CLAIM_USER_ID   — claim name for user ID (default: sub)
  EXTERNAL_JWT_CLAIM_TENANT_ID — claim name for tenant ID (default: tid)
  EXTERNAL_JWT_CLAIM_ROLE      — claim name for role (default: role)
  EXTERNAL_JWT_CLAIM_EMAIL     — claim name for email (default: email)
  EXTERNAL_JWT_CLAIM_NAME      — claim name for name (default: name)
"""

from __future__ import annotations

import os

import jwt

from rolemesh.auth.provider import AuthenticatedUser


class ExternalJwtProvider:
    """Validates JWTs issued by an external SaaS and maps claims to AuthenticatedUser."""

    def __init__(self) -> None:
        self._secret = os.environ.get("EXTERNAL_JWT_SECRET", "")
        self._public_key = os.environ.get("EXTERNAL_JWT_PUBLIC_KEY", "")
        self._issuer = os.environ.get("EXTERNAL_JWT_ISSUER") or None
        algorithms_raw = os.environ.get("EXTERNAL_JWT_ALGORITHMS", "HS256")
        self._algorithms = [a.strip() for a in algorithms_raw.split(",") if a.strip()]

        # Claim mapping
        self._claim_user_id = os.environ.get("EXTERNAL_JWT_CLAIM_USER_ID", "sub")
        self._claim_tenant_id = os.environ.get("EXTERNAL_JWT_CLAIM_TENANT_ID", "tid")
        self._claim_role = os.environ.get("EXTERNAL_JWT_CLAIM_ROLE", "role")
        self._claim_email = os.environ.get("EXTERNAL_JWT_CLAIM_EMAIL", "email")
        self._claim_name = os.environ.get("EXTERNAL_JWT_CLAIM_NAME", "name")

        # Determine key material
        self._key = self._public_key or self._secret
        if not self._key:
            raise ValueError(
                "ExternalJwtProvider requires EXTERNAL_JWT_SECRET or EXTERNAL_JWT_PUBLIC_KEY"
            )

    async def authenticate(self, token: str) -> AuthenticatedUser | None:
        """Validate the external JWT and extract user identity."""
        try:
            options: dict[str, object] = {}
            kwargs: dict[str, object] = {
                "algorithms": self._algorithms,
                "options": options,
            }
            if self._issuer:
                kwargs["issuer"] = self._issuer

            payload = jwt.decode(token, self._key, **kwargs)  # type: ignore[arg-type]

            user_id = str(payload.get(self._claim_user_id, ""))
            tenant_id = str(payload.get(self._claim_tenant_id, ""))
            if not user_id:
                return None

            role = str(payload.get(self._claim_role, "member"))
            if role not in ("owner", "admin", "member"):
                role = "member"

            return AuthenticatedUser(
                user_id=user_id,
                tenant_id=tenant_id,
                role=role,
                email=payload.get(self._claim_email),
                name=payload.get(self._claim_name),
                external_token=token,
            )
        except (jwt.InvalidTokenError, KeyError):
            return None

    async def get_user_by_id(self, user_id: str) -> AuthenticatedUser | None:
        """Not supported in external JWT mode — user lookup requires the SaaS system."""
        return None
