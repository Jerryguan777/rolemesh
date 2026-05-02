"""Data layer — PostgreSQL persistence.

Public API is the union of all entity submodules' public APIs. Import
from ``rolemesh.db`` directly:

    from rolemesh.db import create_coworker, tenant_conn

Entity-scoped imports (``rolemesh.db.coworker``, ``rolemesh.db.safety``,
…) are also valid; the package-level surface is the back-compat target
for the historic ``rolemesh.db.pg`` shim, which has been removed.
"""

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
