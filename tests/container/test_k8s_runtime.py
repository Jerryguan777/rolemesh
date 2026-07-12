"""Unit tests for rolemesh.container.k8s_runtime.

Testing philosophy (docs/21 §4.4, project test guidance):

  * Mocks live ONLY at the kubernetes_asyncio API-client boundary
    (CoreV1Api / NetworkingV1Api / AuthorizationV1Api). Everything inside
    the runtime — manifest mapping, the triple-filter cleanup, the
    verify invariants, the wait() watch+read loop — runs as real code.
  * The pure mapping functions (spec_to_pod_manifest and friends) are
    exercised against real ContainerSpec inputs and their produced dicts
    are asserted directly. We assert SEMANTICS (e.g. "drop ALL is
    present", "readOnly is preserved"), never "field X equals field Y"
    re-derivations of the implementation.
  * Each non-obvious test names the mutation it would catch.

The kubernetes_asyncio model objects returned by the real client are
built with the real client classes (V1Pod, V1NetworkPolicy, ...) so the
attribute paths the runtime walks (``status.container_statuses[0]
.state.terminated.exit_code`` etc.) are the genuine deserialization
shape — not a hand-rolled stand-in that might diverge.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("kubernetes_asyncio")

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException

from rolemesh.container.k8s_runtime import (
    AGENT_MANAGED_BY_LABEL,
    AGENT_MANAGED_BY_VALUE,
    AGENT_ROLE_LABEL,
    AGENT_ROLE_VALUE,
    K8sPodHandle,
    K8sRuntime,
    _terminal_exit_code,
    spec_to_pod_manifest,
)
from rolemesh.container.runtime import ContainerSpec, VolumeMount

# ===========================================================================
# Helpers for building manifests without repeating the keyword plumbing.
# ===========================================================================

_NS = "rolemesh"
_DATA_DIR = "/app/data"
_DATA_PVC = "rolemesh-data"


def _manifest(spec: ContainerSpec, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "namespace": _NS,
        "data_dir": _DATA_DIR,
        "data_pvc": _DATA_PVC,
    }
    kwargs.update(overrides)
    return spec_to_pod_manifest(spec, **kwargs)


def _container(manifest: dict[str, Any]) -> dict[str, Any]:
    containers: list[dict[str, Any]] = manifest["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def _sec_ctx(manifest: dict[str, Any]) -> dict[str, Any]:
    ctx: dict[str, Any] = _container(manifest)["securityContext"]
    return ctx


# ===========================================================================
# Hardening -> securityContext (docs/21 §3 mapping table).
# Mutation analysis: each assertion below is paired with the exact mutation
# of _security_context it would catch.
# ===========================================================================


def test_hardening_drops_all_capabilities() -> None:
    """Default cap_drop=["ALL"] must land as capabilities.drop=["ALL"].

    Mutation caught: dropping the ``capabilities.drop`` mapping, or
    emitting ``add`` instead of ``drop`` — both leave the agent with the
    default Linux capability set (CAP_NET_RAW etc.), a sandbox escape.
    """
    spec = ContainerSpec(name="a", image="img")  # cap_drop defaults to ["ALL"]
    caps = _sec_ctx(_manifest(spec))["capabilities"]
    assert caps["drop"] == ["ALL"]
    # No accidental broad add.
    assert caps.get("add", []) == []


def test_hardening_runs_as_nonroot_true_not_false() -> None:
    """runAsNonRoot must be literally True.

    Mutation caught: ``runAsNonRoot: False`` (or omitting it). On a PSA
    ``restricted`` namespace a False/missing value would let a root image
    through where admission should reject it.
    """
    ctx = _sec_ctx(_manifest(ContainerSpec(name="a", image="img")))
    assert ctx["runAsNonRoot"] is True


def test_hardening_readonly_rootfs_preserved_true() -> None:
    """readonly_rootfs=True -> readOnlyRootFilesystem True.

    Mutation caught: dropping the field, or hard-coding False — either
    gives the agent a writable root filesystem.
    """
    ctx = _sec_ctx(_manifest(ContainerSpec(name="a", image="img", readonly_rootfs=True)))
    assert ctx["readOnlyRootFilesystem"] is True


def test_hardening_readonly_rootfs_false_is_respected() -> None:
    """readonly_rootfs=False -> readOnlyRootFilesystem False (not forced True).

    Mutation caught: hard-coding readOnlyRootFilesystem=True regardless of
    spec would silently break a coworker that legitimately disabled it.
    """
    ctx = _sec_ctx(_manifest(ContainerSpec(name="a", image="img", readonly_rootfs=False)))
    assert ctx["readOnlyRootFilesystem"] is False


def test_hardening_disables_privilege_escalation() -> None:
    """no-new-privileges -> allowPrivilegeEscalation: False.

    Mutation caught: omitting the key (K8s default is True) re-enables
    setuid escalation inside the sandbox.
    """
    ctx = _sec_ctx(_manifest(ContainerSpec(name="a", image="img")))
    assert ctx["allowPrivilegeEscalation"] is False


def test_hardening_seccomp_runtime_default() -> None:
    """seccomp default profile -> seccompProfile.type RuntimeDefault.

    Mutation caught: omitting the profile (Unconfined) widens the syscall
    surface the docker side restricts.
    """
    ctx = _sec_ctx(_manifest(ContainerSpec(name="a", image="img")))
    assert ctx["seccompProfile"] == {"type": "RuntimeDefault"}


def test_user_uid_gid_split() -> None:
    """spec.user 'uid:gid' -> runAsUser/runAsGroup ints (docker User parity).

    Mutation caught: forgetting to split on ':' (would int('1000:1000')
    -> ValueError) or swapping uid/gid.
    """
    ctx = _sec_ctx(_manifest(ContainerSpec(name="a", image="img", user="1000:2000")))
    assert ctx["runAsUser"] == 1000
    assert ctx["runAsGroup"] == 2000


def test_user_uid_only_no_group() -> None:
    ctx = _sec_ctx(_manifest(ContainerSpec(name="a", image="img", user="1000")))
    assert ctx["runAsUser"] == 1000
    assert "runAsGroup" not in ctx


def test_cap_add_when_present() -> None:
    spec = ContainerSpec(name="a", image="img", cap_add=["NET_BIND_SERVICE"])
    caps = _sec_ctx(_manifest(spec))["capabilities"]
    assert caps["add"] == ["NET_BIND_SERVICE"]
    assert caps["drop"] == ["ALL"]


# ===========================================================================
# DATA_DIR mount translation (docs/21 §7.1). The K8s side is FAIL-CLOSED:
# a source outside DATA_DIR is physically impossible (no host fs in the
# orchestrator pod) and must raise pointing at agent.extraVolumes —
# unlike the docker side, which passes such mounts through with a warning.
# ===========================================================================


def test_mount_outside_data_dir_fails_closed_with_extravolumes_hint() -> None:
    """A bind source outside DATA_DIR must RAISE (not pass through).

    This is THE behavioural divergence from docker. Mutation caught:
    copying docker_runtime's pass-through semantics here (returning the
    path instead of raising) would silently drop a mount on K8s.
    """
    spec = ContainerSpec(
        name="a",
        image="img",
        mounts=[VolumeMount(host_path="/etc/secrets", container_path="/s", readonly=True)],
    )
    with pytest.raises(ValueError) as ei:
        _manifest(spec)
    msg = str(ei.value)
    assert "agent.extraVolumes" in msg
    assert "/etc/secrets" in msg  # the offending source is named


def test_mount_traversal_escape_rejected() -> None:
    """DATA_DIR/x/../../etc must not masquerade as 'under DATA_DIR'.

    Mutation caught: comparing raw strings instead of normpath would let
    a ``..`` path slip the containment check and translate to a bogus
    subPath that escapes the PVC root.
    """
    escape = f"{_DATA_DIR}/spawns/../../etc/shadow"
    spec = ContainerSpec(
        name="a",
        image="img",
        mounts=[VolumeMount(host_path=escape, container_path="/x", readonly=True)],
    )
    with pytest.raises(ValueError):
        _manifest(spec)


def test_mount_subpath_translation_relative_to_data_dir() -> None:
    """A source under DATA_DIR maps to the PVC subPath = relative path.

    Mutation caught: using the absolute host_path as subPath (or the
    wrong relative root) would point the mount at the wrong PVC subtree.
    """
    spec = ContainerSpec(
        name="a",
        image="img",
        mounts=[
            VolumeMount(
                host_path=f"{_DATA_DIR}/spawns/job1/skills",
                container_path="/skills",
                readonly=True,
            )
        ],
    )
    vms = _container(_manifest(spec))["volumeMounts"]
    assert len(vms) == 1
    assert vms[0]["subPath"] == "spawns/job1/skills"
    assert vms[0]["mountPath"] == "/skills"
    assert vms[0]["name"] == "data"


def test_mount_at_data_dir_root_omits_subpath() -> None:
    """A mount exactly at DATA_DIR (relpath '.') must NOT set subPath.

    Mutation caught: emitting ``subPath: "."`` mounts a non-existent
    subdir named ".", breaking the mount.
    """
    spec = ContainerSpec(
        name="a",
        image="img",
        mounts=[VolumeMount(host_path=_DATA_DIR, container_path="/data", readonly=False)],
    )
    vm = _container(_manifest(spec))["volumeMounts"][0]
    assert "subPath" not in vm


def test_mount_readonly_flag_fidelity_ro() -> None:
    """readonly=True must produce readOnly: True.

    Mutation caught: dropping the readOnly mapping would silently make a
    read-only mount writable.
    """
    spec = ContainerSpec(
        name="a",
        image="img",
        mounts=[
            VolumeMount(
                host_path=f"{_DATA_DIR}/ro", container_path="/ro", readonly=True
            )
        ],
    )
    vm = _container(_manifest(spec))["volumeMounts"][0]
    assert vm["readOnly"] is True


def test_mount_readonly_flag_fidelity_rw() -> None:
    """readonly=False must produce readOnly: False (not silently True).

    Mutation caught: hard-coding readOnly=True would break writable
    mounts; inverting the bool would expose a ro mount as writable.
    """
    spec = ContainerSpec(
        name="a",
        image="img",
        mounts=[
            VolumeMount(
                host_path=f"{_DATA_DIR}/rw", container_path="/rw", readonly=False
            )
        ],
    )
    vm = _container(_manifest(spec))["volumeMounts"][0]
    assert vm["readOnly"] is False


def test_mounts_share_single_data_pvc_volume() -> None:
    """Multiple DATA_DIR mounts ride one 'data' PVC volume via subPath.

    Mutation caught: declaring a separate PVC volume per mount (K8s
    forbids two volumes claiming the same RWO PVC on one pod).
    """
    spec = ContainerSpec(
        name="a",
        image="img",
        mounts=[
            VolumeMount(host_path=f"{_DATA_DIR}/a", container_path="/a", readonly=True),
            VolumeMount(host_path=f"{_DATA_DIR}/b", container_path="/b", readonly=False),
        ],
    )
    m = _manifest(spec)
    pvc_volumes = [v for v in m["spec"]["volumes"] if "persistentVolumeClaim" in v]
    assert len(pvc_volumes) == 1
    assert pvc_volumes[0]["persistentVolumeClaim"]["claimName"] == _DATA_PVC
    assert pvc_volumes[0]["name"] == "data"


# ===========================================================================
# tmpfs -> emptyDir(medium=Memory, sizeLimit) (docs/21 §8).
# ===========================================================================


def test_tmpfs_maps_to_memory_backed_emptydir() -> None:
    """tmpfs becomes emptyDir with medium=Memory (RAM-backed, not disk).

    Mutation caught: omitting medium=Memory makes a disk-backed scratch
    dir, defeating the tmpfs intent (secrets touch disk).
    """
    spec = ContainerSpec(name="a", image="img", tmpfs={"/tmp": "rw,size=64m"})
    m = _manifest(spec)
    tmp_vol = next(v for v in m["spec"]["volumes"] if v["name"] == "tmpfs-tmp")
    assert tmp_vol["emptyDir"]["medium"] == "Memory"
    assert tmp_vol["emptyDir"]["sizeLimit"] == "64Mi"
    # And it is mounted writable at the requested path.
    vm = next(
        v for v in _container(m)["volumeMounts"] if v["name"] == "tmpfs-tmp"
    )
    assert vm["mountPath"] == "/tmp"
    assert vm["readOnly"] is False


def test_tmpfs_size_unit_conversion_gigabytes() -> None:
    """size=1g -> 1Gi (binary K8s quantity), not '1g'.

    Mutation caught: passing the docker suffix through verbatim ('1g' is
    not a valid K8s quantity and the kubelet would reject the pod).
    """
    spec = ContainerSpec(name="a", image="img", tmpfs={"/scratch": "size=1g"})
    vol = next(
        v for v in _manifest(spec)["spec"]["volumes"] if v["name"].startswith("tmpfs-")
    )
    assert vol["emptyDir"]["sizeLimit"] == "1Gi"


def test_tmpfs_without_size_is_unbounded() -> None:
    """No size option -> emptyDir with no sizeLimit key (unbounded).

    Mutation caught: defaulting to sizeLimit '0' would create a
    zero-capacity tmpfs that fails the first write.
    """
    spec = ContainerSpec(name="a", image="img", tmpfs={"/tmp": "rw,uid=1000"})
    vol = next(
        v for v in _manifest(spec)["spec"]["volumes"] if v["name"].startswith("tmpfs-")
    )
    assert "sizeLimit" not in vol["emptyDir"]


def test_distinct_tmpfs_paths_get_distinct_volume_names() -> None:
    """Two tmpfs mounts must not collide on volume name (DNS-1123).

    Mutation caught: a constant volume name would make the second
    emptyDir shadow the first.
    """
    spec = ContainerSpec(
        name="a",
        image="img",
        tmpfs={"/tmp": "size=8m", "/var/run": "size=8m"},
    )
    names = {
        v["name"]
        for v in _manifest(spec)["spec"]["volumes"]
        if v["name"].startswith("tmpfs-")
    }
    assert len(names) == 2


# ===========================================================================
# Pod-level fields: restartPolicy, SA token, runtimeClass, DNS, pull secret,
# labels.
# ===========================================================================


def test_pod_is_one_shot_and_token_starved() -> None:
    """restartPolicy Never + automountServiceAccountToken False (docs/21 §4.4).

    Mutation caught: restartPolicy=Always would resurrect a finished
    agent; automounting the SA token would hand the agent the
    orchestrator's K8s API creds — a privilege escalation.
    """
    pod_spec = _manifest(ContainerSpec(name="a", image="img"))["spec"]
    assert pod_spec["restartPolicy"] == "Never"
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["enableServiceLinks"] is False


def test_agent_labels_stamped() -> None:
    """The contract labels (role + managed-by) must be on every pod.

    Mutation caught: dropping a label detaches the pod from both the
    NetworkPolicy selectors (isolation) and cleanup_orphans (reaping).
    """
    labels = _manifest(ContainerSpec(name="a", image="img"))["metadata"]["labels"]
    assert labels[AGENT_ROLE_LABEL] == AGENT_ROLE_VALUE
    assert labels[AGENT_MANAGED_BY_LABEL] == AGENT_MANAGED_BY_VALUE


def test_dns_forces_resolver_via_dnspolicy_none() -> None:
    """spec.dns -> dnsPolicy None + dnsConfig.nameservers (docs/21 §3).

    Mutation caught: leaving the cluster-default dnsPolicy lets the agent
    reach kube-dns directly, bypassing the gateway resolver (DNS exfil).
    """
    spec = ContainerSpec(name="a", image="img", dns=["172.28.100.53"])
    pod_spec = _manifest(spec)["spec"]
    assert pod_spec["dnsPolicy"] == "None"
    assert pod_spec["dnsConfig"]["nameservers"] == ["172.28.100.53"]


def test_dns_config_carries_cluster_search_and_ndots() -> None:
    """dnsPolicy None drops the kubelet resolv.conf, so the agent pod must
    bring the cluster search domains + ndots:5 itself — without them a short
    name like ``nats`` is sent absolute and never completes to its FQDN, so
    the gateway's cluster.local exemption never fires (docs/21 §6.3).

    Mutation caught: omitting searches, or dropping ndots below the search
    depth, silently breaks internal-name resolution.
    """
    spec = ContainerSpec(name="a", image="img", dns=["172.28.100.53"])
    dns_config = _manifest(spec, namespace="team-x")["spec"]["dnsConfig"]
    assert dns_config["searches"] == [
        "team-x.svc.cluster.local",
        "svc.cluster.local",
        "cluster.local",
    ]
    assert {"name": "ndots", "value": "5"} in dns_config["options"]


def test_no_dns_keeps_cluster_default() -> None:
    """Empty spec.dns must NOT pin dnsPolicy None (would break the gateway pod).

    Mutation caught: always setting dnsPolicy None with empty nameservers
    yields a pod with no resolver at all.
    """
    pod_spec = _manifest(ContainerSpec(name="a", image="img"))["spec"]
    assert "dnsPolicy" not in pod_spec
    assert "dnsConfig" not in pod_spec


def test_gvisor_runtime_class_only_for_runsc() -> None:
    """spec.runtime 'runsc' -> runtimeClassName; 'runc'/None -> absent.

    Mutation caught: always emitting runtimeClassName would schedule
    every pod onto gVisor (or fail when the class is absent); never
    emitting it silently drops the gVisor isolation request.
    """
    runsc = _manifest(ContainerSpec(name="a", image="img", runtime="runsc"))["spec"]
    assert runsc["runtimeClassName"] == "gvisor"

    custom = _manifest(
        ContainerSpec(name="a", image="img", runtime="runsc"),
        runtime_class="gvisor-rke2",
    )["spec"]
    assert custom["runtimeClassName"] == "gvisor-rke2"

    runc = _manifest(ContainerSpec(name="a", image="img", runtime="runc"))["spec"]
    assert "runtimeClassName" not in runc

    default = _manifest(ContainerSpec(name="a", image="img"))["spec"]
    assert "runtimeClassName" not in default


def test_image_pull_secret_mapped_when_set() -> None:
    spec = ContainerSpec(name="a", image="img")
    pod_spec = _manifest(spec, image_pull_secret="regcred")["spec"]
    assert pod_spec["imagePullSecrets"] == [{"name": "regcred"}]
    # Absent when empty.
    assert "imagePullSecrets" not in _manifest(spec)["spec"]


def test_image_pull_policy_default_and_override() -> None:
    """Default IfNotPresent, else the configured policy.

    Mutation caught: omitting imagePullPolicy lets K8s default a :latest
    tag to Always, which ImagePullBackOffs against kind-loaded local
    images (no registry to pull from) — exactly the failure the kind
    integration hit. Asserting the field is present guards the fix.
    """
    container = _manifest(ContainerSpec(name="a", image="img:latest"))["spec"][
        "containers"
    ][0]
    assert container["imagePullPolicy"] == "IfNotPresent"

    pulled = _manifest(
        ContainerSpec(name="a", image="img:latest"), image_pull_policy="Always"
    )["spec"]["containers"][0]
    assert pulled["imagePullPolicy"] == "Always"


def test_resource_limits_memory_and_cpu() -> None:
    """memory '512m' -> bytes quantity; cpu 1.5 -> millicores.

    Mutation caught: passing '512m' through verbatim (K8s reads 'm' as
    millibytes, a 2000x under-limit); using whole cores instead of
    millicores for fractional CPU.
    """
    spec = ContainerSpec(name="a", image="img", memory_limit="512m", cpu_limit=1.5)
    limits = _container(_manifest(spec))["resources"]["limits"]
    assert limits["memory"] == "536870912"  # 512 * 1024**2 bytes
    assert limits["cpu"] == "1500m"


def test_env_mapped_as_name_value_pairs() -> None:
    spec = ContainerSpec(name="a", image="img", env={"FOO": "bar"})
    env = _container(_manifest(spec))["env"]
    assert {"name": "FOO", "value": "bar"} in env


# ===========================================================================
# _terminal_exit_code against REAL V1Pod deserialization shapes.
# ===========================================================================


def _pod(phase: str | None, exit_code: int | None) -> k8s_client.V1Pod:
    statuses = None
    if exit_code is not None:
        term = k8s_client.V1ContainerStateTerminated(exit_code=exit_code, reason="x")
        cs = k8s_client.V1ContainerStatus(
            name="agent",
            image="img",
            image_id="",
            ready=False,
            restart_count=0,
            state=k8s_client.V1ContainerState(terminated=term),
        )
        statuses = [cs]
    return k8s_client.V1Pod(
        metadata=k8s_client.V1ObjectMeta(name="p", resource_version="7"),
        status=k8s_client.V1PodStatus(phase=phase, container_statuses=statuses),
    )


def test_exit_code_from_terminated_state() -> None:
    assert _terminal_exit_code(_pod("Failed", 137)) == 137
    assert _terminal_exit_code(_pod("Succeeded", 0)) == 0


def test_exit_code_none_while_running() -> None:
    """A running pod must report None (still in flight), not a sentinel.

    Mutation caught: returning -1 or 0 for a running pod would make
    wait() return prematurely.
    """
    assert _terminal_exit_code(_pod("Running", None)) is None


def test_exit_code_minus_one_for_terminal_phase_without_container_state() -> None:
    """Evicted/terminal pod with no terminated container -> -1 (docs/21).

    Mutation caught: returning None here would hang wait() forever on a
    pod that will never produce a container status.
    """
    assert _terminal_exit_code(_pod("Failed", None)) == -1


# ===========================================================================
# Fakes at the API-client boundary.
# ===========================================================================


class _Resp:
    """A fake aiohttp-style streaming response for read_namespaced_pod_log."""

    def __init__(self, lines: list[bytes]) -> None:
        self.content = _AsyncLines(lines)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _AsyncLines:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self) -> _AsyncLines:
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeWatch:
    """Watch double: yields a scripted event list, supports async with/for."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def stream(self, _func: Any, **_kwargs: Any) -> _FakeWatch:
        return self

    async def __aenter__(self) -> _FakeWatch:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    def __aiter__(self) -> _FakeWatch:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


def _api_exc(status: int) -> ApiException:
    exc = ApiException(status=status)
    return exc


# ===========================================================================
# wait(): watch path, periodic-read fallback, code propagation.
# ===========================================================================


@pytest.mark.asyncio
async def test_wait_returns_exit_code_from_watch_event() -> None:
    """watch delivers terminated.exitCode -> wait() returns it.

    Mutation caught: reading the wrong status field, or ignoring DELETED
    vs terminated distinction.
    """
    running = _pod("Running", None)
    finished = _pod("Failed", 3)

    reads = [running]

    class _Core:
        async def read_namespaced_pod(self, _name: str, _ns: str) -> k8s_client.V1Pod:
            return reads.pop(0)

        async def list_namespaced_pod(self, *_a: Any, **_k: Any) -> Any:
            raise AssertionError("real list is never called; watch is faked")

    watch = _FakeWatch([{"type": "MODIFIED", "object": finished}])
    handle = K8sPodHandle(
        core=_Core(), namespace=_NS, name="p", watch_factory=lambda: watch
    )
    assert await handle.wait() == 3


@pytest.mark.asyncio
async def test_wait_returns_immediately_if_already_terminal_on_read() -> None:
    """If the first read shows a terminal pod, no watch is needed.

    Mutation caught: unconditionally entering the watch even when the
    read already has the exit code would hang on a pod that produced no
    further events.
    """
    finished = _pod("Succeeded", 0)

    class _Core:
        async def read_namespaced_pod(self, _name: str, _ns: str) -> k8s_client.V1Pod:
            return finished

    def _watch_factory() -> Any:
        raise AssertionError("watch must not be created when read is terminal")

    handle = K8sPodHandle(
        core=_Core(), namespace=_NS, name="p", watch_factory=_watch_factory
    )
    assert await handle.wait() == 0


@pytest.mark.asyncio
async def test_wait_periodic_read_fallback_when_watch_stream_breaks() -> None:
    """Watch yields nothing terminal (stream ends); the NEXT read catches it.

    This is the docs/21 §10 'K8s watch drops' safety net. Mutation
    caught: relying solely on the watch (no fresh read each loop) would
    hang forever when every watch window expires before the terminal
    event.
    """
    running = _pod("Running", None)
    finished = _pod("Failed", 9)
    # First read: running. Watch then ends with no terminal event.
    # Second loop read: finished.
    reads = [running, finished]

    class _Core:
        async def read_namespaced_pod(self, _name: str, _ns: str) -> k8s_client.V1Pod:
            return reads.pop(0)

        async def list_namespaced_pod(self, *_a: Any, **_k: Any) -> Any:
            raise AssertionError("real list is never called; watch is faked")

    # Empty event list -> stream ends immediately without a terminal event.
    handle = K8sPodHandle(
        core=_Core(), namespace=_NS, name="p", watch_factory=lambda: _FakeWatch([])
    )
    # Patch the inter-loop sleep so the test is instant.
    import rolemesh.container.k8s_runtime as mod

    original = mod._WAIT_RETRY_DELAY_S
    mod._WAIT_RETRY_DELAY_S = 0.0
    try:
        assert await handle.wait() == 9
    finally:
        mod._WAIT_RETRY_DELAY_S = original


@pytest.mark.asyncio
async def test_wait_returns_minus_one_when_pod_vanishes() -> None:
    """A 404 on the read means the pod is gone -> -1 (unrecoverable).

    Mutation caught: re-raising the 404 would crash the await path
    instead of degrading to an unknown exit code.
    """

    class _Core:
        async def read_namespaced_pod(self, _name: str, _ns: str) -> k8s_client.V1Pod:
            raise _api_exc(404)

    handle = K8sPodHandle(
        core=_Core(), namespace=_NS, name="p", watch_factory=lambda: _FakeWatch([])
    )
    assert await handle.wait() == -1


@pytest.mark.asyncio
async def test_wait_deleted_event_returns_minus_one() -> None:
    """A DELETED watch event -> -1 (pod removed underneath us)."""
    running = _pod("Running", None)

    class _Core:
        async def read_namespaced_pod(self, _name: str, _ns: str) -> k8s_client.V1Pod:
            return running

        async def list_namespaced_pod(self, *_a: Any, **_k: Any) -> Any:
            raise AssertionError("real list is never called; watch is faked")

    watch = _FakeWatch([{"type": "DELETED", "object": running}])
    handle = K8sPodHandle(
        core=_Core(), namespace=_NS, name="p", watch_factory=lambda: watch
    )
    assert await handle.wait() == -1


@pytest.mark.asyncio
async def test_read_stderr_retries_past_containercreating_400() -> None:
    """A 400 (ContainerCreating) is retried, not surfaced (docs/21 §8).

    Mutation caught: surfacing the 400 would make every freshly-created
    pod's log read fail because run() returns before the kubelet starts
    the container.
    """
    calls = {"n": 0}

    class _Core:
        async def read_namespaced_pod_log(self, *_a: Any, **_k: Any) -> _Resp:
            calls["n"] += 1
            if calls["n"] == 1:
                raise _api_exc(400)
            return _Resp([b"diagnostic line\n"])

    import rolemesh.container.k8s_runtime as mod

    original = mod._LOG_RETRY_INTERVAL_S
    mod._LOG_RETRY_INTERVAL_S = 0.0
    try:
        handle = K8sPodHandle(
            core=_Core(), namespace=_NS, name="p", watch_factory=lambda: _FakeWatch([])
        )
        out = [chunk async for chunk in handle.read_stderr()]
    finally:
        mod._LOG_RETRY_INTERVAL_S = original
    assert out == [b"diagnostic line\n"]
    assert calls["n"] == 2  # one failed attempt, one success


# ===========================================================================
# run(): delete-then-create ordering.
# ===========================================================================


class _RecordingCore:
    """Records the order of delete/read/create calls; scripts read results."""

    def __init__(self, read_sequence: list[Any]) -> None:
        self.calls: list[str] = []
        self._reads = list(read_sequence)

    async def delete_namespaced_pod(self, _name: str, _ns: str, **_k: Any) -> None:
        self.calls.append("delete")

    async def read_namespaced_pod(self, _name: str, _ns: str) -> Any:
        self.calls.append("read")
        result = self._reads.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def create_namespaced_pod(self, *, namespace: str, body: Any) -> None:
        self.calls.append("create")


class _FakeWatchMod:
    """Stand-in for the kubernetes_asyncio.watch module's Watch factory."""

    @staticmethod
    def Watch() -> _FakeWatch:  # mirrors the upstream Watch class name
        return _FakeWatch([])


def _inject_runtime(rt: K8sRuntime, core: object) -> None:
    """Inject test doubles at the private client boundary.

    These slots hold real kubernetes_asyncio clients/modules in
    production; the boundary is exactly where doubling is permitted.
    ``_core`` is typed Any in the runtime so it accepts the double
    directly; ``_watch_mod`` is typed as the watch module, so the
    stand-in needs one ignore.
    """
    rt._core = core
    rt._watch_mod = _FakeWatchMod  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_run_deletes_then_waits_404_then_creates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run() must delete, wait until read returns 404, THEN create.

    Mutation caught: creating before the old pod is gone races a 409
    (the very thing delete-then-create exists to avoid); skipping the
    delete entirely would leave a stale same-name pod.
    """
    # delete succeeds; first read still finds the dying pod; second read 404.
    core = _RecordingCore(read_sequence=[_pod("Running", None), _api_exc(404)])

    rt = K8sRuntime()
    _inject_runtime(rt, core)

    import rolemesh.container.k8s_runtime as mod

    monkeypatch.setattr(mod, "_REPLACE_WAIT_INTERVAL_S", 0.0)
    handle = await rt.run(ContainerSpec(name="agent-x", image="img"))

    assert handle.name == "agent-x"
    # delete precedes create, and a 404-confirming read sits between them.
    assert core.calls[0] == "delete"
    assert "create" in core.calls
    assert core.calls.index("delete") < core.calls.index("create")
    last_read = max(i for i, c in enumerate(core.calls) if c == "read")
    assert last_read < core.calls.index("create")


@pytest.mark.asyncio
async def test_run_no_existing_pod_creates_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the initial delete 404s (nothing there), create proceeds at once.

    Mutation caught: treating a 404-on-delete as fatal would make the
    common 'first run' path impossible.
    """
    core = _RecordingCore(read_sequence=[])

    async def _delete_404(_name: str, _ns: str, **_k: Any) -> None:
        core.calls.append("delete")
        raise _api_exc(404)

    monkeypatch.setattr(core, "delete_namespaced_pod", _delete_404)

    rt = K8sRuntime()
    _inject_runtime(rt, core)

    await rt.run(ContainerSpec(name="agent-y", image="img"))
    assert core.calls == ["delete", "create"]


# ===========================================================================
# cleanup_orphans(): triple-filter safety invariant (docs/21 §3, INV-3).
# ===========================================================================


def _orphan_pod(name: str, image: str) -> k8s_client.V1Pod:
    return k8s_client.V1Pod(
        metadata=k8s_client.V1ObjectMeta(name=name),
        spec=k8s_client.V1PodSpec(
            containers=[k8s_client.V1Container(name="agent", image=image)]
        ),
    )


class _CleanupCore:
    def __init__(self, pods: list[k8s_client.V1Pod]) -> None:
        self._pods = pods
        self.deleted: list[str] = []

    async def list_namespaced_pod(self, _ns: str, **_k: Any) -> Any:
        return k8s_client.V1PodList(items=self._pods)

    async def delete_namespaced_pod(self, name: str, _ns: str, **_k: Any) -> None:
        self.deleted.append(name)


@pytest.mark.asyncio
async def test_cleanup_skips_pod_with_disallowed_image() -> None:
    """Label + name-prefix match but image NOT in allowlist -> NOT deleted.

    This is the security invariant: the image allowlist is the positive
    identity signal that protects an unrelated pod which merely happens
    to carry our managed-by label and name prefix (e.g. a copied
    manifest). Mutation caught: dropping the image check (deleting on
    label+prefix alone) would let cleanup reap pods we never launched.
    """
    pods = [_orphan_pod("rolemesh-agent-1", "evil/other:latest")]
    core = _CleanupCore(pods)
    rt = K8sRuntime()
    rt._core = core
    removed = await rt.cleanup_orphans(
        "rolemesh-agent-", allowed_images=frozenset({"rolemesh-agent:latest"})
    )
    assert removed == []
    assert core.deleted == []


@pytest.mark.asyncio
async def test_cleanup_skips_pod_with_wrong_prefix() -> None:
    """Label + allowed image but name prefix mismatch -> NOT deleted.

    Mutation caught: dropping the prefix re-check (trusting the
    server-side label selector alone) would reap correctly-imaged pods
    from a different naming scope.
    """
    pods = [_orphan_pod("other-pod-1", "rolemesh-agent:latest")]
    core = _CleanupCore(pods)
    rt = K8sRuntime()
    rt._core = core
    removed = await rt.cleanup_orphans(
        "rolemesh-agent-", allowed_images=frozenset({"rolemesh-agent:latest"})
    )
    assert removed == []


@pytest.mark.asyncio
async def test_cleanup_deletes_only_triple_match() -> None:
    """All three (label via selector + prefix + allowed image) -> deleted.

    Mutation caught: inverting any filter; the positive case proves the
    skip tests above are not vacuously passing because cleanup never
    deletes anything.
    """
    pods = [
        _orphan_pod("rolemesh-agent-keep", "rolemesh-agent:latest"),  # delete
        _orphan_pod("rolemesh-agent-bad", "stranger:latest"),  # wrong image
        _orphan_pod("foreign-x", "rolemesh-agent:latest"),  # wrong prefix
    ]
    core = _CleanupCore(pods)
    rt = K8sRuntime()
    rt._core = core
    removed = await rt.cleanup_orphans(
        "rolemesh-agent-", allowed_images=frozenset({"rolemesh-agent:latest"})
    )
    assert removed == ["rolemesh-agent-keep"]
    assert core.deleted == ["rolemesh-agent-keep"]


# ===========================================================================
# verify_infrastructure(): each invariant fails closed with guidance.
# These drive the real verify code through a fake API surface; each test
# breaks ONE invariant and asserts the resulting RuntimeError.
# ===========================================================================


def _np(name: str, *, match_labels: dict[str, str] | None, match_expressions: bool = False) -> Any:
    exprs = (
        [k8s_client.V1LabelSelectorRequirement(key="x", operator="Exists")]
        if match_expressions
        else None
    )
    selector = k8s_client.V1LabelSelector(
        match_labels=match_labels, match_expressions=exprs
    )
    return k8s_client.V1NetworkPolicy(
        metadata=k8s_client.V1ObjectMeta(name=name),
        spec=k8s_client.V1NetworkPolicySpec(
            pod_selector=selector, policy_types=["Ingress", "Egress"]
        ),
    )


_GOOD_POLICIES: dict[str, Any] = {
    "agent-default-deny": _np(
        "agent-default-deny", match_labels={AGENT_ROLE_LABEL: AGENT_ROLE_VALUE}
    ),
    "agent-allow-egress": _np(
        "agent-allow-egress", match_labels={AGENT_ROLE_LABEL: AGENT_ROLE_VALUE}
    ),
    "gateway-policy": _np("gateway-policy", match_labels={"app": "egress-gateway"}),
    "orchestrator-policy": _np(
        "orchestrator-policy", match_labels={"app": "orchestrator"}
    ),
}


class _VerifyCore:
    """CoreV1Api double for verify: namespace/PVC/Service reads."""

    def __init__(
        self,
        *,
        pvc_phase: str = "Bound",
        pvc_missing: bool = False,
        service_cluster_ip: str = "10.0.0.5",
    ) -> None:
        self._pvc_phase = pvc_phase
        self._pvc_missing = pvc_missing
        self._service_cluster_ip = service_cluster_ip

    async def read_namespaced_persistent_volume_claim(self, _name: str, _ns: str) -> Any:
        if self._pvc_missing:
            raise _api_exc(404)
        return k8s_client.V1PersistentVolumeClaim(
            metadata=k8s_client.V1ObjectMeta(name=_name),
            status=k8s_client.V1PersistentVolumeClaimStatus(phase=self._pvc_phase),
        )

    async def read_namespaced_service(self, _name: str, _ns: str) -> Any:
        return k8s_client.V1Service(
            metadata=k8s_client.V1ObjectMeta(name=_name),
            spec=k8s_client.V1ServiceSpec(cluster_ip=self._service_cluster_ip),
        )


class _VerifyNet:
    def __init__(self, policies: dict[str, Any]) -> None:
        self._policies = policies

    async def read_namespaced_network_policy(self, name: str, _ns: str) -> Any:
        if name not in self._policies:
            raise _api_exc(404)
        return self._policies[name]


class _VerifyAuth:
    def __init__(self, *, allow: bool = True, deny_verb: str | None = None) -> None:
        self._allow = allow
        self._deny_verb = deny_verb

    async def create_self_subject_access_review(self, *, body: dict[str, Any]) -> Any:
        verb = body["spec"]["resourceAttributes"]["verb"]
        allowed = self._allow and verb != self._deny_verb
        return k8s_client.V1SelfSubjectAccessReview(
            spec=k8s_client.V1SelfSubjectAccessReviewSpec(),
            status=k8s_client.V1SubjectAccessReviewStatus(allowed=allowed),
        )


def _wire_verify(
    rt: K8sRuntime,
    *,
    core: _VerifyCore,
    net: _VerifyNet,
    auth: _VerifyAuth,
) -> None:
    rt._core = core
    rt._networking = net
    rt._authorization = auth


async def _patched_verify(rt: K8sRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run verify_infrastructure with the network reachability probes stubbed.

    The gateway-healthz and NATS checks open real sockets; this unit
    suite covers the OBJECT invariants (namespace/policies/PVC/ClusterIP/
    RBAC). The reachability checks are exercised by the contract suite.
    Only the two probe functions (bound into k8s_runtime at import) are
    replaced — the real verify code runs around them, including the
    object checks that precede AND the RBAC check that follows the
    probes.
    """

    async def _ok(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr("rolemesh.container.k8s_runtime._check_http_healthz", _ok)
    monkeypatch.setattr("rolemesh.container.k8s_runtime._check_tcp_reachable", _ok)
    await rt.verify_infrastructure()


@pytest.mark.asyncio
async def test_verify_passes_when_all_invariants_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: a fully-correct cluster passes.

    Guards against the negative tests being vacuously green (e.g. verify
    raising for an unrelated reason on every input).
    """
    monkeypatch.setattr("rolemesh.core.config.EGRESS_GATEWAY_DNS_IP", "10.0.0.5")
    rt = K8sRuntime()
    _wire_verify(
        rt,
        core=_VerifyCore(service_cluster_ip="10.0.0.5"),
        net=_VerifyNet(dict(_GOOD_POLICIES)),
        auth=_VerifyAuth(allow=True),
    )
    await _patched_verify(rt, monkeypatch)  # must not raise


@pytest.mark.asyncio
async def test_verify_fails_when_a_network_policy_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One missing NetworkPolicy -> fail-closed with Helm guidance.

    Mutation caught: tolerating a missing policy would let agents run
    without isolation in force.
    """
    policies = dict(_GOOD_POLICIES)
    del policies["agent-allow-egress"]
    rt = K8sRuntime()
    _wire_verify(
        rt,
        core=_VerifyCore(),
        net=_VerifyNet(policies),
        auth=_VerifyAuth(),
    )
    with pytest.raises(RuntimeError) as ei:
        await _patched_verify(rt, monkeypatch)
    msg = str(ei.value)
    assert "agent-allow-egress" in msg
    assert "helm" in msg.lower()


@pytest.mark.asyncio
async def test_verify_fails_when_agent_policy_selector_misses_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent policy whose selector omits the role label -> fail-closed.

    Mutation caught: skipping the selector-match check would accept a
    policy that exists but does not actually cover agent pods (isolation
    silently absent).
    """
    policies = dict(_GOOD_POLICIES)
    policies["agent-default-deny"] = _np(
        "agent-default-deny", match_labels={"app": "something-else"}
    )
    rt = K8sRuntime()
    _wire_verify(rt, core=_VerifyCore(), net=_VerifyNet(policies), auth=_VerifyAuth())
    with pytest.raises(RuntimeError, match="podSelector"):
        await _patched_verify(rt, monkeypatch)


@pytest.mark.asyncio
async def test_verify_fails_when_component_policy_also_selects_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A component policy that ALSO catches agents -> fail-closed (additive).

    NetworkPolicies are additive: a gateway policy whose selector matches
    agent pods would grant them the gateway's egress and defeat
    default-deny. Mutation caught: not rejecting an over-broad component
    selector.
    """
    policies = dict(_GOOD_POLICIES)
    # gateway-policy selector matches agents (empty -> all pods).
    policies["gateway-policy"] = _np("gateway-policy", match_labels={})
    rt = K8sRuntime()
    _wire_verify(rt, core=_VerifyCore(), net=_VerifyNet(policies), auth=_VerifyAuth())
    with pytest.raises(RuntimeError, match="default-deny"):
        await _patched_verify(rt, monkeypatch)


@pytest.mark.asyncio
async def test_verify_fails_when_pvc_not_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PVC in Pending (not Bound) -> fail-closed.

    Mutation caught: accepting any phase would let agents start with no
    storage actually provisioned.
    """
    rt = K8sRuntime()
    _wire_verify(
        rt,
        core=_VerifyCore(pvc_phase="Pending"),
        net=_VerifyNet(dict(_GOOD_POLICIES)),
        auth=_VerifyAuth(),
    )
    with pytest.raises(RuntimeError, match="Bound"):
        await _patched_verify(rt, monkeypatch)


@pytest.mark.asyncio
async def test_verify_fails_on_cluster_ip_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """Service ClusterIP != configured EGRESS_GATEWAY_DNS_IP -> fail-closed.

    Agents get the configured IP pinned as their resolver; a drift
    silently kills agent DNS. Mutation caught: skipping the ClusterIP
    comparison.
    """
    monkeypatch.setattr("rolemesh.core.config.EGRESS_GATEWAY_DNS_IP", "10.0.0.99")
    rt = K8sRuntime()
    _wire_verify(
        rt,
        core=_VerifyCore(service_cluster_ip="10.0.0.5"),  # drifted
        net=_VerifyNet(dict(_GOOD_POLICIES)),
        auth=_VerifyAuth(),
    )
    with pytest.raises(RuntimeError, match="ClusterIP"):
        await _patched_verify(rt, monkeypatch)


@pytest.mark.asyncio
async def test_verify_fails_when_rbac_denies_a_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    """A denied SelfSubjectAccessReview verb -> fail-closed with the verb named.

    Mutation caught: ignoring the review's 'allowed' flag would let the
    orchestrator start without the RBAC it needs to manage sandboxes,
    surfacing as opaque 403s at the first agent spawn instead.
    """
    monkeypatch.setattr("rolemesh.core.config.EGRESS_GATEWAY_DNS_IP", "10.0.0.5")
    rt = K8sRuntime()
    _wire_verify(
        rt,
        core=_VerifyCore(service_cluster_ip="10.0.0.5"),
        net=_VerifyNet(dict(_GOOD_POLICIES)),
        auth=_VerifyAuth(allow=True, deny_verb="delete"),
    )
    with pytest.raises(RuntimeError) as ei:
        await _patched_verify(rt, monkeypatch)
    assert "delete" in str(ei.value)


@pytest.mark.asyncio
async def test_verify_fails_when_namespace_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing namespace surfaces via the first namespaced read.

    verify deliberately does NOT do a cluster-scoped namespaces/get
    (it would need RBAC a namespaced Role cannot grant — docs/21
    §10.2). When the namespace is absent every namespaced read 404s;
    the NetworkPolicy check is first, and its error must name the
    namespace-missing case so the operator is not sent hunting for a
    policy in a namespace that does not exist.

    Mutation caught: if someone re-adds a cluster-scoped namespace probe
    requiring namespaces/get, the pods-only RBAC would no longer be
    sufficient and this manifestation path would change.
    """
    monkeypatch.setattr("rolemesh.core.config.EGRESS_GATEWAY_DNS_IP", "10.0.0.5")
    rt = K8sRuntime()
    _wire_verify(
        rt,
        core=_VerifyCore(),
        # Namespace gone => every namespaced policy read 404s.
        net=_VerifyNet({}),
        auth=_VerifyAuth(),
    )
    with pytest.raises(RuntimeError, match="does not exist"):
        await _patched_verify(rt, monkeypatch)


# ===========================================================================
# Guard: calling sandbox methods before ensure_available() must fail loudly.
# ===========================================================================


@pytest.mark.asyncio
async def test_run_without_ensure_available_raises() -> None:
    """A clear error beats an AttributeError on None._core.

    Mutation caught: removing the _ensure_core guard would surface a
    confusing 'NoneType has no attribute' instead of actionable guidance.
    """
    rt = K8sRuntime()
    with pytest.raises(RuntimeError, match="ensure_available"):
        await rt.run(ContainerSpec(name="a", image="img"))


# ===========================================================================
# Node pinning (docs/21 §7.2). ``pin_node`` is a DECISION handed down by
# the deployment layer: the chart renders ROLEMESH_K8S_AGENT_PIN_NODE only
# in rwo-colocated storage mode, where the ReadWriteOnce data PVC attaches
# on exactly one node and an agent scheduled elsewhere hangs Pending
# forever on a Multi-Attach error. The manifest builder knows nothing
# about storage modes — it pins iff told to.
# ===========================================================================


def test_pin_node_sets_node_selector() -> None:
    manifest = _manifest(ContainerSpec(name="a", image="img"), pin_node="worker-1")
    assert manifest["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "worker-1"}


def test_no_pin_node_means_no_node_selector() -> None:
    # Mutation caught: unconditionally emitting the selector would pin
    # every deployment's agents to one node; an empty-string selector
    # would make the pod unschedulable everywhere.
    assert "nodeSelector" not in _manifest(ContainerSpec(name="a", image="img"))["spec"]
    assert (
        "nodeSelector"
        not in _manifest(ContainerSpec(name="a", image="img"), pin_node="")["spec"]
    )
