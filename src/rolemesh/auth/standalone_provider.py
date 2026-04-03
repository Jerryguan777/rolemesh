"""Standalone auth provider — stub for future self-managed authentication.

Will include login, registration, password hashing, and JWT issuance
when standalone deployment mode is implemented.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser


class StandaloneProvider:
    """Stub for future standalone auth. All methods raise NotImplementedError."""

    async def authenticate(self, token: str) -> AuthenticatedUser | None:
        raise NotImplementedError("Standalone auth not yet implemented")

    async def get_user_by_id(self, user_id: str) -> AuthenticatedUser | None:
        raise NotImplementedError("Standalone auth not yet implemented")
