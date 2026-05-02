"""Data layer — PostgreSQL persistence.

Public API is the union of all entity submodules' public APIs. Import
from ``rolemesh.db`` directly:

    from rolemesh.db import create_coworker, tenant_conn

The ``rolemesh.db.pg`` shim still works for legacy ``from
rolemesh.db.pg import X`` call sites and is removed once they are
migrated.
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
