"""``/api/v1/coworkers`` REST surface (design §3 Phase 1).

The router lives at module scope so :mod:`webui.api_v1` can mount
it under the ``/api/v1`` prefix. Independent from the legacy
``/api/admin/agents/*`` surface: no helpers are imported from
:mod:`webui.admin` — shared logic lives in :mod:`rolemesh.db.*`.

Validation chain on create / update is the load-bearing piece:

1. ``model_id`` must point at an active row in ``models``.
2. The tenant must already have a credential row for the model's
   provider (``MISSING_CREDENTIAL`` 422 otherwise).
3. ``(agent_backend × model.provider × model.family)`` must be a
   supported triple (``BACKEND_INCOMPAT`` via
   :func:`rolemesh.core.backend_capabilities.validate_combo`).
4. ``name`` is UNIQUE per tenant — surfaced as 409.

A coworker with no ``model_id`` is still allowed (a tenant may
defer the model choice until first run), but the moment one is
attached the chain above runs in full.
"""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.core.backend_capabilities import BackendCompatError, validate_combo
from rolemesh.db import (
    create_coworker,
    get_coworkers_for_tenant,
    get_model_by_id,
    tenant_has_credential_for_provider,
)
from webui.dependencies import get_current_user
from webui.schemas_v1 import Coworker, CoworkerCreate
from webui.v1.errors import ErrorResponseException, raise_error_response

router = APIRouter(prefix="/coworkers", tags=["Coworkers"])


def _coworker_to_response(cw: object) -> Coworker:
    """Project a :class:`rolemesh.core.types.Coworker` to the wire model.

    Kept as a free function (not a method on the Pydantic model) so
    handlers can pre-fetch and project without spinning up a
    ``BaseModel`` per row in the listing path.
    """
    # Late attribute access — ``rolemesh.core.types.Coworker`` is a
    # dataclass; spelling each field out so a contract drift trips
    # mypy/pyright instead of silently dropping.
    return Coworker(
        id=cw.id,
        tenant_id=cw.tenant_id,
        name=cw.name,
        folder=cw.folder,
        agent_backend=cw.agent_backend,  # type: ignore[arg-type]
        model_id=cw.model_id,
        system_prompt=cw.system_prompt,
        status=cw.status,  # type: ignore[arg-type]
        agent_role=cw.agent_role,  # type: ignore[arg-type]
        max_concurrent=cw.max_concurrent,
        created_by_user_id=cw.created_by_user_id,
        created_at=cw.created_at,
    )


async def _validate_model_and_credential(
    tenant_id: str, model_id: str, backend_name: str
) -> None:
    """Run steps 1-3 of the create / update validation chain.

    Raises an ``ErrorResponseException`` with the appropriate envelope
    on failure; returns ``None`` on success. Step 4 (name uniqueness)
    happens at INSERT time as an ``asyncpg.UniqueViolationError`` —
    the helper here can't know whether the tenant is in a duplicate
    state without reading the same column the INSERT would.
    """
    model = await get_model_by_id(model_id)
    if model is None or not model.is_active:
        raise_error_response(
            "MODEL_NOT_FOUND",
            f"Model {model_id!r} not found or inactive.",
            status_code=422,
            details={"model_id": model_id},
        )
    if not await tenant_has_credential_for_provider(tenant_id, model.provider):
        raise_error_response(
            "MISSING_CREDENTIAL",
            (
                f"Tenant has no credential for provider {model.provider!r}; "
                "configure one before attaching this model."
            ),
            status_code=422,
            details={"provider": model.provider, "model_id": model_id},
        )
    try:
        validate_combo(backend_name, model.provider, model.model_family)
    except BackendCompatError as exc:
        raise_error_response(
            BackendCompatError.code,
            str(exc),
            status_code=422,
            details={
                "agent_backend": backend_name,
                "provider": model.provider,
                "model_family": model.model_family,
            },
        )


@router.get("", response_model=list[Coworker])
async def list_coworkers(
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[Coworker]:
    """List coworkers visible to the caller's tenant.

    No filtering by role today — Phase 1 surfaces every row in the
    tenant. Visibility scoping (``created_by_user_id``) is a Phase 2
    concern per design §8.
    """
    cws = await get_coworkers_for_tenant(user.tenant_id)
    return [_coworker_to_response(c) for c in cws]


@router.post("", response_model=Coworker, status_code=201)
async def create_coworker_endpoint(
    body: CoworkerCreate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Coworker:
    if body.model_id is not None:
        await _validate_model_and_credential(
            tenant_id=user.tenant_id,
            model_id=body.model_id,
            backend_name=body.agent_backend,
        )
    try:
        cw = await create_coworker(
            tenant_id=user.tenant_id,
            name=body.name,
            folder=body.folder,
            agent_backend=body.agent_backend,
            system_prompt=body.system_prompt,
            max_concurrent=body.max_concurrent,
            agent_role=body.agent_role,
            model_id=body.model_id,
            created_by_user_id=(
                user.user_id if _looks_like_uuid(user.user_id) else None
            ),
        )
    except asyncpg.UniqueViolationError as exc:
        # ``coworkers`` has UNIQUE (tenant_id, folder); ``name`` is
        # not currently UNIQUE in the DB but the design treats it as
        # tenant-unique for UX. Surface either as RESOURCE_IN_USE
        # with the offending constraint name in details — clients get
        # enough to decide which field to flag.
        raise ErrorResponseException(
            status_code=409,
            code="RESOURCE_IN_USE",
            message="A coworker with this name/folder already exists in the tenant.",
            details={"constraint": getattr(exc, "constraint_name", "") or ""},
        ) from exc
    except HTTPException:
        raise
    return _coworker_to_response(cw)


def _looks_like_uuid(value: str) -> bool:
    """Return True when ``value`` is a 36-char UUID-ish string.

    The bootstrap fast-path (single-token mode) sets ``user_id`` to
    the literal ``"bootstrap"`` — not a UUID — and that value would
    fail the FK on ``coworkers.created_by_user_id``. The
    multi-user bootstrap path upserts a real ``users`` row so its
    UUID is safe to attribute. This guard lets the v1 endpoint
    accept both auth modes without paying the FK violation cost.
    """
    if len(value) != 36:
        return False
    parts = value.split("-")
    return len(parts) == 5 and all(
        all(c in "0123456789abcdefABCDEF" for c in p) for p in parts
    )
