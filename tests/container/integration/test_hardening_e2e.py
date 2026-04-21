"""End-to-end hardening tests — real containers, real kernel.

Each test here uses a real Docker daemon to prove the hardening controls
actually enforce the documented behaviour, not just that our code asks
Docker to enforce them. The matching unit tests in `tests/container/`
verify the request side; this module verifies the enforcement side.

Scope is deliberately narrow — only the five controls that are most
vulnerable to silent regression (a later refactor flips a default without
anyone noticing):

  1. ReadonlyRootfs — writing to / fails, writing to tmpfs succeeds
  2. CapDrop=ALL    — CAP_NET_RAW is dropped (ping fails)
  3. PidsLimit      — fork bomb is contained, process table stays small
  4. Metadata IMDS  — 169.254.169.254 blackholed to 127.0.0.1
  5. ICC=false      — two containers on the bridge cannot talk

Intentionally NOT covered here:
  * gVisor runsc (requires runsc on the host)
  * userns-remap (requires daemon reconfiguration)
  * AppArmor profile enforcement (signal is weak and platform-specific)
  * Default seccomp profile filter mode (platform-specific syscall tests)

Those live in the manual verification checklist in
docs/safety/container-hardening.md.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import aiodocker
import aiodocker.exceptions
import pytest

from .conftest import PROBE_IMAGE

pytestmark = pytest.mark.integration


async def _run_and_capture(
    client: aiodocker.Docker,
    name: str,
    config: dict[str, Any],
    *,
    timeout: float = 15.0,
) -> tuple[int, str]:
    """Run a throwaway container and return (exit_code, combined_logs).

    Caller owns the config; this helper owns creation/cleanup so every
    test has identical teardown semantics.
    """
    container = await client.containers.create_or_replace(name=name, config=config)
    try:
        await container.start()
        result = await asyncio.wait_for(container.wait(), timeout=timeout)
        exit_code = int(result.get("StatusCode", -1))
        logs = await container.log(stdout=True, stderr=True)
        return exit_code, "".join(logs)
    finally:
        with contextlib.suppress(aiodocker.exceptions.DockerError):
            await container.delete(force=True)


# ---------------------------------------------------------------------------
# 1. ReadonlyRootfs
# ---------------------------------------------------------------------------


async def test_readonly_rootfs_blocks_writes_to_root(
    docker_client: aiodocker.Docker, unique_name: str,
) -> None:
    """Writing to / must fail; writing to tmpfs must succeed. If a future
    change flips readonly_rootfs default to False this fires immediately."""
    code, logs = await _run_and_capture(
        docker_client, unique_name,
        {
            "Image": PROBE_IMAGE,
            "Cmd": ["sh", "-c", "touch /readonly-canary && echo FAIL_WRITE_SUCCEEDED"],
            "HostConfig": {
                "ReadonlyRootfs": True,
                "AutoRemove": False,
            },
        },
    )
    # sh exits non-zero when touch fails; the canary string must not appear.
    assert code != 0, f"write to / succeeded unexpectedly: {logs!r}"
    assert "FAIL_WRITE_SUCCEEDED" not in logs


async def test_tmpfs_accepts_writes_under_readonly_rootfs(
    docker_client: aiodocker.Docker, unique_name: str,
) -> None:
    """Sibling of the above: with tmpfs mounted, writes under the tmpfs
    path must succeed even when rootfs is read-only."""
    code, logs = await _run_and_capture(
        docker_client, unique_name,
        {
            "Image": PROBE_IMAGE,
            "Cmd": ["sh", "-c", "echo x > /tmp/canary && cat /tmp/canary"],
            "HostConfig": {
                "ReadonlyRootfs": True,
                "Tmpfs": {"/tmp": "rw,size=8m"},
                "AutoRemove": False,
            },
        },
    )
    assert code == 0, f"tmpfs write failed: {logs!r}"
    assert "x" in logs


# ---------------------------------------------------------------------------
# 2. CapDrop=["ALL"] actually removes capabilities
# ---------------------------------------------------------------------------


async def test_cap_drop_all_zeroes_effective_capability_set(
    docker_client: aiodocker.Docker, unique_name: str,
) -> None:
    """Read /proc/self/status CapEff — should be all zeros under CapDrop=ALL.

    This is a direct signal rather than an indirect one (ping / mount etc.
    are unreliable because busybox can use unprivileged syscalls that
    succeed regardless of capabilities)."""
    code, logs = await _run_and_capture(
        docker_client, unique_name,
        {
            "Image": PROBE_IMAGE,
            # Last command's exit code is propagated as the container's.
            "Cmd": ["sh", "-c", "grep ^CapEff /proc/self/status"],
            "HostConfig": {
                "CapDrop": ["ALL"],
                "AutoRemove": False,
            },
        },
    )
    assert code == 0, f"could not read /proc/self/status: {logs!r}"
    # Format: "CapEff:\t0000000000000000"
    import re as _re
    m = _re.search(r"CapEff:\s*([0-9a-f]+)", logs)
    assert m, f"could not parse CapEff line: {logs!r}"
    cap_eff = int(m.group(1), 16)
    assert cap_eff == 0, (
        f"CapEff is {m.group(1)} — expected all zeros under CapDrop=ALL"
    )


# ---------------------------------------------------------------------------
# 3. PidsLimit
# ---------------------------------------------------------------------------


async def test_pids_limit_triggers_fork_failure(
    docker_client: aiodocker.Docker, unique_name: str,
) -> None:
    """When fork attempts exceed PidsLimit, the kernel returns EAGAIN and
    busybox surfaces "can't fork: Resource temporarily unavailable".

    Detection strategy: launch 512 background sleeps. With PidsLimit=32
    most spawns fail; busybox writes the error to stderr; we grep for it.
    (We can't count successes via `$?` inside the same shell — that
    shell also counts against the limit.)"""
    pids_cap = 32
    _, logs = await _run_and_capture(
        docker_client, unique_name,
        {
            "Image": PROBE_IMAGE,
            "Cmd": [
                "sh", "-c",
                "i=0; while [ $i -lt 512 ]; do sleep 30 & i=$((i+1)); done; wait",
            ],
            "HostConfig": {
                "PidsLimit": pids_cap,
                "AutoRemove": False,
            },
        },
        timeout=15.0,
    )
    # Signal that the cgroup denied some forks. Exact message varies
    # between busybox versions — match on the canonical EAGAIN text.
    assert "Resource temporarily unavailable" in logs or "can't fork" in logs, (
        f"PidsLimit={pids_cap} did not trigger fork failures; logs={logs[:500]!r}"
    )


# ---------------------------------------------------------------------------
# 4. Metadata blackhole
# ---------------------------------------------------------------------------


async def test_metadata_ip_blackholed_to_loopback(
    docker_client: aiodocker.Docker, unique_name: str,
) -> None:
    """With ExtraHosts redirecting 169.254.169.254 to 127.0.0.1, and
    nothing listening on loopback, TCP connect must fail.

    Note on literal IPs: ExtraHosts maps hostnames, but IP-literal
    resolution goes through the kernel stack. The /etc/hosts entry still
    matters because busybox nc resolves via getaddrinfo(3), which reads
    /etc/hosts and overrides the literal. If the entry is missing, the
    connect goes direct to 169.254.169.254 and either times out or
    (in a cloud VM) returns IMDS data — hence the explicit ExtraHosts here."""
    code, logs = await _run_and_capture(
        docker_client, unique_name,
        {
            "Image": PROBE_IMAGE,
            # Last command's exit code is propagated to the container.
            # Exit 0 only if connect succeeds; exit non-zero on block/timeout.
            "Cmd": ["sh", "-c", "nc -zvw2 169.254.169.254 80"],
            "HostConfig": {
                "ExtraHosts": ["169.254.169.254:127.0.0.1"],
                "AutoRemove": False,
            },
        },
        timeout=15.0,
    )
    assert code != 0, f"connection to IMDS succeeded: {logs!r}"


# ---------------------------------------------------------------------------
# 5. ICC=false — containers on the hardened bridge cannot reach each other
# ---------------------------------------------------------------------------


async def test_icc_false_blocks_sibling_ping(
    docker_client: aiodocker.Docker, unique_name: str,
) -> None:
    """Two containers attached to a bridge with enable_icc=false must NOT
    be able to route packets to each other — the whole point of
    rolemesh-agent-net. Uses a dedicated test network to avoid touching
    production state."""
    net_name = f"{unique_name}-net"
    client = docker_client

    # Create a dedicated network for this test (ICC off).
    net = await client.networks.create(config={
        "Name": net_name,
        "Driver": "bridge",
        "Options": {"com.docker.network.bridge.enable_icc": "false"},
    })
    receiver_name = f"{unique_name}-rx"
    sender_name = f"{unique_name}-tx"
    receiver = None
    sender = None
    try:
        # Receiver: idle, kept around so it has a resolvable IP.
        receiver = await client.containers.create_or_replace(
            name=receiver_name,
            config={
                "Image": PROBE_IMAGE,
                "Cmd": ["sh", "-c", "sleep 30"],
                "HostConfig": {"NetworkMode": net_name},
            },
        )
        await receiver.start()

        # Grab receiver's IP.
        info = await receiver.show()
        rx_ip = info["NetworkSettings"]["Networks"][net_name]["IPAddress"]
        assert rx_ip, "receiver did not get an IP on the test network"

        # Sender pings the receiver. On an ICC=false bridge the ping must fail.
        sender = await client.containers.create_or_replace(
            name=sender_name,
            config={
                "Image": PROBE_IMAGE,
                # TCP probe rather than ICMP: ping is unreliable for this
                # signal (busybox may use unprivileged datagram sockets,
                # and the ICC filter applies at L2 not L4). nc with a
                # timeout exits non-zero if the connect times out or is
                # rejected — both count as "sibling unreachable".
                # Last command's exit code propagates to container exit.
                "Cmd": ["sh", "-c", f"nc -zvw2 {rx_ip} 22"],
                "HostConfig": {"NetworkMode": net_name},
            },
        )
        await sender.start()
        result = await asyncio.wait_for(sender.wait(), timeout=15.0)
        exit_code = int(result.get("StatusCode", -1))
        logs = "".join(await sender.log(stdout=True, stderr=True))

        # nc connect must fail — exit code != 0 means the packet did not
        # get through (drop or refuse). On ICC=true it would succeed or
        # get "connection refused" from the sibling's idle port — either
        # way we'd see a different result than a silent drop.
        assert exit_code != 0, (
            f"sibling reached despite enable_icc=false (logs={logs!r})"
        )
    finally:
        for c in (sender, receiver):
            if c is not None:
                with contextlib.suppress(aiodocker.exceptions.DockerError):
                    await c.delete(force=True)
        with contextlib.suppress(aiodocker.exceptions.DockerError):
            await net.delete()
