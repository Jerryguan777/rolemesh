"""Builtin auth provider — stub for future self-managed authentication.

Will include login, registration, password hashing, and JWT issuance
when builtin deployment mode is implemented.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser


class BuiltinProvider:
    """Stub for future builtin auth. All methods raise NotImplementedError."""

    async def authenticate(self, token: str) -> AuthenticatedUser | None:
        raise NotImplementedError("Builtin auth not yet implemented")

    async def get_user_by_id(self, user_id: str) -> AuthenticatedUser | None:
        raise NotImplementedError("Builtin auth not yet implemented")
