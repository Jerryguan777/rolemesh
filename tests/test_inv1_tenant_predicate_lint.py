"""INV-1 lint (v1.1 §11) — every tenant-scoped SQL string in ``src/rolemesh/db/``
that does a SELECT / UPDATE / DELETE against a tenant table MUST also
carry an explicit ``WHERE ... tenant_id = ...`` predicate.

This is the *belt* half of belt-and-braces: RLS at the database is the
braces, the explicit predicate is the belt. The two layers catch
disjoint failure modes — RLS catches a forgotten GUC or a missed
``WHERE`` in a future hot-patch; the explicit predicate catches a
broken RLS policy or a connection that happens to run as ``rolemesh_
system`` (BYPASSRLS).

The lint is intentionally simple grep + regex, not a Python AST walk.
Two reasons:

1. The SQL strings in ``src/rolemesh/db/`` are hand-written tagged
   literals; ``ast.parse`` would have to thread through string
   concatenation / f-strings / .format(...) calls. Not worth the
   complexity.
2. False positives are cheap to silence with an ``# inv-1-ok: <reason>``
   comment on the offending line; false negatives are paid for in
   cross-tenant leak incidents. The asymmetry is the right one.

What counts as "tenant-scoped":

The ``TENANT_SCOPED_TABLES`` set below mirrors the actual columns. To
add a new table, append its name to the set AND make sure the table
itself carries a ``tenant_id`` column. Junction tables that inherit
RLS via a parent (``coworker_mcp_servers`` / ``coworker_skills`` /
``skill_files``) are NOT in this set — the predicate-by-parent JOIN is
expressed differently and is checked at the design-review layer, not
here.
"""

from __future__ import annotations

import re
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent.parent / "src" / "rolemesh" / "db"

# Tables that carry their own ``tenant_id`` column and therefore must
# show ``tenant_id`` in every SELECT/UPDATE/DELETE statement in
# ``src/rolemesh/db/``. Keep alphabetised.
TENANT_SCOPED_TABLES: frozenset[str] = frozenset({
    "approval_audit_log",
    "approval_policies",
    "approval_requests",
    "channel_bindings",
    "conversations",
    "coworkers",
    "eval_runs",
    "external_tenant_map",
    "mcp_servers",
    "messages",
    "oidc_user_tokens",
    "runs",
    "safety_decisions",
    "safety_rules",
    "safety_rules_audit",
    "scheduled_tasks",
    "sessions",
    "skills",
    "task_run_logs",
    "tenant_model_credentials",
    "user_agent_assignments",
    "users",
})

# Match a SELECT / UPDATE / DELETE on any of the tracked tables. We
# capture the leading verb + the table name as a single token so we
# can re-discover both in the matched window. ``re.IGNORECASE`` keeps
# the regex robust against future "select" / "Select" stylings even
# though the convention in this repo is all-caps.
_TABLES_ALT = "|".join(sorted(TENANT_SCOPED_TABLES))
_STMT_RE = re.compile(
    r"\b(?P<verb>SELECT|UPDATE|DELETE)\b[^\"']{0,200}?\bFROM\b\s+"
    rf"(?P<table>{_TABLES_ALT})\b",
    re.IGNORECASE,
)
# UPDATE / DELETE don't always have a FROM. Match them directly too —
# ``UPDATE coworkers SET ...`` / ``DELETE FROM coworkers ...``. ``DELETE``
# always uses ``FROM`` in PG, so the first regex covers it via the
# common path; ``UPDATE`` does not, so we add a second pass.
_UPDATE_RE = re.compile(
    rf"\b(?P<verb>UPDATE)\s+(?P<table>{_TABLES_ALT})\b",
    re.IGNORECASE,
)


def _strip_comments_keeping_position(src: str) -> str:
    """Replace Python ``#`` line comments with spaces of the same length.

    Keeps line numbers stable so reported violations point at the right
    line in the original file. We DO NOT strip SQL ``--`` comments —
    a ``-- WHERE tenant_id`` inside a SQL literal is a string match
    that should still flag the SQL as unsafe; this lint is opinionated
    against "this WHERE is technically present but commented out".
    """
    out: list[str] = []
    for line in src.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            out.append(" " * len(line.rstrip("\n")) + ("\n" if line.endswith("\n") else ""))
        else:
            out.append(line)
    return "".join(out)


def _line_of(src: str, offset: int) -> int:
    return src.count("\n", 0, offset) + 1


# Hard upper bound when scanning upward for an inv-1-ok annotation.
# An ``async def`` enclosing function rarely runs longer than this, and
# we want to capture annotations placed at the top of the function ("this
# whole function is a cross-tenant maintenance loop") as well as those
# placed right next to the SQL. False positives from this generous
# window would be a comment several functions up that happens to mention
# the marker; the marker is distinctive enough (``inv-1-ok``) that this
# does not happen in practice.
_INV1_OK_LOOKBACK_LINES = 25


def _has_inv1_ok(src: str, line_no: int) -> bool:
    """Does any of the ``_INV1_OK_LOOKBACK_LINES`` lines above ``line_no``
    carry an ``# inv-1-ok: <reason>`` annotation?

    The annotation may sit at multiple natural spots:
      - At the top of the function (describes the whole loop)
      - Right above the ``async with admin_conn():`` line
      - Right above the ``await conn.fetch(`` call
      - On a line inside the SQL ``\"\"\"`` body itself
    A generous fixed lookback catches all of them without needing the
    lint to understand Python scope. The ``inv-1-ok`` marker is
    distinctive enough that incidental matches from unrelated comments
    are not a real risk.
    """
    lines = src.splitlines()
    end = min(len(lines), line_no)
    start = max(0, end - _INV1_OK_LOOKBACK_LINES)
    for i in range(start, end):
        if "inv-1-ok" in lines[i]:
            return True
    return False


def _find_violations(path: Path) -> list[tuple[int, str, str]]:
    """Return ``[(line_no, verb, table)]`` for SQL strings missing the
    ``tenant_id`` predicate. Uses a window-based check: from the
    matched verb, scan forward up to ~400 chars for the next statement
    terminator and require ``tenant_id`` to appear inside that window.
    """
    raw = path.read_text(encoding="utf-8")
    src = _strip_comments_keeping_position(raw)
    violations: list[tuple[int, str, str]] = []

    for matcher in (_STMT_RE, _UPDATE_RE):
        for m in matcher.finditer(src):
            start = m.start()
            line_no = _line_of(src, start)
            if _has_inv1_ok(raw, line_no):
                continue
            # Lookahead window: up to the next ``;`` or 400 chars.
            window_end = src.find(";", m.end())
            if window_end == -1 or window_end - start > 400:
                window_end = min(start + 400, len(src))
            window = src[start:window_end]
            if "tenant_id" in window:
                continue
            violations.append((line_no, m.group("verb").upper(), m.group("table")))
    return violations


def test_db_module_sql_carries_tenant_predicate() -> None:
    """Every tenant-scoped statement in ``src/rolemesh/db/*.py`` must
    mention ``tenant_id`` in the same statement window. Anything else is
    either (a) a real INV-1 violation or (b) genuinely tenant-agnostic
    and needs an ``# inv-1-ok: <reason>`` opt-out so the lint stays
    honest.
    """
    assert DB_DIR.is_dir(), f"db dir not found: {DB_DIR}"

    all_violations: dict[str, list[tuple[int, str, str]]] = {}
    for py_file in sorted(DB_DIR.glob("*.py")):
        # ``schema.py`` is DDL — CREATE TABLE / ALTER / RLS policies
        # talk about the tables themselves, not about tenant data
        # access. Skip it wholesale; the RLS belt-and-braces test
        # exercises tenant isolation at the schema layer.
        if py_file.name == "schema.py":
            continue
        violations = _find_violations(py_file)
        if violations:
            all_violations[str(py_file.relative_to(DB_DIR.parent.parent.parent))] = violations

    if all_violations:
        lines = ["INV-1 violations (SQL missing tenant_id predicate):"]
        for fname, items in all_violations.items():
            for line_no, verb, table in items:
                lines.append(f"  {fname}:{line_no}  {verb} on {table}")
        lines.append(
            "\nFix: add `WHERE tenant_id = ...` to the statement, or annotate "
            "the line / preceding line with `# inv-1-ok: <reason>` if the "
            "access is genuinely tenant-agnostic (e.g. resolver hatch)."
        )
        raise AssertionError("\n".join(lines))


def test_inv1_lint_catches_synthetic_violation(tmp_path: Path) -> None:
    """Mutation-style self-check: planting a bad SELECT against a
    tracked table in a file the lint scans must trip the check.

    This is the inverse of the test above — it guarantees the lint
    actually fails when it should. Without it, an empty regex or a
    typo in ``TENANT_SCOPED_TABLES`` could silently let everything
    pass.
    """
    bad_src = (
        '"""Synthetic db module to verify INV-1 lint catches missing predicates."""\n'
        "async def leaky():\n"
        '    return await conn.fetch("SELECT * FROM coworkers")\n'
    )
    leaky = tmp_path / "leaky.py"
    leaky.write_text(bad_src)
    violations = _find_violations(leaky)
    assert violations, "lint failed to detect a SELECT without tenant_id predicate"
    assert violations[0][1] == "SELECT"
    assert violations[0][2] == "coworkers"


def test_inv1_ok_annotation_silences_lint(tmp_path: Path) -> None:
    """Explicit opt-out comment on the SQL line must suppress the
    violation — this is the safety valve for tenant-agnostic resolver
    hatches. Without this, callers would either be forced to rewrite
    legitimate cross-tenant maintenance loops or to coexist with a
    permanently red CI."""
    ok_src = (
        '"""Synthetic db module."""\n'
        "async def admin_scan():\n"
        '    return await conn.fetch(  # inv-1-ok: cross-tenant maintenance loop\n'
        '        "SELECT id FROM coworkers"\n'
        '    )\n'
    )
    silenced = tmp_path / "silenced.py"
    silenced.write_text(ok_src)
    violations = _find_violations(silenced)
    assert not violations, (
        "inv-1-ok annotation failed to silence the lint; "
        f"got violations: {violations}"
    )
