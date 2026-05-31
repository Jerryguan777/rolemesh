"""``/api/v1/models`` REST surface (read-only, design §3 Phase 2).

The platform model catalog is tenant-agnostic so we do not bind the
read paths to ``tenant_conn``; ``rolemesh.db.model`` already routes
these through ``admin_conn`` for the same reason. Admin write
surfaces (``POST`` / ``PATCH``) are deferred to v2 per design §14.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    ModelRow,
    get_model_by_id,
    list_models,
)
from webui.dependencies import get_current_user
from webui.schemas_v1 import Model, ModelFamily, ModelProvider
from webui.v1.errors import raise_error_response

router = APIRouter(prefix="/models", tags=["Models"])


def _model_to_response(m: ModelRow) -> Model:
    return Model(
        id=m.id,
        provider=m.provider,  # type: ignore[arg-type]
        model_id=m.model_id,
        model_family=m.model_family,  # type: ignore[arg-type]
        display_name=m.display_name,
        is_active=m.is_active,
        created_at=m.created_at.isoformat() if m.created_at is not None else None,
    )


@router.get("", response_model=list[Model])
async def list_models_endpoint(
    provider: ModelProvider | None = Query(default=None),
    family: ModelFamily | None = Query(default=None),
    _user: AuthenticatedUser = Depends(get_current_user),
) -> list[Model]:
    """List active platform models, optionally filtered.

    The ``_user`` dependency is present to anchor the endpoint to
    the bearer-token surface even though the response contents are
    tenant-agnostic — Phase 1 made every ``/api/v1`` route
    authenticated so a leaked bearer is the only way in.
    """
    rows = await list_models(provider=provider, family=family)
    return [_model_to_response(r) for r in rows]


@router.get("/{model_id}", response_model=Model)
async def get_model_endpoint(
    model_id: str,
    _user: AuthenticatedUser = Depends(get_current_user),
) -> Model:
    row = await get_model_by_id(model_id)
    if row is None:
        raise_error_response(
            "NOT_FOUND",
            "Model not found.",
            status_code=404,
            details={"model_id": model_id},
        )
    return _model_to_response(row)
