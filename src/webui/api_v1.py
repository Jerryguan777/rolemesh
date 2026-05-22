"""``/api/v1`` router skeleton.

Each Phase-1 endpoint set lives in its own submodule under
:mod:`webui.v1`. This module composes them under the
``/api/v1`` prefix and registers the design §13 error-envelope
handler.

The router itself stays thin: real handlers go in the submodules
so the per-endpoint test files import a single FastAPI app fixture
without dragging in unrelated cross-section.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from rolemesh.core.backend_capabilities import backends_as_json
from webui.schemas_v1 import Backend
from webui.v1.approval_policies import router as approval_policies_router
from webui.v1.approvals import router as approvals_router
from webui.v1.auth import me_router as auth_me_router
from webui.v1.auth import router as auth_router
from webui.v1.conversations import (
    conversations_router,
    coworker_conversations_router,
)
from webui.v1.coworker_mcp import router as coworker_mcp_router
from webui.v1.coworkers import router as coworkers_router
from webui.v1.credentials import router as credentials_router
from webui.v1.mcp_servers import router as mcp_servers_router
from webui.v1.models import router as models_router
from webui.v1.runs import router as runs_router
from webui.v1.skills import (
    coworker_skills_router,
    skills_router,
)

router = APIRouter(prefix="/api/v1")


@router.get(
    "/backends",
    response_model=list[Backend],
    summary="Static backend × provider × family compatibility matrix",
)
async def get_backends(response: Response) -> list[dict[str, object]]:
    """Return the static backend × provider × family compatibility matrix.

    Public metadata: no auth required (per design §2.3 / §3 Phase 1).
    Frontends use this to render the per-coworker configuration form;
    a one-hour Cache-Control lets the browser skip the round-trip.

    The ``response_model`` parameter ties this handler to ``Backend``
    (defined in :mod:`webui.schemas_v1`) so FastAPI validates the
    actual payload against the OpenAPI contract at every call —
    catching the failure locally instead of letting the frontend's
    typed client crash on a drifted field.
    """
    response.headers["Cache-Control"] = "max-age=3600"
    return backends_as_json()


router.include_router(auth_router)
router.include_router(auth_me_router)
router.include_router(coworkers_router)
router.include_router(coworker_mcp_router)
router.include_router(coworker_conversations_router)
router.include_router(conversations_router)
router.include_router(credentials_router)
router.include_router(mcp_servers_router)
router.include_router(models_router)
router.include_router(runs_router)
router.include_router(approval_policies_router)
router.include_router(approvals_router)
router.include_router(skills_router)
router.include_router(coworker_skills_router)
