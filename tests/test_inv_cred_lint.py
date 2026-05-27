"""INV-CRED-1 lint — no host env LLM credential reads in ``src/rolemesh/egress/``.

After the per-tenant credential migration (docs/config-drift-fix-plan
§3 D1), LLM API keys live in ``tenant_model_credentials`` and are
resolved per-request by
:class:`rolemesh.egress.credentials.CredentialResolver`. Any
``os.environ.get(...)`` of an LLM-key-shaped name inside
``src/rolemesh/egress/`` is a regression that re-introduces the
single-tenant bug — the gateway would silently use a host-wide key
instead of the requesting tenant's row.

Implementation:

The lint is grep + regex, deliberately, for the same reason the
sibling INV-1 lint
(:mod:`tests.test_inv1_tenant_predicate_lint`) is: env-read calls
are verbatim string literals, no AST walk needed. False positives
get a one-line ``# inv-cred-ok: <reason>`` annotation; false
negatives would be paid for in a cross-tenant credential leak.

Forbidden name shapes (suffix / substring match):

    *_API_KEY           — ANTHROPIC_API_KEY, PI_OPENAI_API_KEY, ...
    *_AUTH_TOKEN        — ANTHROPIC_AUTH_TOKEN, ...
    *_OAUTH_TOKEN       — CLAUDE_CODE_OAUTH_TOKEN, ...
    *BEARER*            — AWS_BEARER_TOKEN_BEDROCK, ...

Names NOT flagged (intentional — these are infra, not credentials):
``CREDENTIAL_VAULT_KEY`` (master Fernet key), ``*_BASE_URL`` (per-
deployment upstream overrides), ``EGRESS_UPSTREAM_DNS``.

Scope:

Limited to ``src/rolemesh/egress/`` because credential injection is
the egress proxy's contract. Other modules (``src/rolemesh/agent/``,
``src/rolemesh/container/``, etc.) legitimately read host env vars
for non-credential infra and have their own design constraints.
Expanding the scope is an independent chore.
"""

from __future__ import annotations

import re
from pathlib import Path

EGRESS_DIR = Path(__file__).resolve().parent.parent / "src" / "rolemesh" / "egress"

# Match the three idiomatic host-env reads — ``os.environ.get(...)`` /
# ``os.environ[...]`` / ``os.getenv(...)`` — and also the aliased form
# ``_os.environ.get(...)`` that ``reverse_proxy.py`` used historically.
# Only matches string-literal names; ``os.environ.get(var_name)`` slips
# through (a variable name could hide anything). That's an acceptable
# false negative: it requires deliberate obfuscation, which a reviewer
# would catch.
_KEY_READ_RE = re.compile(
    r"""
    (?:os|_os)\.
    (?:environ\.get|environ|getenv)
    \s* [\(\[] \s*
    ["']
    (?P<name>[A-Z_][A-Z0-9_]*)
    ["']
    """,
    re.VERBOSE,
)


def _is_forbidden_key(name: str) -> bool:
    """Does ``name`` look like an LLM credential env var?"""
    if name.endswith("_API_KEY"):
        return True
    if name.endswith("_AUTH_TOKEN"):
        return True
    if name.endswith("_OAUTH_TOKEN"):
        return True
    return "BEARER" in name


# A short window is enough — env reads are usually a single line and
# the opt-out comment sits directly above (or on) the call. INV-1's
# lookback of 25 lines is for multi-line SQL literals where the
# annotation can naturally land at the top of the enclosing function;
# host env reads have no such structure.
_INV_CRED_OK_LOOKBACK_LINES = 3


def _line_of(src: str, offset: int) -> int:
    return src.count("\n", 0, offset) + 1


def _has_inv_cred_ok(src: str, line_no: int) -> bool:
    """Does any of the lines in [line_no - LOOKBACK, line_no] carry
    an ``# inv-cred-ok: <reason>`` annotation?
    """
    lines = src.splitlines()
    end = min(len(lines), line_no)
    start = max(0, end - _INV_CRED_OK_LOOKBACK_LINES)
    return any("inv-cred-ok" in lines[i] for i in range(start, end))


def _find_violations(path: Path) -> list[tuple[int, str]]:
    """Return ``[(line_no, key_name), ...]`` for forbidden host-env reads.

    Pure file grep — does not import the target module, so a syntax
    error or import-time side effect in the scanned file can't break
    the lint.
    """
    src = path.read_text(encoding="utf-8")
    violations: list[tuple[int, str]] = []
    for m in _KEY_READ_RE.finditer(src):
        name = m.group("name")
        if not _is_forbidden_key(name):
            continue
        line_no = _line_of(src, m.start())
        if _has_inv_cred_ok(src, line_no):
            continue
        violations.append((line_no, name))
    return violations


def test_egress_module_has_no_host_env_llm_key_reads() -> None:
    """INV-CRED-1 over the live ``src/rolemesh/egress/`` tree.

    Every match of an LLM-key-shaped name in an ``os.environ.get`` /
    ``os.environ[...]`` / ``os.getenv`` call inside this directory is
    a regression. Annotate with ``# inv-cred-ok: <reason>`` only when
    a non-credential use of the same name is genuinely needed (none
    expected today).
    """
    assert EGRESS_DIR.is_dir(), f"egress dir not found: {EGRESS_DIR}"

    all_violations: dict[str, list[tuple[int, str]]] = {}
    for py_file in sorted(EGRESS_DIR.rglob("*.py")):
        violations = _find_violations(py_file)
        if violations:
            rel = py_file.relative_to(EGRESS_DIR.parent.parent.parent)
            all_violations[str(rel)] = violations

    if all_violations:
        lines = [
            "INV-CRED-1 violations "
            "(host env LLM credential reads in src/rolemesh/egress/):",
        ]
        for fname, items in all_violations.items():
            for line_no, key_name in items:
                lines.append(
                    f"  {fname}:{line_no}  os.environ read of {key_name}"
                )
        lines.append(
            "\nFix: resolve the credential via "
            "rolemesh.egress.credentials.CredentialResolver "
            "(keyed on tenant_id from the request's Identity), "
            "or annotate with `# inv-cred-ok: <reason>` if this is "
            "genuinely a non-credential use of the same env name."
        )
        raise AssertionError("\n".join(lines))


def test_inv_cred_lint_catches_synthetic_violation(tmp_path: Path) -> None:
    """Mutation-style self-check: plant a bad read; lint must catch it.

    Without this, a regex typo (e.g. dropping the ``_API_KEY`` branch)
    could silently let every real violation pass on the live tree
    while the main test stays green.
    """
    bad = tmp_path / "leaky.py"
    bad.write_text(
        '"""Synthetic egress module — would leak the host env key."""\n'
        "import os\n"
        "\n"
        "def f() -> str:\n"
        '    return os.environ.get("ANTHROPIC_API_KEY", "")\n'
    )
    violations = _find_violations(bad)
    assert violations, "lint failed to detect ANTHROPIC_API_KEY read"
    assert violations[0][1] == "ANTHROPIC_API_KEY"


def test_inv_cred_ok_annotation_silences_lint(tmp_path: Path) -> None:
    """The ``# inv-cred-ok: <reason>`` opt-out comment must suppress
    the violation. Without this self-check, a refactor that broke the
    annotation parser would silently force every legitimate non-
    credential use of a matching name to require code changes."""
    ok = tmp_path / "annotated.py"
    ok.write_text(
        '"""Synthetic egress module with a justified read."""\n'
        "import os\n"
        "\n"
        "def f() -> str:\n"
        "    # inv-cred-ok: test fixture, never hit in production proxy\n"
        '    return os.environ.get("ANTHROPIC_API_KEY", "")\n'
    )
    violations = _find_violations(ok)
    assert not violations, (
        "inv-cred-ok annotation failed to silence the lint; "
        f"got violations: {violations}"
    )
