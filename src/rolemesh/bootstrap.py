"""Process-entry bootstrap: load ``.env`` into ``os.environ`` once.

Two usage patterns:
  * **Explicit** (``from rolemesh.bootstrap import load_env;
    load_env()``): function call, deterministic order.
  * **Side-effect-on-import** (``import rolemesh.bootstrap``):
    same behaviour, achieves the same result without a dangling
    function call that trips ruff's E402 on downstream imports.
    Safe because ``load_env()`` is idempotent.

Why this module lives at the top of the ``rolemesh`` package rather
than under ``rolemesh.core``: ``rolemesh/core/__init__.py`` eagerly
imports ``core.config``, which captures os.environ at module-level.
Importing ``rolemesh.core.anything`` triggers that eager import and
the config values get frozen BEFORE bootstrap has a chance to run.
Living at ``rolemesh.bootstrap`` dodges the ordering trap because
``rolemesh/__init__.py`` itself has no side effects.

Why this module exists
----------------------

Pre-fix, RoleMesh had two parallel configuration sources: some modules
called ``read_env_file()`` (reads ``.env`` for a hard-coded key
whitelist) and some called ``os.environ.get()`` directly. Operators
who wrote secrets into ``.env`` expected them to apply everywhere;
they didn't, and the boundary was invisible. The observed symptom was
WebSocket handshakes rejecting valid bootstrap tokens because
``ADMIN_BOOTSTRAP_TOKEN`` was only read from the process environment
and the user had never ``source .env``'d their shell.

The fix picks one source of truth — ``os.environ`` — and hooks
``.env`` into it at process start via ``python-dotenv``. Every
``os.environ.get(...)`` call site in the codebase then sees the same
values, and ``read_env_file`` is deleted.

Contract
--------

* ``load_env()`` is idempotent and safe to call multiple times.
* ``override=False``: existing ``os.environ`` values (set by systemd,
  docker --env-file, K8s envFrom, or a manual shell export) win over
  ``.env``. This matches the unix principle that explicit action
  trumps implicit config.
* Call ``load_env()`` FIRST at each process entry (before importing
  any module that captures env values at import time like
  ``core/config.py``). Tests deliberately do NOT auto-run this — they
  bring their own environment.

Production deployments without a ``.env`` file hit a silent no-op:
``load_dotenv`` finds nothing, ``os.environ`` already has values
injected by the process manager, and the rest of the code reads the
same way regardless.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


def _find_dotenv() -> Path | None:
    """Walk CWD + this file's parents looking for a ``.env``.

    Covers the ``uv run rolemesh`` case (CWD at repo root) AND unusual
    invocations (e.g. a systemd service with a different CWD) where
    we still want to find the ``.env`` shipped with the install.

    Returns ``None`` when no ``.env`` exists anywhere — that's the
    normal production case.
    """
    candidates: list[Path] = [Path.cwd()]
    here = Path(__file__).resolve()
    # src/rolemesh/core/bootstrap.py → parents[3] == repo root
    if len(here.parents) > 3:
        candidates.append(here.parents[3])
    for root in candidates:
        candidate = root / ".env"
        if candidate.is_file():
            return candidate
    return None


_loaded = False


def load_env() -> None:
    """Idempotent ``.env`` loader.

    Finds and loads the first ``.env`` found near CWD or the installed
    package. Subsequent calls are no-ops so it's safe to call from
    both ``rolemesh.main`` and ``webui.main`` entry points in the
    same process (rare but possible in tests).
    """
    global _loaded
    if _loaded:
        return
    dotenv_path = _find_dotenv()
    if dotenv_path is not None:
        load_dotenv(dotenv_path=dotenv_path, override=False)
    else:
        # No file — still call load_dotenv for its "find on
        # PYTHONPATH" default behaviour. Harmless when nothing's
        # there; useful if a user drops a .env in a non-standard
        # place we didn't probe.
        load_dotenv(override=False)
    _loaded = True


__all__ = ["load_env"]


# Side-effect-on-import entrypoint. Importing this module runs
# load_env() exactly once. Entry-point files prefer this over an
# explicit function call because it avoids a non-import statement at
# module scope (ruff E402 would flag every subsequent import
# otherwise). Tests that need to keep their own environment should
# not import this module at all, or should set their env BEFORE
# importing rolemesh.
load_env()

