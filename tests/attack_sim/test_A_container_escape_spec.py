"""A. Container escape / sandbox breakout — spec-level assertions.

Spec-level means: we check that the Docker HostConfig the orchestrator
would send to dockerd **disallows the attack by construction**. These
tests do not start containers — they verify the declarative contract.

Pair each test with the manual counterpart in
``scripts/verify-hardening.sh`` to confirm the assertion holds when a
real container is running.

Attacks modeled:

  A1. Fork bomb                 → PidsLimit == 512 (cap set)
  A2. ptrace another process    → seccomp default + no-new-privileges
  A3. Mount /proc/kcore         → CapDrop ALL (no CAP_SYS_ADMIN)
  A4. Write /etc/shadow         → ReadonlyRootfs == True
  A5. Metadata credential theft → ExtraHosts blackhole injected
  A6. docker.sock mount inject  → basename-match guard rejects
  A7. Privileged=True request   → HostConfig never emits Privileged
  A8. Swap-based OOM amplif.    → MemorySwap == Memory (swap disabled)
"""

from __future__ import annotations

from typing import Any

import pytest

from rolemesh.agent.executor import AgentBackendConfig
from rolemesh.container.docker_runtime import (
    DockerRuntime,
    _is_docker_socket_path,
)
from rolemesh.container.runner import build_container_spec
from rolemesh.container.runtime import VolumeMount
from rolemesh.core.types import ContainerConfig, Coworker


# ---------------------------------------------------------------------------
# Test utility: build a final (ContainerSpec, HostConfig) pair reflecting
# what would be sent to dockerd.
# ---------------------------------------------------------------------------


def _build_hostconfig(
    *,
    mounts: list[VolumeMount] | None = None,
    coworker: Coworker | None = None,
    backend: AgentBackendConfig | None = None,
) -> tuple[Any, dict[str, Any]]:
    spec = build_container_spec(
        mounts or [],
        container_name="attack-sim",
        job_id="job-1",
        backend_config=backend
        or AgentBackendConfig(name="claude", image="img", extra_env={}),
        coworker=coworker,
    )
    config = DockerRuntime._spec_to_config(spec)
    return spec, config["HostConfig"]


# ---------------------------------------------------------------------------
# A1. Fork bomb
# ---------------------------------------------------------------------------


def test_A1_fork_bomb_capped_by_pids_limit() -> None:
    """Attacker: runs ``:(){ :|:& };:`` inside the agent to exhaust
    host PID space. Defense: PidsLimit set on HostConfig clamps the
    max number of processes a container can spawn."""
    _, hc = _build_hostconfig()
    assert hc["PidsLimit"] == 512, (
        f"PidsLimit must be a finite cap (512) to block fork bombs; "
        f"got {hc.get('PidsLimit')!r}"
    )


# ---------------------------------------------------------------------------
# A2. ptrace another process
# ---------------------------------------------------------------------------


def test_A2_ptrace_requires_cap_blocked_by_cap_drop() -> None:
    """Attacker: uses ptrace to attach to another process in the same
    container (e.g. a sibling cron, or the agent runner itself) and
    read its environment.
    Defense: CapDrop ALL removes CAP_SYS_PTRACE; seccomp default
    blocks the syscall; no-new-privileges prevents escalation via
    setuid."""
    _, hc = _build_hostconfig()
    assert "ALL" in hc["CapDrop"], (
        "CapDrop must include ALL so CAP_SYS_PTRACE cannot be used"
    )
    assert hc.get("CapAdd") in (None, [], ["NET_BIND_SERVICE"]), (
        f"CapAdd must not grant dangerous caps; got {hc.get('CapAdd')!r}"
    )
    security_opt = hc.get("SecurityOpt") or []
    assert any("no-new-privileges" in s for s in security_opt), (
        "SecurityOpt must include no-new-privileges to stop setuid "
        f"escalation; got {security_opt!r}"
    )


# ---------------------------------------------------------------------------
# A3. Mount /proc/kcore
# ---------------------------------------------------------------------------


def test_A3_kcore_mount_requires_sys_admin_which_is_dropped() -> None:
    """Attacker: mount /proc/kcore to read kernel memory.
    Defense: any ``mount`` syscall requires CAP_SYS_ADMIN — dropped.
    AppArmor default profile also blocks kernel filesystem writes.
    This assertion pins that CapAdd never smuggles SYS_ADMIN back."""
    _, hc = _build_hostconfig()
    cap_add = hc.get("CapAdd") or []
    dangerous = {
        "SYS_ADMIN",
        "CAP_SYS_ADMIN",
        "SYS_MODULE",
        "CAP_SYS_MODULE",
        "SYS_RAWIO",
        "CAP_SYS_RAWIO",
        "SYS_PTRACE",
        "CAP_SYS_PTRACE",
    }
    intersect = {c.upper() for c in cap_add} & dangerous
    assert not intersect, f"CapAdd leaks dangerous caps: {intersect}"

    # AppArmor/seccomp are applied as SecurityOpt. Their defaults are
    # Docker's own profiles; the invariant is that we don't disable them.
    security_opt_lower = " ".join(hc.get("SecurityOpt") or []).lower()
    assert "seccomp=unconfined" not in security_opt_lower
    assert "apparmor=unconfined" not in security_opt_lower


# ---------------------------------------------------------------------------
# A4. Write /etc/shadow (or any rootfs path)
# ---------------------------------------------------------------------------


def test_A4_rootfs_is_readonly() -> None:
    """Attacker: write to /etc/passwd, drop an rc.local, or otherwise
    persist code in rootfs. Defense: ReadonlyRootfs flips the entire
    / filesystem to r/o. Tmpfs carve-outs provide writable scratch
    for legitimate paths only (/tmp, /home/agent/.cache, etc.)."""
    spec, hc = _build_hostconfig()
    assert hc["ReadonlyRootfs"] is True, (
        "ReadonlyRootfs must be true to block rootfs persistence attacks"
    )
    # Writable paths exist only via Tmpfs or Binds, not via root writability.
    tmpfs = hc.get("Tmpfs") or {}
    assert isinstance(tmpfs, dict)
    # Sanity: every tmpfs target should be a legitimate path. Failing
    # entries here would indicate a rogue write path snuck in.
    for target in tmpfs:
        assert target.startswith(("/tmp", "/home/agent", "/var/tmp")), (
            f"unexpected tmpfs writable target: {target!r}"
        )
    _ = spec


# ---------------------------------------------------------------------------
# A5. Metadata credential theft
# ---------------------------------------------------------------------------


def test_A5_metadata_endpoints_blackholed() -> None:
    """Attacker: curl http://169.254.169.254/... to read GCP/AWS/Azure
    instance metadata (including attached service-account tokens).
    Defense: ExtraHosts maps all known metadata hostnames + the v4
    link-local IP to 127.0.0.1 — requests hit the container's own
    loopback and fail."""
    spec, _ = _build_hostconfig()
    eh = spec.extra_hosts
    # Accept both "hostname:ip" and ExtraHosts list form.
    resolved = {}
    if isinstance(eh, dict):
        resolved = {host: ip for host, ip in eh.items()}
    elif isinstance(eh, list):
        for entry in eh:
            if ":" in entry:
                h, ip = entry.split(":", 1)
                resolved[h] = ip
    # Hardening policy pins these explicitly.
    must_blackhole = {
        "metadata.google.internal",
        "169.254.169.254",
    }
    for host in must_blackhole:
        assert host in resolved, (
            f"metadata endpoint {host!r} missing from ExtraHosts blackhole"
        )
        assert resolved[host] == "127.0.0.1", (
            f"{host!r} resolves to {resolved[host]!r}; must be 127.0.0.1"
        )


# ---------------------------------------------------------------------------
# A6. docker.sock mount injection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attacker_mount",
    [
        "/var/run/docker.sock",
        "/run/docker.sock",
        # Substring bypass attempt: if guard used substring match,
        # "/tmp/docker.sock.txt" would squeak through. Basename-match
        # closes this.
        "/tmp/not-really-docker.sock.attacker",
        # Symlink-like names
        "/opt/containerd/docker.sock",
    ],
)
def test_A6_docker_sock_mount_injection_detected(attacker_mount: str) -> None:
    """Attacker vector: convince the orchestrator (via misconfigured
    additional_mounts / extra_mounts / backend config) to bind
    docker.sock into the container. Result would be trivial container
    escape via the docker API.
    Defense: ``_is_docker_socket_path`` uses basename matching (not
    substring) and the spec builder rejects docker.sock everywhere."""
    # Basename-match guard classifies each candidate.
    is_sock = _is_docker_socket_path(attacker_mount)
    basename = attacker_mount.rsplit("/", 1)[-1]
    # Verify: anything whose basename is exactly "docker.sock" is
    # flagged; the almost-but-not-quite cases above are NOT flagged
    # (they're legitimate user files that happen to contain the string).
    expected = basename == "docker.sock"
    assert is_sock == expected, (
        f"Guard disagrees for {attacker_mount!r}: is_sock={is_sock}, "
        f"basename={basename!r} expected={expected}"
    )


def test_A6_spec_to_config_rejects_docker_sock_mount() -> None:
    """Integration: a malicious ContainerSpec.mounts entry with
    docker.sock (from any upstream path — extra_mounts, additional_mounts,
    or a rogue runner function) must be refused at the final
    ``_spec_to_config`` gate. That gate is the last line of defense
    regardless of how the mount got injected."""
    from rolemesh.container.runtime import ContainerSpec

    # Craft a ContainerSpec directly with docker.sock — simulates a
    # bypass of upstream filters. The DockerRuntime._spec_to_config
    # must still reject it.
    spec = ContainerSpec(
        name="attack",
        image="img",
        mounts=[
            VolumeMount(
                host_path="/var/run/docker.sock",
                container_path="/var/run/docker.sock",
                readonly=False,
            )
        ],
    )
    with pytest.raises(Exception):  # noqa: BLE001 — any exception is a pass
        DockerRuntime._spec_to_config(spec)


# ---------------------------------------------------------------------------
# A7. Privileged container request
# ---------------------------------------------------------------------------


def test_A7_privileged_never_true() -> None:
    """Attacker vector: a compromised backend config or caller tries
    to set HostConfig.Privileged=True, which would undo every other
    hardening. Defense: the spec-to-config pipeline never writes
    Privileged, so it defaults to False."""
    _, hc = _build_hostconfig()
    assert hc.get("Privileged", False) is False, (
        "Privileged must never be True — it undoes every other control"
    )


# ---------------------------------------------------------------------------
# A8. Swap-based memory amplification
# ---------------------------------------------------------------------------


def test_A8_swap_disabled_equal_to_memory() -> None:
    """Attacker vector: a container with Memory=2g but no swap cap can
    use the host's swap space, multiplying its effective memory and
    slowing the whole host. Defense: MemorySwap == Memory disables
    container swap entirely."""
    cw = Coworker(
        id="cw",
        tenant_id="t",
        name="Test",
        folder="f",
        container_config=ContainerConfig(memory_limit="1g"),
    )
    _, hc = _build_hostconfig(coworker=cw)
    assert hc["Memory"] > 0
    assert hc["MemorySwap"] == hc["Memory"], (
        f"MemorySwap={hc['MemorySwap']} must equal Memory={hc['Memory']} "
        "to disable swap; otherwise attacker can use swap to amplify memory"
    )
    # Swappiness 0 for belt-and-braces (prefers OOM-kill over swap-out).
    swappiness = hc.get("MemorySwappiness")
    assert swappiness in (None, 0), f"MemorySwappiness must be 0, got {swappiness}"
