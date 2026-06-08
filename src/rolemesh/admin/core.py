"""Shared admin-provisioning core for the CLI and the env-seed wrapper.

Both the ``rolemesh-admin create-admin`` CLI and the webui startup seed
(``ensure_seed_admin``) call :func:`create_admin`, so the logic that
seeds a privileged user row exists in exactly one place.

Why this is a provisioning tool and not an auth path: seeding the first
``platform_admin`` is a provisioning problem, not an authentication one.
The write goes through the BYPASSRLS admin pool (no request/auth
context, and it may touch a tenant the caller holds no session for) and
is idempotent so re-runs are safe. It is the seeding path for the first
admin: an explicit, auditable operator action bound to whoever already
holds host + DB access, introducing no permanent network-reachable
secret.

Role-model dependency: the four-role / platform-vs-tenant *scope*
model is now wired (``rolemesh.auth.permissions`` recognizes
``platform_admin`` as the superset role). ``platform_admin`` is
platform-scoped and has no tenant of its own, but ``users.tenant_id``
is NOT NULL, so its row is anchored to a reserved sentinel tenant
(slug ``__platform__``) created idempotently here — never to a real
business tenant, and never NULL. The eventual tenant-less / scoped
representation is a later migration.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from rolemesh.db import (
    admin_conn,
    create_tenant,
    create_user_with_external_sub,
    get_tenant_by_slug,
    get_user_by_email,
    get_user_by_external_sub,
)

logger = logging.getLogger(__name__)

# The platform-plane role — superset of every tenant capability
# (see ``rolemesh.auth.permissions``).
PLATFORM_ADMIN_ROLE = "platform_admin"

# Reserved sentinel tenant that anchors platform_admin rows. The leading/
# trailing double-underscore slug is not a legal user-chosen tenant slug, so it
# can never collide with a real business tenant. Created idempotently on first
# platform_admin provisioning; reused on every subsequent run.
PLATFORM_TENANT_SLUG = "__platform__"

# Tenant-plane roles recognized by the existing role model. ``--role``
# accepts these as an emergency escape hatch (scripts / tests / platform
# lockout recovery) only; the product path for tenant roles is the
# platform_admin UI/API or OIDC JIT provisioning — never this tool.
_TENANT_SCOPED_ROLES = frozenset({"owner", "admin", "member"})

ALLOWED_ROLES = frozenset({PLATFORM_ADMIN_ROLE}) | _TENANT_SCOPED_ROLES

# Env keys for the startup seed wrapper.
_SEED_EMAIL_ENV = "ROLEMESH_SEED_ADMIN_EMAIL"
_SEED_EXTERNAL_SUB_ENV = "ROLEMESH_SEED_ADMIN_EXTERNAL_SUB"
_SEED_NAME_ENV = "ROLEMESH_SEED_ADMIN_NAME"


class AdminProvisionError(Exception):
    """Raised for configuration errors (bad role, missing --tenant).

    DB / connectivity failures are *not* wrapped — they propagate so the
    CLI exits non-zero with a real traceback and the webui startup fails
    loud rather than booting with a half-applied seed.
    """


@dataclass(frozen=True)
class AdminProvisionResult:
    user_id: str
    role: str
    tenant_id: str
    tenant_slug: str
    email: str
    external_sub: str | None
    created: bool


async def _ensure_tenant(slug: str) -> str:
    """Return the id of tenant ``slug``, creating it if absent."""
    existing = await get_tenant_by_slug(slug)
    if existing is not None:
        return existing.id
    created = await create_tenant(name=slug, slug=slug)
    return created.id


async def create_admin(
    *,
    email: str,
    role: str = PLATFORM_ADMIN_ROLE,
    external_sub: str | None = None,
    name: str | None = None,
    tenant: str | None = None,
) -> AdminProvisionResult:
    """Idempotently create/confirm a privileged user row.

    Defaults to ``platform_admin`` (platform genesis — the first admin
    no one can create through the UI yet). Tenant-scoped roles
    (owner/admin/member) require ``tenant`` (a slug), which is created if
    absent.

    The ``email`` is the IdP login identifier, *not* a credential —
    authentication still runs through the IdP, so even this path
    introduces no network-reachable secret. ``external_sub`` binds a
    known IdP subject now; left unset, the row is linked on first OIDC
    login that matches the email.

    Idempotent: an existing user (matched by ``external_sub`` when
    given, else by ``email`` within the resolved tenant) is returned
    unchanged with ``created=False``.
    """
    email = email.strip()
    if not email:
        raise AdminProvisionError("--email must be a non-empty string")
    if role not in ALLOWED_ROLES:
        raise AdminProvisionError(
            f"--role {role!r} is not one of {sorted(ALLOWED_ROLES)}"
        )
    display_name = name or email.split("@", 1)[0]

    # Resolve the tenant the row hangs off. platform_admin is
    # platform-scoped and has no tenant of its own, but users.tenant_id
    # is NOT NULL, so we anchor it to the reserved ``__platform__``
    # sentinel tenant (created idempotently). It never accepts an
    # arbitrary --tenant; passing one is a usage error. Tenant-scoped
    # roles require an explicit --tenant.
    if role == PLATFORM_ADMIN_ROLE:
        if tenant is not None and tenant != PLATFORM_TENANT_SLUG:
            raise AdminProvisionError(
                "platform_admin is platform-scoped; --tenant is only valid "
                "for tenant-scoped roles (owner/admin/member)"
            )
        tenant_slug = PLATFORM_TENANT_SLUG
    else:
        if not tenant:
            raise AdminProvisionError(
                f"--tenant <slug> is required for tenant-scoped role {role!r}"
            )
        tenant_slug = tenant

    tenant_id = await _ensure_tenant(tenant_slug)

    # Idempotency: prefer the unique external_sub identity, then fall
    # back to (tenant, email). A repeat run is a no-op.
    if external_sub:
        by_sub = await get_user_by_external_sub(external_sub)
        if by_sub is not None:
            return AdminProvisionResult(
                user_id=by_sub.id,
                role=by_sub.role,
                tenant_id=by_sub.tenant_id,
                tenant_slug=tenant_slug,
                email=by_sub.email or email,
                external_sub=external_sub,
                created=False,
            )

    by_email = await get_user_by_email(email, tenant_id=tenant_id)
    if by_email is not None:
        return AdminProvisionResult(
            user_id=by_email.id,
            role=by_email.role,
            tenant_id=by_email.tenant_id,
            tenant_slug=tenant_slug,
            email=email,
            external_sub=by_email.external_sub,
            created=False,
        )

    if external_sub:
        # Reuse the existing admin-pool writer that also populates the
        # unique external_sub column.
        user = await create_user_with_external_sub(
            tenant_id=tenant_id,
            name=display_name,
            email=email,
            role=role,
            external_sub=external_sub,
        )
        user_id = user.id
    else:
        # No external_sub yet (linked on first OIDC login). create_user()
        # writes through the RLS business pool; provisioning must not
        # depend on an RLS/auth request context (the row may be platform
        # scope), so we issue the insert through the BYPASSRLS admin pool
        # directly — same pattern as ensure_bootstrap_user_row.
        async with admin_conn() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO users (tenant_id, name, email, role)
                VALUES ($1::uuid, $2, $3, $4)
                RETURNING id
                """,
                tenant_id,
                display_name,
                email,
                role,
            )
        assert row is not None
        user_id = str(row["id"])

    return AdminProvisionResult(
        user_id=user_id,
        role=role,
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
        email=email,
        external_sub=external_sub,
        created=True,
    )


async def ensure_seed_admin() -> AdminProvisionResult | None:
    """Env-seed wrapper: provision a platform_admin from env at startup.

    Off (returns ``None``) unless ``ROLEMESH_SEED_ADMIN_EMAIL`` is set,
    so it self-disables in deployments that don't opt in. When set it
    delegates to :func:`create_admin` (always ``platform_admin`` — for
    other roles use the CLI), reading the optional
    ``ROLEMESH_SEED_ADMIN_EXTERNAL_SUB`` / ``ROLEMESH_SEED_ADMIN_NAME``.
    Idempotent: an already-present admin is left untouched.

    Intended for managed / IaC deploys; the CLI remains the canonical
    interactive path.
    """
    email = os.environ.get(_SEED_EMAIL_ENV, "").strip()
    if not email:
        return None
    external_sub = os.environ.get(_SEED_EXTERNAL_SUB_ENV) or None
    name = os.environ.get(_SEED_NAME_ENV) or None
    result = await create_admin(
        email=email,
        role=PLATFORM_ADMIN_ROLE,
        external_sub=external_sub,
        name=name,
    )
    if result.created:
        logger.info(
            "Seeded platform_admin %s (user_id=%s)", email, result.user_id
        )
    else:
        logger.info(
            "Seed platform_admin already present: %s (user_id=%s)",
            email,
            result.user_id,
        )
    return result
