"""``/api/v1/platform/tenants`` REST surface — tenant lifecycle.

Platform-plane only: every handler is gated on ``platform.tenant.manage``,
which lives in ``_PLATFORM_ONLY_ACTIONS`` so no tenant role can reach it
(see :mod:`rolemesh.auth.permissions`). This is where the platform operator
runs a tenant *as a customer* — provision a new one, list/inspect all, and
suspend/resume — as opposed to ``/api/v1/tenant`` where a tenant owner edits
their OWN settings.

All DB access goes through ``admin_conn`` (BYPASSRLS, cross-tenant): the
caller has no single-tenant auth context here and must see/operate on every
tenant. Suspending a tenant takes effect at two enforcement points elsewhere
(a single authentication chokepoint and the scheduler), not in this module —
here we only flip ``tenants.status``.

The reserved sentinel tenant ``__platform__`` (which anchors platform_admin
rows, see :mod:`rolemesh.admin.core`) is never a customer: provision cannot
recreate it and suspend/resume refuse to touch it, so the platform operators'
own accounts can never be locked out via this surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends

from rolemesh import db
from rolemesh.admin.core import PLATFORM_TENANT_SLUG
from rolemesh.core.logger import get_logger
from webui.dependencies import require_action
from webui.schemas_v1 import (
    PlatformTenantProvision,
    PlatformTenantResponse,
)
from webui.v1.errors import raise_error_response

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser
    from rolemesh.core.types import Tenant

logger = get_logger()

router = APIRouter(prefix="/platform/tenants", tags=["Platform"])


def _to_response(t: Tenant) -> PlatformTenantResponse:
    return PlatformTenantResponse(
        id=t.id,
        name=t.name,
        slug=t.slug,
        plan=t.plan,
        max_concurrent_containers=t.max_concurrent_containers,
        status=t.status,  # type: ignore[arg-type]
        created_at=t.created_at,
    )


def _reject_if_sentinel(tenant: Tenant) -> None:
    """Refuse lifecycle operations on the reserved ``__platform__`` tenant.

    Matched by slug — the sentinel is created with this exact slug and it is
    not a legal user-chosen slug, so a real customer can never collide with
    it. Raises the design §13 envelope (403) rather than silently no-op'ing
    so the caller learns the operation was refused, not applied.
    """
    if tenant.slug == PLATFORM_TENANT_SLUG:
        raise_error_response(
            "FORBIDDEN",
            "The reserved platform tenant cannot be managed as a customer.",
            status_code=403,
            details={"slug": PLATFORM_TENANT_SLUG},
        )


async def _get_or_404(tenant_id: str) -> Tenant:
    tenant = await db.get_tenant(tenant_id)
    if tenant is None:
        raise_error_response(
            "NOT_FOUND",
            "Tenant not found.",
            status_code=404,
            details={"id": tenant_id},
        )
    return tenant


@router.get("", response_model=list[PlatformTenantResponse])
async def list_tenants_endpoint(
    user: AuthenticatedUser = Depends(require_action("platform.tenant.manage")),
) -> list[PlatformTenantResponse]:
    """List every tenant (including the sentinel and any suspended ones)."""
    tenants = await db.get_all_tenants()
    return [_to_response(t) for t in tenants]


@router.post("", response_model=PlatformTenantResponse, status_code=201)
async def provision_tenant_endpoint(
    body: PlatformTenantProvision,
    user: AuthenticatedUser = Depends(require_action("platform.tenant.manage")),
) -> PlatformTenantResponse:
    """Provision a new tenant. It starts ``active`` (schema default).

    A caller-supplied slug colliding with the reserved sentinel is rejected
    before any write; a slug already taken by a real tenant surfaces as a
    409 rather than a raw uniqueness traceback.
    """
    if body.slug == PLATFORM_TENANT_SLUG:
        raise_error_response(
            "FORBIDDEN",
            "The reserved platform slug cannot be provisioned.",
            status_code=403,
            details={"slug": PLATFORM_TENANT_SLUG},
        )
    if body.slug is not None and await db.get_tenant_by_slug(body.slug) is not None:
        raise_error_response(
            "CONFLICT",
            "A tenant with this slug already exists.",
            status_code=409,
            details={"slug": body.slug},
        )
    tenant = await db.create_tenant(name=body.name, slug=body.slug)
    logger.info("Tenant provisioned", tenant_id=tenant.id, slug=tenant.slug)
    return _to_response(tenant)


@router.get("/{tenant_id}", response_model=PlatformTenantResponse)
async def get_tenant_endpoint(
    tenant_id: str,
    user: AuthenticatedUser = Depends(require_action("platform.tenant.manage")),
) -> PlatformTenantResponse:
    return _to_response(await _get_or_404(tenant_id))


@router.post("/{tenant_id}/suspend", response_model=PlatformTenantResponse)
async def suspend_tenant_endpoint(
    tenant_id: str,
    user: AuthenticatedUser = Depends(require_action("platform.tenant.manage")),
) -> PlatformTenantResponse:
    """Suspend a tenant: its users fail auth and its tasks stop running.

    Idempotent — suspending an already-suspended tenant just returns it.
    The sentinel tenant is refused so platform operators cannot lock
    themselves out.
    """
    tenant = await _get_or_404(tenant_id)
    _reject_if_sentinel(tenant)
    updated = await db.set_tenant_status(tenant_id, "suspended")
    assert updated is not None  # row existed at _get_or_404 a moment ago
    logger.info("Tenant suspended", tenant_id=tenant_id)
    return _to_response(updated)


@router.post("/{tenant_id}/resume", response_model=PlatformTenantResponse)
async def resume_tenant_endpoint(
    tenant_id: str,
    user: AuthenticatedUser = Depends(require_action("platform.tenant.manage")),
) -> PlatformTenantResponse:
    """Resume a suspended tenant back to ``active`` (suspend is reversible).

    Idempotent. The sentinel is never suspended in the first place, so the
    same refusal applies for symmetry rather than letting it look managed.
    """
    tenant = await _get_or_404(tenant_id)
    _reject_if_sentinel(tenant)
    updated = await db.set_tenant_status(tenant_id, "active")
    assert updated is not None
    logger.info("Tenant resumed", tenant_id=tenant_id)
    return _to_response(updated)
