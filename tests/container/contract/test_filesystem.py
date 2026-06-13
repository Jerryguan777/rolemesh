"""T-FS: filesystem hardening + storage contract (docs/21 §3 "Hardening"
and §7 storage model, §8 tmpfs ownership row).

In-container probes report a JSON document on stderr (the contract
stream) so each assertion reads observed facts, not exit-code guesses.
"""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING

import pytest

from .conftest import make_tmpfs

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from rolemesh.container.runtime import VolumeMount as VolumeMountT

    from .conftest import Topology

from rolemesh.container.runtime import VolumeMount

pytestmark = pytest.mark.integration

_EROFS = 30  # errno.EROFS — write to a read-only filesystem


async def test_rootfs_is_readonly(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
) -> None:
    """T-FS-1: with the spec's hardening defaults, writing to / fails
    with EROFS — a compromised agent cannot persist into the image
    filesystem."""
    code = textwrap.dedent("""
        import json, sys
        try:
            open("/contract-probe", "w")
            result = {"wrote": True}
        except OSError as exc:
            result = {"wrote": False, "errno": exc.errno}
        sys.stderr.write(json.dumps(result))
    """)
    exit_code, stderr = await run_python("fs-erofs", code)
    assert exit_code == 0
    result = json.loads(stderr)
    assert result["wrote"] is False, "rootfs accepted a write — hardening lost"
    assert result["errno"] == _EROFS


async def test_tmpfs_mount_is_writable_and_owned_by_runtime_uid(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
    topology: Topology,
) -> None:
    """T-FS-2: a tmpfs in the production shape is writable and owned by
    the uid the container process runs as (docs/21 §8: K8s emptyDir has
    no uid option, so 'owner == runtime uid' is the cross-runtime
    contract the docker side must also satisfy)."""
    code = textwrap.dedent("""
        import json, os, sys
        path = "/scratch/probe"
        with open(path, "w") as fh:
            fh.write("x" * 1024)
        st = os.stat("/scratch")
        sys.stderr.write(json.dumps({
            "uid": os.getuid(),
            "mount_uid": st.st_uid,
            "mount_gid": st.st_gid,
            "written": os.path.getsize(path),
        }))
    """)
    exit_code, stderr = await run_python(
        "fs-tmpfs",
        code,
        tmpfs=make_tmpfs(
            "/scratch", size_mb=16, uid=topology.agent_uid, gid=topology.agent_gid
        ),
    )
    assert exit_code == 0
    result = json.loads(stderr)
    assert result["written"] == 1024
    assert result["mount_uid"] == result["uid"], (
        "tmpfs owner drifted from the runtime uid — every agent write "
        "to scratch space would EACCES (see runner._default_tmpfs)"
    )
    assert result["mount_gid"] == topology.agent_gid


def _mount(host: Path, container: str, *, readonly: bool) -> VolumeMountT:
    return VolumeMount(
        host_path=str(host), container_path=container, readonly=readonly
    )


async def test_readonly_mount_is_readable_but_rejects_writes(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
    host_mount_dir: Path,
) -> None:
    """T-FS-3: a readonly VolumeMount exposes host content and refuses
    writes with EROFS — 'ro' must be enforcement, not advice."""
    ro_dir = host_mount_dir / "ro"
    ro_dir.mkdir()
    (ro_dir / "seed.txt").write_text("host-seeded-content", encoding="utf-8")

    code = textwrap.dedent("""
        import json, sys
        seen = open("/mnt/ro/seed.txt", encoding="utf-8").read()
        try:
            open("/mnt/ro/intruder", "w")
            write = {"wrote": True}
        except OSError as exc:
            write = {"wrote": False, "errno": exc.errno}
        sys.stderr.write(json.dumps({"seen": seen, **write}))
    """)
    exit_code, stderr = await run_python(
        "fs-ro",
        code,
        mounts=[_mount(ro_dir, "/mnt/ro", readonly=True)],
    )
    assert exit_code == 0
    result = json.loads(stderr)
    assert result["seen"] == "host-seeded-content"
    assert result["wrote"] is False
    assert result["errno"] == _EROFS
    assert not (ro_dir / "intruder").exists()


async def test_readwrite_mount_content_is_visible_on_host(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
    host_mount_dir: Path,
) -> None:
    """T-FS-4: content written through a rw VolumeMount lands in the
    host directory — the shared-storage contract (docs/21 §7) the
    orchestrator relies on to read agent session artifacts."""
    rw_dir = host_mount_dir / "rw"
    rw_dir.mkdir()
    rw_dir.chmod(0o777)  # container runs as non-root uid

    code = textwrap.dedent("""
        import json, sys
        with open("/mnt/rw/artifact.txt", "w", encoding="utf-8") as fh:
            fh.write("written-inside-container")
        sys.stderr.write(json.dumps({"ok": True}))
    """)
    exit_code, stderr = await run_python(
        "fs-rw",
        code,
        mounts=[_mount(rw_dir, "/mnt/rw", readonly=False)],
    )
    assert exit_code == 0
    assert json.loads(stderr)["ok"] is True
    assert (rw_dir / "artifact.txt").read_text(encoding="utf-8") == (
        "written-inside-container"
    )
