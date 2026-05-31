"""Multi-user bootstrap map for token → (tenant, role) without an IdP.

Design ref: §5.2.1 (alternative A). The single ``ADMIN_BOOTSTRAP_TOKEN``
path stays untouched for backward compat; this module adds a second
fast-path triggered by ``BOOTSTRAP_USERS`` (a JSON-encoded list of
specs) so workflows that need multiple distinct identities don't
have to stand up an IdP.

Spec shape (one element per token):

    {
        "token":   "tok-alice",       # opaque bearer
        "user_id": "alice",            # slug, used to derive the UUID
        "tenant":  "default",          # tenant slug
        "role":    "owner"             # owner | admin | member
    }

The slug is mapped to a stable UUID via
``uuid5(NAMESPACE_URL, "bootstrap:<slug>")`` so the same env produces
the same DB row across restarts. The user row is upserted on first
match (``ON CONFLICT DO NOTHING``) and reused on subsequent hits.

Validation is done at startup: a single bad spec aborts boot. Spec
errors at request time would either silently fail open (bad) or
intermittently fail closed (also bad), so we fail loud at the only
predictable moment.

INV-4 ties in: the upsert means the returned ``user_id`` is a real
UUID, so ``resolve_actor_user_id`` returns it verbatim and the
bootstrap pseudo-user fallback is not hit.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Final

from rolemesh.db._pool import admin_conn

logger = logging.getLogger(__name__)

_ALLOWED_ROLES: Final[frozenset[str]] = frozenset({"owner", "admin", "member"})


class BootstrapUsersConfigError(ValueError):
    """Raised at startup when BOOTSTRAP_USERS is malformed.

    Keep it ValueError-derived so an uncaught one collapses the
    startup with a clear traceback rather than silently masking a
    misconfiguration.
    """


@dataclass(frozen=True)
class BootstrapUserSpec:
    token: str
    user_id_slug: str
    tenant_slug: str
    role: str

    @property
    def stable_uuid(self) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"bootstrap:{self.user_id_slug}"))


def _validate_spec(raw: object, index: int) -> BootstrapUserSpec:
    if not isinstance(raw, dict):
        raise BootstrapUsersConfigError(
            f"BOOTSTRAP_USERS[{index}] must be an object, got {type(raw).__name__}"
        )
    required = ("token", "user_id", "tenant", "role")
    missing = [k for k in required if k not in raw]
    if missing:
        raise BootstrapUsersConfigError(
            f"BOOTSTRAP_USERS[{index}] missing required field(s): {missing}"
        )
    for k in required:
        if not isinstance(raw[k], str) or not raw[k]:
            raise BootstrapUsersConfigError(
                f"BOOTSTRAP_USERS[{index}].{k} must be a non-empty string"
            )
    if raw["role"] not in _ALLOWED_ROLES:
        raise BootstrapUsersConfigError(
            f"BOOTSTRAP_USERS[{index}].role={raw['role']!r} not in "
            f"{sorted(_ALLOWED_ROLES)}"
        )
    return BootstrapUserSpec(
        token=raw["token"],
        user_id_slug=raw["user_id"],
        tenant_slug=raw["tenant"],
        role=raw["role"],
    )


def parse_bootstrap_users_env(env_value: str | None) -> dict[str, BootstrapUserSpec]:
    """Parse the BOOTSTRAP_USERS env var into a token → spec map.

    Returns an empty dict if the variable is absent or empty (the
    feature is off). Raises ``BootstrapUsersConfigError`` for any
    malformed input — including duplicate tokens, which would
    otherwise cause non-deterministic auth.
    """
    if not env_value:
        return {}
    try:
        decoded = json.loads(env_value)
    except json.JSONDecodeError as exc:
        raise BootstrapUsersConfigError(
            f"BOOTSTRAP_USERS is not valid JSON: {exc}"
        ) from exc
    if not isinstance(decoded, list):
        raise BootstrapUsersConfigError(
            "BOOTSTRAP_USERS must decode to a JSON array of spec objects"
        )
    out: dict[str, BootstrapUserSpec] = {}
    for i, raw in enumerate(decoded):
        spec = _validate_spec(raw, i)
        if spec.token in out:
            raise BootstrapUsersConfigError(
                f"BOOTSTRAP_USERS[{i}].token={spec.token!r} duplicates an "
                f"earlier entry"
            )
        out[spec.token] = spec
    return out


# Process-wide map populated at startup. ``None`` means "not yet
# loaded" so we can tell uninitialized from "feature is off"
# (in the latter case the map is an empty dict).
_specs_by_token: dict[str, BootstrapUserSpec] | None = None
# Set of (tenant_id, user_uuid) pairs we have already upserted in this
# process. Avoids hammering the DB on every authenticated request.
_upserted: set[tuple[str, str]] = set()


def init_bootstrap_users(
    env_value: str | None = None, auth_mode: str | None = None
) -> None:
    """Parse BOOTSTRAP_USERS once at startup.

    ``auth_mode`` is the deployed AuthProvider mode. Per §5.2.1 the
    multi-user bootstrap map is intended for dev/test only; if the
    mode is anything other than ``external`` (the default fast-path
    mode), warn so operators don't accidentally ship a prod deploy
    with permissive token-to-user mappings.
    """
    global _specs_by_token, _upserted
    if env_value is None:
        env_value = os.environ.get("BOOTSTRAP_USERS", "")
    _specs_by_token = parse_bootstrap_users_env(env_value)
    _upserted = set()
    if _specs_by_token:
        mode = auth_mode if auth_mode is not None else os.environ.get(
            "AUTH_MODE", "external"
        )
        if mode != "external":
            logger.warning(
                "BOOTSTRAP_USERS is set while AUTH_MODE=%r; the multi-user "
                "bootstrap map is intended for the external fast-path only.",
                mode,
            )


def get_spec_for_token(token: str) -> BootstrapUserSpec | None:
    """Return the spec for ``token`` or ``None`` if none matches.

    Falls back to ``None`` (not raise) so callers can chain into the
    next auth strategy when the token is unknown.
    """
    if _specs_by_token is None:
        # The startup hook never ran (e.g. unit-test entry). Lazy-init
        # from env so direct callers in tests still work — but in
        # production the explicit ``init_bootstrap_users`` call is
        # what gates the warn-log.
        init_bootstrap_users()
    assert _specs_by_token is not None
    return _specs_by_token.get(token)


async def ensure_bootstrap_user_row(
    spec: BootstrapUserSpec, tenant_id: str
) -> str:
    """Upsert the spec's user row and return the UUID.

    The slug → UUID mapping is deterministic so an idempotent
    ``INSERT … ON CONFLICT DO NOTHING`` is correct regardless of how
    many processes / requests collide here.
    """
    user_uuid = spec.stable_uuid
    cache_key = (tenant_id, user_uuid)
    if cache_key in _upserted:
        return user_uuid
    async with admin_conn() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, tenant_id, name, role)
            VALUES ($1::uuid, $2::uuid, $3, $4)
            ON CONFLICT (id) DO NOTHING
            """,
            user_uuid,
            tenant_id,
            spec.user_id_slug,
            spec.role,
        )
    _upserted.add(cache_key)
    return user_uuid


def _reset_for_tests() -> None:
    """Test-only reset of process-wide state."""
    global _specs_by_token, _upserted
    _specs_by_token = None
    _upserted = set()
