"""T-LC: sandbox lifecycle — run / exit code / stop / replace / orphan
cleanup (docs/21 §3 "Sandbox lifecycle" + "Orphan cleanup" rows).

The per-job creation/destruction of agent sandboxes is the ONE
imperative container operation application code keeps (§1.4); these
cases pin down its observable semantics.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid
from typing import TYPE_CHECKING

import pytest

from .conftest import CONTRACT_PREFIX

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rolemesh.container.runtime import (
        ContainerHandle,
        ContainerRuntime,
        ContainerSpec,
    )

    from .conftest import Topology

pytestmark = pytest.mark.integration

_SLEEP_FOREVER = "import time; time.sleep(300)"


async def test_wait_propagates_zero_exit_code(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
) -> None:
    """T-LC-1: a process that exits 0 yields wait() == 0."""
    exit_code, _ = await run_python("lc-exit0", "raise SystemExit(0)")
    assert exit_code == 0


async def test_wait_propagates_nonzero_exit_code(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
) -> None:
    """T-LC-2: a nonzero exit code travels through wait() unchanged —
    the scheduler distinguishes success from failure by this value."""
    exit_code, _ = await run_python("lc-exit7", "raise SystemExit(7)")
    assert exit_code == 7


async def test_stop_terminates_long_running_container(
    runtime: ContainerRuntime,
    make_spec: Callable[..., ContainerSpec],
    spawn: Callable[[ContainerSpec], Awaitable[ContainerHandle]],
) -> None:
    """T-LC-3: runtime.stop(name) ends a container that would otherwise
    run for minutes, promptly, and frees its name for reuse."""
    spec = make_spec("lc-stop", python=_SLEEP_FOREVER)
    await spawn(spec)

    started = time.monotonic()
    await runtime.stop(spec.name, timeout=1)
    elapsed = time.monotonic() - started
    assert elapsed < 15, f"stop took {elapsed:.1f}s for a sleeping container"

    # The name is observably free again: a fresh run under the SAME
    # name starts and completes. (Runtime-agnostic liveness probe — a
    # still-running first container would hold the name.)
    respec = dataclasses.replace(
        make_spec("lc-stop-recycle", python="raise SystemExit(0)"), name=spec.name
    )
    handle = await spawn(respec)
    assert await asyncio.wait_for(handle.wait(), timeout=30) == 0


async def test_run_replaces_existing_container_with_same_name(
    make_spec: Callable[..., ContainerSpec],
    spawn: Callable[[ContainerSpec], Awaitable[ContainerHandle]],
) -> None:
    """T-LC-4: spawning under an existing name replaces the old
    container (create_or_replace semantics; the K8s side maps name
    conflicts to delete-then-create, docs/21 §8)."""
    first = make_spec("lc-replace", python=_SLEEP_FOREVER)
    await spawn(first)

    second = dataclasses.replace(
        first, entrypoint=["python", "-c", "raise SystemExit(5)"]
    )
    handle = await spawn(second)

    # The replacement runs to completion under the contested name; the
    # sleeping predecessor no longer owns it.
    assert await asyncio.wait_for(handle.wait(), timeout=30) == 5


async def test_cleanup_orphans_reaps_only_prefix_and_image_matches(
    runtime: ContainerRuntime,
    topology: Topology,
    make_spec: Callable[..., ContainerSpec],
    spawn: Callable[[ContainerSpec], Awaitable[ContainerHandle]],
) -> None:
    """T-LC-5: cleanup_orphans removes exactly the containers that match
    BOTH the name prefix and the image allowlist (INV-3 cleanup-safety).

    Three containers probe the two filters independently:
      * victim  — prefix match + allowlisted image  → reaped
      * foreign — prefix match + NON-allowlisted image → must survive
        (a user's unrelated container whose name overlaps ours)
      * decoy   — allowlisted image, name CONTAINS the prefix but does
        not START with it → must survive (catches substring-vs-prefix
        regressions in the name filter)
    """
    token = uuid.uuid4().hex[:8]
    prefix = f"{CONTRACT_PREFIX}orphan-{token}-"
    agent_image = topology.agent_image
    foreign_image = topology.foreign_image

    victim = dataclasses.replace(
        make_spec("x", python=_SLEEP_FOREVER), name=f"{prefix}victim"
    )
    foreign = dataclasses.replace(
        make_spec("x", python=_SLEEP_FOREVER, image=foreign_image),
        name=f"{prefix}foreign",
    )
    decoy = dataclasses.replace(
        make_spec("x", python=_SLEEP_FOREVER),
        name=f"{CONTRACT_PREFIX}decoy-{token}-{prefix}tail",
    )
    for spec in (victim, foreign, decoy):
        await spawn(spec)

    removed = await runtime.cleanup_orphans(
        prefix, allowed_images=frozenset({agent_image})
    )
    assert removed == [victim.name]

    # Survival proof, runtime-agnostic: a second sweep that DOES
    # allowlist the other image/prefix still finds each survivor alive
    # — had the first sweep touched them, there would be nothing left
    # to reap. (This also cleans them up.)
    assert await runtime.cleanup_orphans(
        prefix, allowed_images=frozenset({foreign_image})
    ) == [foreign.name]
    assert await runtime.cleanup_orphans(
        f"{CONTRACT_PREFIX}decoy-{token}-", allowed_images=frozenset({agent_image})
    ) == [decoy.name]
