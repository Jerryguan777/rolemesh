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

from typing import TYPE_CHECKING

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Response

from rolemesh.core.backend_capabilities import BackendCompatError, validate_combo
from rolemesh.db import (
    create_coworker,
    delete_coworker,
    get_coworker,
    get_coworkers_for_tenant,
    get_model_by_id,
    tenant_has_credential_for_provider,
    update_coworker,
)
from webui.dependencies import get_current_user
from webui.schemas_v1 import Coworker, CoworkerCreate, CoworkerUpdate
from webui.v1 import coworker_events
from webui.v1.errors import ErrorResponseException, raise_error_response

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser

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
    # CREATE-side hot-reload: published the same way PATCH does so the
    # running orchestrator picks the new coworker up without a process
    # restart. ``reload_coworker_into_state`` already handles the
    # "first time we hear about this coworker" branch — see the docstring
    # in :mod:`rolemesh.orchestration.coworker_hot_reload`. Smoke-discovered
    # gap: without this publish, the orchestrator's in-memory state
    # missed CREATEd coworkers and ``_handle_incoming`` silently dropped
    # every inbound message routed at them.
    try:
        await coworker_events.publish_coworker_restart(
            coworker_id=cw.id,
            tenant_id=user.tenant_id,
        )
    except Exception:
        # Same best-effort posture as PATCH — DB row is the source of
        # truth, the next process boot picks it up.
        pass
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


async def _get_coworker_or_404(coworker_id: str, tenant_id: str) -> object:
    """Fetch one coworker; raise the v1 envelope on miss / wrong tenant.

    Catches ``DataError`` (raised when ``coworker_id`` is not a valid
    UUID) so callers don't have to special-case it; both the bad
    UUID case and the legitimate "not found" case present as the
    same 404 to the client. Surfacing them differently leaks the
    DB's parsing rules.
    """
    try:
        cw = await get_coworker(coworker_id, tenant_id=tenant_id)
    except asyncpg.DataError:
        cw = None
    if cw is None:
        raise_error_response(
            "NOT_FOUND",
            "Coworker not found.",
            status_code=404,
            details={"coworker_id": coworker_id},
        )
    return cw


@router.get("/{coworker_id}", response_model=Coworker)
async def get_coworker_endpoint(
    coworker_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Coworker:
    cw = await _get_coworker_or_404(coworker_id, user.tenant_id)
    return _coworker_to_response(cw)


@router.patch("/{coworker_id}", response_model=Coworker)
async def patch_coworker_endpoint(
    coworker_id: str,
    body: CoworkerUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Coworker:
    """Update selected fields on a coworker.

    A ``model_id`` change re-runs the same validation chain as POST
    and, on success, publishes ``web.coworker.restart`` on JetStream
    so the orchestrator hot-reloads the cached config (design §7).
    The publish is best-effort (logged but not failed) because the
    DB write has already committed by the time we hit that line —
    the orchestrator picks up the change on its next full restart
    even if the broadcast misses.
    """
    cw = await _get_coworker_or_404(coworker_id, user.tenant_id)
    model_changed = body.model_id is not None and body.model_id != cw.model_id

    if body.model_id is not None:
        # Validate against the *target* backend — either the new one
        # if the caller passed it (the v1 surface doesn't support
        # this today; the path is here so the same helper works for
        # both POST and PATCH) or the existing backend.
        backend_name = cw.agent_backend
        await _validate_model_and_credential(
            tenant_id=user.tenant_id,
            model_id=body.model_id,
            backend_name=backend_name,
        )

    kwargs: dict[str, object] = {}
    if body.name is not None:
        kwargs["name"] = body.name
    if body.system_prompt is not None:
        kwargs["system_prompt"] = body.system_prompt
    if body.status is not None:
        kwargs["status"] = body.status
    if body.max_concurrent is not None:
        kwargs["max_concurrent"] = body.max_concurrent
    if body.model_id is not None:
        kwargs["model_id"] = body.model_id

    try:
        updated = await update_coworker(
            coworker_id, tenant_id=user.tenant_id, **kwargs,  # type: ignore[arg-type]
        )
    except asyncpg.UniqueViolationError as exc:
        raise ErrorResponseException(
            status_code=409,
            code="RESOURCE_IN_USE",
            message="A coworker with this name/folder already exists in the tenant.",
            details={"constraint": getattr(exc, "constraint_name", "") or ""},
        ) from exc
    if updated is None:
        # The row vanished between the 404 check and the UPDATE —
        # treat as a normal 404 rather than 500 so a concurrent
        # DELETE doesn't surface as a server bug.
        raise_error_response(
            "NOT_FOUND",
            "Coworker not found.",
            status_code=404,
            details={"coworker_id": coworker_id},
        )
    if model_changed:
        await coworker_events.publish_coworker_restart(
            coworker_id=coworker_id, tenant_id=user.tenant_id,
        )
    return _coworker_to_response(updated)


@router.delete("/{coworker_id}", status_code=204)
async def delete_coworker_endpoint(
    coworker_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Delete a coworker. DB FK ON DELETE CASCADE handles dependents.

    Per design §3 "DELETE 语义" table, a coworker DELETE cascades to
    conversations / runs / messages. No 409 path on this endpoint —
    the design treats coworkers as roots of their own subtree.
    """
    await _get_coworker_or_404(coworker_id, user.tenant_id)
    await delete_coworker(coworker_id, tenant_id=user.tenant_id)
    return Response(status_code=204)
