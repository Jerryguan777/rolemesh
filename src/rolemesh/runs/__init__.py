"""``runs`` lifecycle — the single writer for the ``runs`` table.

Design plan critique §3 anchors the contract: rows in ``runs`` are
INSERTed by *the service that triggered the run* (the WebUI's WS
handler on browser triggers; the scheduler on cron triggers). The
agent container never INSERTs — it only UPDATEs terminal columns
via :func:`update_run_terminal`. Keeping the writer side narrow is
the only way INV-6 ("every terminal path UPDATEs status /
completed_at / usage") stays auditable.

01b builds the actual WS plumbing on top of these helpers; 01a only
ships the helpers themselves so 01b can stand them up without also
re-litigating who's allowed to INSERT.
"""

from rolemesh.runs.lifecycle import (
    create_run,
    get_run,
    update_run_terminal,
)

__all__ = ["create_run", "get_run", "update_run_terminal"]
