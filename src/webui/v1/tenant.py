"""``/api/v1/tenant`` REST surface — tenant settings (owner only).

Equivalence migration of the legacy ``/api/admin/tenant`` GET/PATCH
(``webui.admin``) onto the ``/api/v1`` conventions: same DB calls and
response shape, gated by the owner-only ``tenant.manage`` capability,
returning the design §13 error envelope.

This resource only carries tenant *settings*; per-tenant credentials are
a separate top-level resource at ``/api/v1/credentials``
(:mod:`webui.v1.credentials`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends

from rolemesh import db
from webui.dependencies import require_action
from webui.schemas import TenantResponse, TenantUpdate
from webui.v1.errors import raise_error_response

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser
    from rolemesh.core.types import Tenant

router = APIRouter(prefix="/tenant", tags=["Tenant"])


def _tenant_to_response(t: Tenant) -> TenantResponse:
    return TenantResponse(
        id=t.id,
        name=t.name,
        slug=t.slug,
        plan=t.plan,
        max_concurrent_containers=t.max_concurrent_containers,
        created_at=t.created_at,
    )


@router.get("", response_model=TenantResponse)
async def get_tenant(
    user: AuthenticatedUser = Depends(require_action("tenant.manage")),
) -> TenantResponse:
    tenant = await db.get_tenant(user.tenant_id)
    if tenant is None:
        raise_error_response("NOT_FOUND", "Tenant not found.", status_code=404)
    return _tenant_to_response(tenant)


@router.patch("", response_model=TenantResponse)
async def update_tenant(
    body: TenantUpdate,
    user: AuthenticatedUser = Depends(require_action("tenant.manage")),
) -> TenantResponse:
    tenant = await db.update_tenant(
        user.tenant_id,
        name=body.name,
        max_concurrent_containers=body.max_concurrent_containers,
    )
    if tenant is None:
        raise_error_response("NOT_FOUND", "Tenant not found.", status_code=404)
    return _tenant_to_response(tenant)
