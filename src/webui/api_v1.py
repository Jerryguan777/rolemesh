"""``/api/v1`` router skeleton.

Phase 0 only ships the public ``GET /api/v1/backends`` endpoint, but
the prefixed router lives here so Phase 1+ endpoints can be hung off
it without touching ``webui/main.py`` again. Auth dependencies are
re-used from ``webui.dependencies`` per the design.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from rolemesh.core.backend_capabilities import backends_as_json

router = APIRouter(prefix="/api/v1")


@router.get("/backends")
async def get_backends(response: Response) -> list[dict[str, object]]:
    """Return the static backend × provider × family compatibility matrix.

    Public metadata: no auth required (per design §2.3 / §3 Phase 1).
    Frontends use this to render the per-coworker configuration form;
    a one-hour Cache-Control lets the browser skip the round-trip.
    """
    response.headers["Cache-Control"] = "max-age=3600"
    return backends_as_json()
