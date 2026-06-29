"""A (Kubernetes). Container escape / sandbox breakout — K8s spec level.

The Docker counterpart lives in ``test_A_container_escape_spec.py`` and
asserts the ``HostConfig`` sent to dockerd. With ``ROLEMESH_CONTAINER_RUNTIME=k8s``
(src/rolemesh/container/k8s_runtime.py) the SAME ``ContainerSpec`` is mapped
onto a Pod manifest instead — so the identical hardening contract must hold
on the Kubernetes plane, expressed as a pod ``securityContext`` + pod-spec
fields rather than docker HostConfig keys.

``spec_to_pod_manifest`` is a pure function with an import-free module top
(no ``[k8s]`` extra needed) — these run anywhere the Docker A-tests do.

Mapping of the A-series onto Kubernetes:

  A2/A3  ptrace / kcore (need caps)  → capabilities.drop == [ALL], no add
  A2     no-new-privileges           → allowPrivilegeEscalation: false
  A2     seccomp default             → seccompProfile: RuntimeDefault
  A4     rootfs write                → readOnlyRootFilesystem: true
  A5     cloud metadata theft        → hostAliases blackhole + NetworkPolicy
  A7     privileged container        → never privileged; runAsNonRoot
  A9(k8s) ServiceAccount token theft → automountServiceAccountToken: false
"""

from __future__ import annotations

from typing import Any

from rolemesh.agent.executor import AgentBackendConfig
from rolemesh.container.k8s_runtime import (
    AGENT_POD_LABELS,
    AGENT_ROLE_LABEL,
    AGENT_ROLE_VALUE,
    NETPOL_AGENT_ALLOW_EGRESS,
    NETPOL_AGENT_DEFAULT_DENY,
    REQUIRED_AGENT_NETWORK_POLICIES,
    spec_to_pod_manifest,
)
from rolemesh.container.runner import build_container_spec
from rolemesh.core.types import ContainerConfig, Coworker


def _pod(*, coworker: Coworker | None = None) -> dict[str, Any]:
    """Build the Pod manifest the K8s runtime would POST to the API server."""
    spec = build_container_spec(
        [],
        container_name="attack-sim-k8s",
        job_id="job-1",
        backend_config=AgentBackendConfig(name="claude", image="img", extra_env={}),
        coworker=coworker,
    )
    return spec_to_pod_manifest(
        spec, namespace="rolemesh", data_dir="/data", data_pvc="rolemesh-data"
    )


def _sec_ctx(pod: dict[str, Any]) -> dict[str, Any]:
    return pod["spec"]["containers"][0]["securityContext"]


# ---------------------------------------------------------------------------
# A2/A3. ptrace / kcore — capability drop
# ---------------------------------------------------------------------------


def test_A2_k8s_capabilities_drop_all() -> None:
    """Attacker: ptrace a sibling / mount /proc/kcore — both need Linux
    capabilities. Defense: securityContext.capabilities.drop == [ALL] and
    no dangerous cap is added back."""
    sc = _sec_ctx(_pod())
    caps = sc["capabilities"]
    assert "ALL" in caps["drop"], (
        f"capabilities.drop must contain ALL; got {caps['drop']!r}"
    )
    dangerous = {
        "SYS_ADMIN", "CAP_SYS_ADMIN", "SYS_MODULE", "CAP_SYS_MODULE",
        "SYS_PTRACE", "CAP_SYS_PTRACE", "SYS_RAWIO", "CAP_SYS_RAWIO",
    }
    added = {c.upper() for c in caps.get("add", [])}
    assert not (added & dangerous), f"capabilities.add leaks dangerous caps: {added & dangerous}"


def test_A2_k8s_no_privilege_escalation_and_seccomp() -> None:
    """no-new-privileges → allowPrivilegeEscalation: false; the default
    seccomp profile blocks ptrace/mount syscalls → seccompProfile is
    RuntimeDefault (NOT Unconfined, which would disable it)."""
    sc = _sec_ctx(_pod())
    assert sc["allowPrivilegeEscalation"] is False, (
        "allowPrivilegeEscalation must be false (no-new-privileges analog)"
    )
    assert sc["seccompProfile"]["type"] == "RuntimeDefault", (
        f"seccomp must stay RuntimeDefault; got {sc['seccompProfile']!r}"
    )


# ---------------------------------------------------------------------------
# A4. Rootfs write
# ---------------------------------------------------------------------------


def test_A4_k8s_rootfs_is_readonly() -> None:
    """Attacker: persist code by writing rootfs (/etc/*, rc.local). Defense:
    readOnlyRootFilesystem flips / to read-only; writable scratch comes only
    from emptyDir-backed tmpfs mounts."""
    assert _sec_ctx(_pod())["readOnlyRootFilesystem"] is True, (
        "readOnlyRootFilesystem must be true to block rootfs persistence"
    )


# ---------------------------------------------------------------------------
# A7. Privileged container + run-as-root
# ---------------------------------------------------------------------------


def test_A7_k8s_never_privileged_runs_nonroot() -> None:
    """Privileged=true would undo every other control; running as UID 0
    re-opens many escapes. Defense: the manifest never sets privileged and
    pins runAsNonRoot + a non-zero UID."""
    sc = _sec_ctx(_pod())
    assert sc.get("privileged", False) is False, "privileged must never be true"
    assert sc["runAsNonRoot"] is True, "runAsNonRoot must be true"
    # When a uid is pinned it must not be root.
    assert sc.get("runAsUser", 1) != 0, "runAsUser must not be 0 (root)"


# ---------------------------------------------------------------------------
# A9 (k8s-specific). ServiceAccount token theft
# ---------------------------------------------------------------------------


def test_A9_k8s_service_account_token_not_mounted() -> None:
    """K8s-only escape vector: a pod that automounts its ServiceAccount token
    hands an attacker a credential to the API server (and from there, lateral
    movement). Defense: automountServiceAccountToken: false — agent pods get
    no token at all."""
    pod_spec = _pod()["spec"]
    assert pod_spec["automountServiceAccountToken"] is False, (
        "automountServiceAccountToken must be false — an agent must not carry "
        "an API-server credential"
    )


# ---------------------------------------------------------------------------
# A5. Cloud metadata theft — hostAliases blackhole + NetworkPolicy coverage
# ---------------------------------------------------------------------------


def test_A5_k8s_metadata_endpoints_blackholed_in_host_aliases() -> None:
    """Defense-in-depth pin: the spec's metadata blackhole is mapped to
    hostAliases (the primary K8s defense is the default-deny NetworkPolicy,
    but the hostAliases mapping must not silently drop)."""
    pod_spec = _pod()["spec"]
    aliases = pod_spec.get("hostAliases") or []
    blackholed: dict[str, str] = {}
    for entry in aliases:
        for host in entry.get("hostnames", []):
            blackholed[host] = entry["ip"]
    for host in ("metadata.google.internal", "169.254.169.254"):
        assert blackholed.get(host) == "127.0.0.1", (
            f"metadata endpoint {host!r} must be blackholed to 127.0.0.1 in "
            f"hostAliases; got {blackholed.get(host)!r}"
        )


def test_A5_k8s_pod_carries_agent_labels_for_network_policy() -> None:
    """The default-deny + gateway-only-egress NetworkPolicies select agent
    pods by ``rolemesh.io/role=agent``. If the orchestrator stops stamping
    that label, agent pods silently fall OUT of every isolation policy — a
    full egress/metadata escape. Pin the label contract here."""
    labels = _pod()["metadata"]["labels"]
    assert labels.get(AGENT_ROLE_LABEL) == AGENT_ROLE_VALUE, (
        f"pod must carry {AGENT_ROLE_LABEL}={AGENT_ROLE_VALUE} so the agent "
        f"NetworkPolicies select it; got labels {labels!r}"
    )
    # The isolation policies the chart must declare for these pods.
    assert set(REQUIRED_AGENT_NETWORK_POLICIES) == {
        NETPOL_AGENT_DEFAULT_DENY,
        NETPOL_AGENT_ALLOW_EGRESS,
    }
    # Sanity: the stamped labels are exactly the policy selector contract.
    assert AGENT_POD_LABELS[AGENT_ROLE_LABEL] == AGENT_ROLE_VALUE


# ---------------------------------------------------------------------------
# A8. Swap amplification — node-level on K8s (documented non-mapping)
# ---------------------------------------------------------------------------


def test_A8_k8s_memory_limit_bounds_without_swap_field() -> None:
    """On Docker, A8 pins MemorySwap == Memory. On K8s swap is a node-level
    setting with no pod field (docs/21 §8), so memory is bounded by
    resources.limits.memory alone. Assert the limit is emitted so an agent
    cannot request unbounded memory; the no-swap guarantee is a node/kubelet
    invariant, not a manifest field."""
    cw = Coworker(
        id="cw",
        tenant_id="t",
        name="Test",
        folder="f",
        container_config=ContainerConfig(memory_limit="512m"),
    )
    container = _pod(coworker=cw)["spec"]["containers"][0]
    limits = container.get("resources", {}).get("limits", {})
    assert "memory" in limits, (
        "a memory limit must be emitted so the agent cannot exhaust node RAM"
    )
    # No pod-level swap knob exists; ensure we did not invent a bogus one.
    assert "memory-swap" not in limits and "memorySwap" not in limits
