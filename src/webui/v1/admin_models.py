"""``/api/v1/admin/models`` REST surface (PR24).

The platform model catalog is read-everyone, write-operator-only. The
read surface lives at ``/api/v1/models`` and is open to any
authenticated user; this module owns the writes (POST / PATCH /
DELETE) behind a role check.

Why split read and write paths under different prefixes? The frontend
calls the read endpoints on every coworker-create dialog open; an
RBAC denial there would break the happy path for everyone. Keeping
the writes on a separate `/admin/models` path lets the SPA gate the
admin UI behind `me.role == "owner"` without having to special-case
403 responses on the read path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
from fastapi import APIRouter, Depends, Response

from rolemesh.db import (
    ModelRow,
    count_coworkers_using_model,
    create_model,
    get_model_by_id,
    soft_delete_model,
    update_model,
)
from webui.dependencies import get_current_user
from webui.schemas_v1 import Model, ModelCreate, ModelUpdate
from webui.v1.errors import ErrorResponseException, raise_error_response

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser

router = APIRouter(prefix="/admin/models", tags=["Admin"])


def _to_response(m: ModelRow) -> Model:
    return Model(
        id=m.id,
        provider=m.provider,  # type: ignore[arg-type]
        model_id=m.model_id,
        model_family=m.model_family,  # type: ignore[arg-type]
        display_name=m.display_name,
        is_active=m.is_active,
        created_at=m.created_at.isoformat() if m.created_at is not None else None,
    )


def _require_owner(user: AuthenticatedUser) -> None:
    """Enforce the role gate for write operations.

    Spelled out at the top of every write handler rather than via a
    FastAPI dependency so the role rule reads next to the operation it
    guards — easier to audit when a future PR adds a new write
    endpoint and forgets to wire the dependency.
    """
    if user.role != "owner":
        raise ErrorResponseException(
            status_code=403,
            code="FORBIDDEN",
            message="Only tenant owners may mutate the platform model catalog.",
        )


@router.post("", response_model=Model, status_code=201)
async def create_model_endpoint(
    body: ModelCreate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Model:
    _require_owner(user)
    try:
        row = await create_model(
            provider=body.provider,
            model_id=body.model_id,
            model_family=body.model_family,
            display_name=body.display_name,
            is_active=body.is_active,
        )
    except asyncpg.UniqueViolationError as exc:
        raise ErrorResponseException(
            status_code=409,
            code="RESOURCE_IN_USE",
            message=(
                f"A model already exists with provider={body.provider!r} "
                f"and model_id={body.model_id!r}."
            ),
            details={"provider": body.provider, "model_id": body.model_id},
        ) from exc
    return _to_response(row)


@router.patch("/{model_id}", response_model=Model)
async def update_model_endpoint(
    model_id: str,
    body: ModelUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Model:
    _require_owner(user)
    try:
        updated = await update_model(
            model_id,
            display_name=body.display_name,
            is_active=body.is_active,
        )
    except asyncpg.DataError:
        updated = None
    if updated is None:
        raise_error_response(
            "NOT_FOUND",
            "Model not found.",
            status_code=404,
            details={"model_id": model_id},
        )
    return _to_response(updated)


@router.delete("/{model_id}", status_code=204)
async def delete_model_endpoint(
    model_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    _require_owner(user)
    # Confirm the model exists before checking usage — that gives a
    # distinct 404 vs 409 path so the SPA can tell "this model is
    # gone" from "this model is still in use".
    try:
        existing = await get_model_by_id(model_id)
    except asyncpg.DataError:
        existing = None
    if existing is None:
        raise_error_response(
            "NOT_FOUND",
            "Model not found.",
            status_code=404,
            details={"model_id": model_id},
        )
    in_use = await count_coworkers_using_model(model_id)
    if in_use > 0:
        raise ErrorResponseException(
            status_code=409,
            code="RESOURCE_IN_USE",
            message=(
                f"Model is bound to {in_use} coworker(s); "
                "reassign or delete them before retiring the model."
            ),
            details={"model_id": model_id, "coworker_count": in_use},
        )
    # If we get here the model exists and has zero bindings; the
    # soft-delete may still return False if a concurrent caller
    # already set is_active = false. Idempotent: treat that case as a
    # successful no-op (204).
    await soft_delete_model(model_id)
    return Response(status_code=204)
