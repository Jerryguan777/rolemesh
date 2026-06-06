"""Shared pagination conventions for ``/api/v1`` list endpoints.

Every unbounded tenant-scoped collection pages with offset/limit and
returns a named ``XPage`` envelope ``{items, total, limit, offset}``
(the concrete envelopes live in :mod:`webui.schemas_v1`). The
query-parameter types here keep the default and the hard cap in one
place so every list handler agrees — a client can never pull an
unbounded array, and the response echoes ``limit``/``offset`` so the
caller doesn't have to track them itself.

Bounded sub-resources (e.g. a skill's files, a coworker's bindings)
stay as bare arrays — they don't grow with tenant size. Append-only
high-growth histories (conversation messages) use cursor pagination
instead; see the ``*Page`` cursor envelopes in :mod:`webui.schemas_v1`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Query

# One source of truth for the offset/limit window. 200 matches the cap the
# safety-decisions endpoint shipped with; 50 is a sensible default page.
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200

LimitParam = Annotated[
    int,
    Query(
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Maximum number of items to return (default 50, max 200).",
    ),
]
OffsetParam = Annotated[
    int,
    Query(
        ge=0,
        description="Number of items to skip from the start of the ordered set.",
    ),
]
