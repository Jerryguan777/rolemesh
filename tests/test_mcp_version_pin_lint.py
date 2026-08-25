"""Guards the ``mcp`` major-version cap against silent drift.

Background (2026-08-25): ``container/Dockerfile`` installed the agent
image's Python deps with a bare ``pip install ... mcp``. Every rebuild
therefore resolved to whatever was newest on PyPI at build time. mcp 2.0
shipped 2026-07-28 and is a breaking major — it swaps the ``httpx``
dependency for ``httpx2`` and renames ``FastMCP`` to ``MCPServer`` — so a
rebuild silently moved the agent image from 1.x to 2.x while
``uv.lock`` (orchestrator, webui, eval CLI, redteam, tests) stayed on
1.x.

The failure is quiet, which is what makes it worth a lint test:
``src/pi/mcp/client.py`` hands an ``httpx.AsyncClient`` to
``streamable_http_client(url, http_client=...)``; under 2.x the SDK wants
an ``httpx2`` client, the transport dies on the first POST, and
``session.initialize()`` raises ``MCPError: Connection closed``.
``pi.mcp.tool_bridge._connect_one`` catches that per server and logs
"MCP server '<name>' unavailable, skipping" — the agent boots fine and
just has no MCP tools.

Both declaration sites must therefore stay capped until
``src/pi/mcp/client.py``, ``tests/mock_mcp_server.py`` and
``redteam/mcp/*`` are actually ported to the 2.x API.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Matches an ``mcp`` requirement specifier wherever it is declared:
# quoted in pyproject/Dockerfile, or bare on a pip command line.
_MCP_SPEC = re.compile(r"""(?<![\w.-])mcp(?![\w.-])\s*((?:[<>=!~][^"',\s\\]*\s*,?\s*)*)""")


def _specs(text: str) -> list[str]:
    return [m.group(1).strip() for m in _MCP_SPEC.finditer(text)]


def _caps_below_2(spec: str) -> bool:
    """True when *spec* forbids mcp 2.x."""
    return bool(re.search(r"<\s*2(\.|,|$)", spec) or re.search(r"==\s*1\.", spec))


def test_pyproject_caps_mcp_below_2() -> None:
    text = (_REPO_ROOT / "pyproject.toml").read_text()
    specs = [s for s in _specs(text) if s]
    assert specs, "no ``mcp`` requirement found in pyproject.toml"
    for spec in specs:
        assert _caps_below_2(spec), (
            f"pyproject.toml declares ``mcp{spec}`` — mcp 2.x is a breaking "
            "major (httpx -> httpx2, FastMCP -> MCPServer) and the repo's "
            "MCP code is 1.x. Keep the <2 cap until it is ported."
        )


def test_agent_dockerfile_caps_mcp_below_2() -> None:
    """The agent image installs with bare pip and never reads uv.lock.

    This is the site that actually drifted: without a cap the image's mcp
    version is "whatever PyPI served on the day someone rebuilt".
    """
    text = (_REPO_ROOT / "container" / "Dockerfile").read_text()
    specs = _specs(text)
    installed = [s for s in specs if s or "pip install" in text]
    assert installed, "no ``mcp`` install found in container/Dockerfile"
    assert any(_caps_below_2(s) for s in specs), (
        "container/Dockerfile installs ``mcp`` without a <2 cap. An "
        "unpinned rebuild pulls mcp 2.x, whose transport rejects the "
        "httpx client pi/mcp/client.py builds — the agent then boots with "
        "zero MCP tools and only a per-server warning in the log."
    )


def test_dockerfile_mcp_spec_is_shell_quoted() -> None:
    """``mcp<2`` unquoted inside a RUN line is a shell redirect, not a pin."""
    for line in (_REPO_ROOT / "container" / "Dockerfile").read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "mcp" not in stripped:
            continue
        if "<" not in stripped:
            continue
        assert re.search(r"""['"][^'"]*mcp[^'"]*<[^'"]*['"]""", stripped), (
            f"unquoted ``<`` in a Dockerfile mcp requirement: {stripped!r} — "
            "the shell would treat it as an input redirection."
        )
