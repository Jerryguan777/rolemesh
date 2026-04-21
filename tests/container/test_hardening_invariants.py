"""Hardening invariants — hard floors that must hold for every valid
build_container_spec() / _spec_to_config() output, across all legal
input combinations.

This file exists because the per-field single-case tests are easy to
satisfy by mirroring the implementation (the "mirror test" anti-pattern).
Invariant tests answer a different question: *no matter what inputs the
caller supplies, can the final HostConfig / ContainerSpec violate a
hardening guarantee?* They catch the "someone added a new field and
accidentally weakened the baseline" class of regression that single-case
tests cannot.

Coverage strategy: explicit cartesian sweeps (pytest.parametrize) over
the input dimensions that actually reach the ContainerSpec — this mimics
property-based testing without adding hypothesis as a dependency. Each
invariant is asserted over *every* generated spec.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from rolemesh.agent.executor import AgentBackendConfig
from rolemesh.container.docker_runtime import DockerRuntime
from rolemesh.container.runner import build_container_spec
from rolemesh.container.runtime import VolumeMount
from rolemesh.core.config import CONTAINER_ENV_ALLOWLIST
from rolemesh.core.types import ContainerConfig, Coworker


def _coworker(
    *,
    runtime: str | None = None,
    memory_limit: str | None = None,
    cpu_limit: float | None = None,
) -> Coworker:
    cfg = ContainerConfig(runtime=runtime, memory_limit=memory_limit, cpu_limit=cpu_limit)
    return Coworker(
        id="cw", tenant_id="t", name="Test", folder="f",
        container_config=cfg,
    )


# Input matrix: every reasonable combination of the knobs that actually
# flow into build_container_spec. Explicitly includes the adversarial
# corners — empty mounts, over-cap memory, unknown extra_env keys, mixed
# permissions, both auth modes.
_BACKEND_CONFIGS: list[AgentBackendConfig | None] = [
    None,
    AgentBackendConfig(name="claude", image="img", extra_env={"AGENT_BACKEND": "claude"}),
    AgentBackendConfig(name="pi", image="img", extra_env={"AGENT_BACKEND": "pi", "PI_MODEL_ID": "x"}),
    AgentBackendConfig(
        name="rogue", image="img",
        # A new backend that forgot to register its extra_env keys with
        # CONTAINER_ENV_ALLOWLIST — the allowlist must still hold.
        extra_env={"AGENT_BACKEND": "rogue", "SECRET_LEAK": "dont-forward", "DEBUG": "1"},
    ),
]

_COWORKERS: list[Coworker | None] = [
    None,
    _coworker(),
    _coworker(runtime="runc"),
    _coworker(runtime="runsc"),
    _coworker(memory_limit="512m", cpu_limit=0.5),
    # Over-cap — the clamp must still leave the final spec inside the ceiling.
    _coworker(memory_limit="64g", cpu_limit=32.0),
    # Exact boundary values.
    _coworker(memory_limit="8g", cpu_limit=4.0),
]

_MOUNT_SETS: list[list[VolumeMount]] = [
    [],
    [VolumeMount(host_path="/tmp/x", container_path="/workspace/x", readonly=False)],
    [
        VolumeMount(host_path="/tmp/a", container_path="/workspace/a", readonly=True),
        VolumeMount(host_path="/tmp/b", container_path="/workspace/b", readonly=False),
    ],
]

_AUTH_MODES = ["api-key", "oauth"]

# Host UID/GID scenarios the orchestrator is actually run under. This
# dimension is the one that exposed the UID-tmpfs drift bug on 2026-04-21:
# the previous static fixture always used os.getuid() from the test
# runner (usually 1000), which meant the "host UID ≠ 1000" downgrade
# branch in build_container_spec was never exercised.
#
#   (1000, 1000) — canonical Linux dev user, matches image AGENT_UID
#   (0, 0)       — orchestrator running as root; must fall back to
#                  AGENT_UID inside the container, not propagate root
#   (501, 20)    — macOS default first user + staff group
#   (502, 20)    — macOS second user
#   (42, 42)     — arbitrary non-special UID; catches any "magic number"
#                  assumption hiding in the resolver
#   (None, None) — platform has no os.getuid (Windows); resolver must
#                  still produce a coherent spec
_HOST_UIDS: list[tuple[int | None, int | None]] = [
    (1000, 1000),
    (0, 0),
    (501, 20),
    (502, 20),
    (42, 42),
    (None, None),
]


# Flatten the matrix up front so pytest reports one failure per combo.
_CASES: list[tuple[Any, ...]] = [
    (bc, cw, ms, am, hu)
    for bc in _BACKEND_CONFIGS
    for cw in _COWORKERS
    for ms in _MOUNT_SETS
    for am in _AUTH_MODES
    for hu in _HOST_UIDS
]


def _id(case: tuple[Any, ...]) -> str:
    bc, cw, ms, am, hu = case
    bc_tag = bc.name if bc else "nobc"
    cw_tag = (
        f"cw-{cw.container_config.runtime or 'def'}"
        f"-m{cw.container_config.memory_limit or 'def'}"
        if cw and cw.container_config else "nocw"
    )
    hu_tag = f"h{hu[0]}" if hu[0] is not None else "hNone"
    return f"{bc_tag}.{cw_tag}.m{len(ms)}.{am}.{hu_tag}"


@pytest.fixture(params=_CASES, ids=[_id(c) for c in _CASES])
def spec(request: pytest.FixtureRequest) -> Any:
    """Build one ContainerSpec per input case and expose it for invariants."""
    bc, cw, ms, am, (host_uid, host_gid) = request.param

    with patch("rolemesh.container.runner.detect_auth_mode", return_value=am):
        if host_uid is None:
            # Simulate a host without os.getuid (Windows). Temporarily
            # remove the attribute so `hasattr(os, "getuid")` returns False.
            import os as _os
            saved = _os.getuid
            try:
                del _os.getuid  # type: ignore[attr-defined]
                return build_container_spec(ms, "c", "j", backend_config=bc, coworker=cw)
            finally:
                _os.getuid = saved  # type: ignore[attr-defined]
        with (
            patch("rolemesh.container.runner.os.getuid", return_value=host_uid),
            patch("rolemesh.container.runner.os.getgid", return_value=host_gid),
        ):
            return build_container_spec(ms, "c", "j", backend_config=bc, coworker=cw)


@pytest.fixture
def host_config(spec) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return DockerRuntime._spec_to_config(spec)["HostConfig"]


# ---------------------------------------------------------------------------
# Invariants at the ContainerSpec layer (what we hand to DockerRuntime)
# ---------------------------------------------------------------------------


def test_inv_readonly_rootfs_always_true(spec) -> None:  # type: ignore[no-untyped-def]
    """Hard floor: every spec has a readonly root. A caller that needs
    writable rootfs must go through a dedicated helper, not through
    build_container_spec()."""
    assert spec.readonly_rootfs is True


def test_inv_cap_drop_always_includes_all(spec) -> None:  # type: ignore[no-untyped-def]
    assert "ALL" in spec.cap_drop


def test_inv_cap_add_never_grants_dangerous_caps(spec) -> None:  # type: ignore[no-untyped-def]
    """If cap_add ever leaks SYS_ADMIN / NET_ADMIN / SYS_PTRACE the
    drop-ALL above is undone. Guard against that independently of what
    cap_drop looks like."""
    dangerous = {"SYS_ADMIN", "NET_ADMIN", "SYS_PTRACE", "SYS_MODULE", "SYS_RAWIO"}
    assert not (dangerous & set(spec.cap_add))


def test_inv_security_opt_has_no_new_privileges(spec) -> None:  # type: ignore[no-untyped-def]
    assert "no-new-privileges:true" in spec.security_opt


def test_inv_security_opt_never_disables_seccomp(spec) -> None:  # type: ignore[no-untyped-def]
    """Absence of seccomp= means Docker applies its default profile. The
    one value we must never emit is seccomp=unconfined."""
    for opt in spec.security_opt:
        assert "seccomp=unconfined" not in opt


def test_inv_runtime_is_one_of_the_allowed_set(spec) -> None:  # type: ignore[no-untyped-def]
    """OCI runtime must be either the supported pair or None (= Docker default)."""
    assert spec.runtime in {None, "runc", "runsc"}


def test_inv_pids_limit_positive(spec) -> None:  # type: ignore[no-untyped-def]
    """PidsLimit must be a positive integer — 0 or None at this layer would
    silently remove the fork-bomb ceiling."""
    assert spec.pids_limit is not None
    assert spec.pids_limit > 0


def test_inv_memory_limit_within_global_ceiling(spec) -> None:  # type: ignore[no-untyped-def]
    """After clamp, the final spec's memory_limit must not exceed
    CONTAINER_MAX_MEMORY (default 8g). Over-cap coworker configs are
    guaranteed to be clamped down."""
    from rolemesh.container.docker_runtime import _parse_memory
    from rolemesh.core.config import CONTAINER_MAX_MEMORY
    assert _parse_memory(spec.memory_limit) <= _parse_memory(CONTAINER_MAX_MEMORY)


def test_inv_cpu_limit_within_global_ceiling(spec) -> None:  # type: ignore[no-untyped-def]
    from rolemesh.core.config import CONTAINER_MAX_CPU
    assert spec.cpu_limit is not None
    assert spec.cpu_limit <= CONTAINER_MAX_CPU


def test_inv_env_keys_subset_of_allowlist(spec) -> None:  # type: ignore[no-untyped-def]
    """This is the primary defense against backends accidentally leaking
    orchestrator-side env into containers. If a new key joins the
    allowlist the test still passes; if a backend slips a key PAST the
    allowlist (by bypassing _filter_env_allowlist) the test fails."""
    assert set(spec.env.keys()) <= CONTAINER_ENV_ALLOWLIST


def test_inv_tmpfs_uid_gid_matches_user_field(spec) -> None:  # type: ignore[no-untyped-def]
    """Every tmpfs entry that pins uid= / gid= MUST match the container's
    User field. Otherwise Linux owns the tmpfs as <tmpfs_uid>:<tmpfs_gid>
    at mount time, the process runs as <user_uid>:<user_gid>, and every
    write hits EACCES. macOS 2026-04-21 regression: the old code had
    User=502:20 from os.getuid() but tmpfs hardcoded uid=1000, making
    Pi's first mkdir(~/.pi) fail silently.

    Not every tmpfs entry has uid/gid (e.g. /tmp is world-writable via
    mode=1777) — those lines are skipped."""
    import re as _re
    if spec.user is not None:
        expected_uid, expected_gid = (int(x) for x in spec.user.split(":"))
    else:
        # Windows / no-getuid path: tmpfs should use AGENT_UID/GID.
        from rolemesh.container.runner import AGENT_GID, AGENT_UID
        expected_uid, expected_gid = AGENT_UID, AGENT_GID

    for path, options in spec.tmpfs.items():
        uid_match = _re.search(r"\buid=(\d+)", options)
        gid_match = _re.search(r"\bgid=(\d+)", options)
        if uid_match is None and gid_match is None:
            continue  # world-writable tmpfs (e.g. /tmp with mode=1777)
        assert uid_match is not None, f"tmpfs {path} has gid but no uid: {options}"
        assert gid_match is not None, f"tmpfs {path} has uid but no gid: {options}"
        assert int(uid_match.group(1)) == expected_uid, (
            f"tmpfs {path} uid={uid_match.group(1)} but container runs as uid={expected_uid}"
        )
        assert int(gid_match.group(1)) == expected_gid, (
            f"tmpfs {path} gid={gid_match.group(1)} but container runs as gid={expected_gid}"
        )


def test_inv_container_never_runs_as_root(spec) -> None:  # type: ignore[no-untyped-def]
    """If the orchestrator itself runs as root (host_uid=0), the agent
    container must NOT inherit that — CapDrop/readonly-rootfs hardening
    assumes a non-root UID inside the container. The resolver policy is
    to fall back to AGENT_UID (image-baked non-root agent) in this case.

    Also covers the platform-has-no-getuid case where we can't look up
    a runtime UID at all; the spec.user stays None, and Docker uses the
    image's USER agent (non-root)."""
    if spec.user is None:
        # Image USER directive (agent @ uid 1000) takes over. Nothing to assert
        # on spec level; the baseline invariant (non-root) is enforced by the
        # Dockerfile and verified by integration tests.
        return
    uid, gid = (int(x) for x in spec.user.split(":"))
    assert uid != 0, "container must not run as uid 0 (root)"
    # gid 0 is root group; ok to allow in the downgrade-root-to-AGENT_UID
    # fallback (AGENT_GID is 1000 so this never hits in practice), but
    # still: block 0:0 specifically so a future regression can't sneak
    # a full root:root pair through.
    assert (uid, gid) != (0, 0)


def test_inv_home_env_set_when_user_override_present(spec) -> None:  # type: ignore[no-untyped-def]
    """Whenever we hand Docker a `User=...` value, we also MUST set
    env[HOME]=/home/agent. A runtime UID that isn't in /etc/passwd has
    no pwd entry to fall back on, so any SDK calling
    os.path.expanduser('~') would otherwise break."""
    if spec.user is not None:
        assert spec.env.get("HOME") == "/home/agent"


def test_inv_metadata_blackhole_always_present(spec) -> None:  # type: ignore[no-untyped-def]
    """Both IMDS hostnames must resolve to loopback in every agent
    container, irrespective of backend / coworker config. SSRF via
    metadata is cheap to exploit and trivial to block."""
    assert spec.extra_hosts.get("169.254.169.254") == "127.0.0.1"
    assert spec.extra_hosts.get("metadata.google.internal") == "127.0.0.1"


def test_inv_no_docker_sock_in_any_mount(spec) -> None:  # type: ignore[no-untyped-def]
    """Binding docker.sock would hand root to the agent. Double-checked
    at both layers; here we verify no mount made it through."""
    from rolemesh.container.docker_runtime import _is_docker_socket_path
    for m in spec.mounts:
        assert not _is_docker_socket_path(m.host_path)
        assert not _is_docker_socket_path(m.container_path)


def test_inv_bind_mounts_do_not_expose_host_root_filesystems(spec) -> None:  # type: ignore[no-untyped-def]
    """Mounting the host's /, /etc, /proc, /sys, or /boot into an agent
    container defeats isolation before any of the other controls kick in.
    The bind-mount allowlist is expected to catch this, but pin it here
    so a bypass of mount_security.py also fails this invariant."""
    forbidden = {"/", "/etc", "/proc", "/sys", "/boot", "/root"}
    for m in spec.mounts:
        assert m.host_path.rstrip("/") not in forbidden, f"bind mount exposes {m.host_path}"


# ---------------------------------------------------------------------------
# Invariants at the HostConfig layer (what Docker Engine actually sees)
# ---------------------------------------------------------------------------


def test_inv_hc_never_privileged(host_config: dict[str, Any]) -> None:
    """Privileged=True undoes essentially every other control. Must never
    appear in the serialized config, even accidentally."""
    assert host_config.get("Privileged", False) is False


def test_inv_hc_readonly_rootfs_is_true(host_config: dict[str, Any]) -> None:
    assert host_config["ReadonlyRootfs"] is True


def test_inv_hc_cap_drop_contains_all(host_config: dict[str, Any]) -> None:
    assert "ALL" in host_config["CapDrop"]


def test_inv_hc_memory_swap_disabled_when_memory_set(host_config: dict[str, Any]) -> None:
    """Setting Memory without disabling MemorySwap lets cgroups default to
    unlimited swap, defeating the memory cap. Pin the "MemorySwap == Memory"
    contract at the HostConfig layer."""
    if "Memory" in host_config:
        assert host_config.get("MemorySwap") == host_config["Memory"]


def test_inv_hc_no_seccomp_unconfined(host_config: dict[str, Any]) -> None:
    for opt in host_config.get("SecurityOpt", []):
        assert "seccomp=unconfined" not in opt


def test_inv_hc_extra_hosts_has_metadata_blackhole(host_config: dict[str, Any]) -> None:
    entries = host_config.get("ExtraHosts", [])
    assert "metadata.google.internal:127.0.0.1" in entries
    assert "169.254.169.254:127.0.0.1" in entries
