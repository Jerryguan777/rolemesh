"""T-IO: diagnostic stream contract (docs/21 §3 "Diagnostic log stream"
and §8: protocol output rides NATS; stderr is diagnostics-only, and
cases assert "the diagnostic line appears").

The DockerRuntime delivers the stream without truncation; the byte-
exact volume case pins that down so a future backend (K8s pod logs)
cannot silently introduce a cap below what diagnostics need. Volumes
stay under typical kubelet rotation thresholds (10MB) on purpose — the
contract is "multi-megabyte diagnostics survive", not "infinite logs".
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from .conftest import collect_stderr

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rolemesh.container.runtime import ContainerHandle, ContainerSpec

pytestmark = pytest.mark.integration


async def test_diagnostic_stderr_line_is_visible_through_handle(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
) -> None:
    """T-IO-1: a diagnostic line the agent process writes to stderr is
    observable through handle.read_stderr after exit."""
    marker = "CONTRACT-DIAG 7f3a9c"
    code = f"import sys; sys.stderr.write({marker!r} + '\\n')"
    exit_code, stderr = await run_python("io-diag", code)
    assert exit_code == 0
    assert marker in stderr


async def test_crash_traceback_is_visible_through_handle(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
) -> None:
    """T-IO-2: when the agent process dies, its traceback reaches the
    operator via the same stream — the failure-diagnosis path the
    scheduler logs depend on."""
    exit_code, stderr = await run_python(
        "io-crash", "raise RuntimeError('CONTRACT-BOOM')"
    )
    assert exit_code != 0
    assert "CONTRACT-BOOM" in stderr
    assert "Traceback" in stderr


async def test_large_output_is_delivered_completely(
    make_spec: Callable[..., ContainerSpec],
    spawn: Callable[[ContainerSpec], Awaitable[ContainerHandle]],
) -> None:
    """T-IO-3: ~2MB of stderr — including a single 1MB line, the shape
    that breaks naive line-buffered readers — arrives byte-complete and
    without stalling the reader."""
    line_payload = 1_000_000  # one unbroken line
    n_lines = 1_000
    line_len = 1_000  # n_lines * (line_len+1) ≈ 1MB of newline-framed lines
    code = (
        "import sys; "
        f"sys.stderr.write('A' * {line_payload}); "
        f"sys.stderr.write('\\n'); "
        f"[sys.stderr.write('B' * {line_len} + '\\n') for _ in range({n_lines})]; "
        "sys.stderr.write('END-OF-VOLUME\\n')"
    )
    expected = line_payload + 1 + n_lines * (line_len + 1) + len("END-OF-VOLUME\n")

    spec = make_spec("io-volume", python=code)
    handle = await spawn(spec)
    assert await asyncio.wait_for(handle.wait(), timeout=60) == 0
    data = await collect_stderr(handle, timeout=60)
    assert b"END-OF-VOLUME" in data, "tail of the stream was lost"
    assert len(data) == expected, (
        f"stream truncated or padded: got {len(data)} bytes, "
        f"expected {expected}"
    )
