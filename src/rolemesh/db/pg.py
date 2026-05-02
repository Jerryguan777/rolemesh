"""Backwards-compatible facade — re-exports the split data layer.

Pre-refactor this module was a 4910-line catch-all. The implementation
now lives in:

* ``rolemesh.db._pool``    — connection pools + lifecycle
* ``rolemesh.db.schema``   — DDL (CREATE TABLE / RLS policies)
* ``rolemesh.db.tenant``   — Tenant CRUD
* ``rolemesh.db.user``     — User CRUD + OIDC token vault
* ``rolemesh.db.coworker`` — Coworker CRUD + user-agent assignments
* ``rolemesh.db.chat``     — ChannelBinding / Conversation / Session / Message
* ``rolemesh.db.task``     — Scheduled tasks + run logs
* ``rolemesh.db.legacy``   — Migration helpers (RegisteredGroup, sessions_legacy)
* ``rolemesh.db.approval`` — Approval policies / requests / audit
* ``rolemesh.db.safety``   — Safety rules / decisions / audit
* ``rolemesh.db.skill``    — Skills + skill files

This shim re-exports their public APIs so existing
``from rolemesh.db.pg import X`` sites continue to work unchanged. New
code should import from ``rolemesh.db`` directly; this shim is removed
in PR2 once all call sites are migrated.
"""

from __future__ import annotations

from rolemesh.db._pool import *  # noqa: F403
from rolemesh.db.approval import *  # noqa: F403
from rolemesh.db.chat import *  # noqa: F403
from rolemesh.db.coworker import *  # noqa: F403
from rolemesh.db.legacy import *  # noqa: F403
from rolemesh.db.safety import *  # noqa: F403
from rolemesh.db.schema import *  # noqa: F403
from rolemesh.db.skill import *  # noqa: F403
from rolemesh.db.task import *  # noqa: F403
from rolemesh.db.tenant import *  # noqa: F403
from rolemesh.db.user import *  # noqa: F403
