"""AuthProvider abstraction for pluggable authentication backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AuthenticatedUser:
    """Result of authenticating an inbound request."""

    user_id: str
    tenant_id: str
    role: str  # "owner" | "admin" | "member"
    email: str | None = None
    name: str | None = None
    external_token: str | None = None  # original SaaS JWT for reference


@runtime_checkable
class AuthProvider(Protocol):
    """Protocol for pluggable auth backends (embedded SaaS / standalone)."""

    async def authenticate(self, token: str) -> AuthenticatedUser | None:
        """Validate a bearer token and return the authenticated user, or None."""
        ...

    async def get_user_by_id(self, user_id: str) -> AuthenticatedUser | None:
        """Look up a user by ID."""
        ...
