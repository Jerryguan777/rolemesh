"""Kubernetes implementation of ContainerRuntime (docs/21 §4.4).

Maps ContainerSpec onto bare Pods (``restartPolicy: Never``) in a single
namespace. Static infrastructure (NetworkPolicies, gateway Service, PVC,
NATS) is declared by the Helm chart; ``verify_infrastructure`` only checks
the declared invariants — read-only, fail-closed (docs/21 §4.2 rev4).

The ``kubernetes_asyncio`` dependency is optional (``[k8s]`` extra) and is
imported lazily: this module's top level stays import-free so importing it
never fails on docker-only deployments; ``K8sRuntime()`` raises with an
install hint when the extra is missing.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rolemesh.container.docker_runtime import (
    _check_http_healthz,
    _check_tcp_reachable,
    _normalize_image_ref,
    _parse_memory,
)
from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from rolemesh.container.runtime import ContainerSpec, VolumeMount

logger = get_logger()

# ---------------------------------------------------------------------------
# Contract constants (shared with the Helm chart and the contract suite).
# ---------------------------------------------------------------------------

# Labels stamped on every agent pod. They are a CONTRACT, not decoration:
#   - cleanup_orphans selects on AGENT_MANAGED_BY_LABEL/VALUE;
#   - the chart's agent NetworkPolicies select on AGENT_ROLE_LABEL/VALUE
#     (default-deny + gateway-only egress hang off this selector).
# Renaming either silently detaches agents from both mechanisms.
AGENT_ROLE_LABEL = "rolemesh.io/role"
AGENT_ROLE_VALUE = "agent"
AGENT_MANAGED_BY_LABEL = "rolemesh.io/managed-by"
AGENT_MANAGED_BY_VALUE = "orchestrator"

AGENT_POD_LABELS: dict[str, str] = {
    AGENT_ROLE_LABEL: AGENT_ROLE_VALUE,
    AGENT_MANAGED_BY_LABEL: AGENT_MANAGED_BY_VALUE,
}

# NetworkPolicy object names the chart must declare (docs/21 §6.1). The
# two agent-* policies must select the agent role label above; the other
# two must select their own components and must NOT select agent pods
# (NetworkPolicies are additive — a policy that also selected agents
# would grant them its wider egress and defeat default-deny).
NETPOL_AGENT_DEFAULT_DENY = "agent-default-deny"
NETPOL_AGENT_ALLOW_EGRESS = "agent-allow-egress"
NETPOL_GATEWAY = "gateway-policy"
NETPOL_ORCHESTRATOR = "orchestrator-policy"
REQUIRED_AGENT_NETWORK_POLICIES: tuple[str, ...] = (
    NETPOL_AGENT_DEFAULT_DENY,
    NETPOL_AGENT_ALLOW_EGRESS,
)
REQUIRED_COMPONENT_NETWORK_POLICIES: tuple[str, ...] = (
    NETPOL_GATEWAY,
    NETPOL_ORCHESTRATOR,
)

# Conventional RuntimeClass name for gVisor (docs/21 §6.4); overridable
# via ROLEMESH_K8S_RUNTIME_CLASS for clusters that registered it under a
# different name.
_DEFAULT_GVISOR_RUNTIME_CLASS = "gvisor"

# Cluster DNS domain (docs/21 §6.3). Agents run with dnsPolicy None, which
# drops the kubelet-injected resolv.conf entirely, so the agent pod must
# carry the cluster search domains itself or a short name like ``nats``
# is never completed to ``nats.<ns>.svc.cluster.local``. Overridable for
# clusters that customised --cluster-domain.
_CLUSTER_DOMAIN = os.environ.get("ROLEMESH_K8S_CLUSTER_DOMAIN", "cluster.local")

# Matches the kubelet ClusterFirst default. Names with fewer than this many
# dots get the search list appended before being tried absolute — so a
# 1-dot internal name resolves via search, and only genuinely-qualified
# external names skip it.
_NDOTS = "5"

_INSTALL_HINT = (
    "The Kubernetes container backend requires the optional "
    "kubernetes_asyncio dependency. Install it with: uv sync --extra k8s"
)

_HELM_HINT = (
    "Infrastructure is declared by the deployment layer, not created by "
    "the orchestrator — install/upgrade the chart: "
    "helm upgrade --install rolemesh deploy/charts/rolemesh "
    "-n <namespace> (docs/21 §10.2)"
)

# Retry budget for checks that race the deployment layer's cold start
# (gateway healthz, NATS). Static object checks (namespace, policies,
# PVC, Service) never retry — waiting cannot create an object. Same
# rationale and numbers as docker_runtime; separate constants so the
# contract suite's fast_verify fixture can patch each backend's knob.
_VERIFY_RETRY_BUDGET_S: float = 60.0
_VERIFY_RETRY_INTERVAL_S: float = 2.0

# run() delete-then-create: how long to wait for an old same-name pod to
# disappear (404) before create. Pods are deleted with grace 0 here, so
# normally this resolves in well under a second.
_REPLACE_WAIT_BUDGET_S: float = 60.0
_REPLACE_WAIT_INTERVAL_S: float = 0.2

# wait(): server-side watch window; on expiry the stream ends cleanly
# and we fall back to a fresh read (docs/21 §10 risk: K8s watch drops).
_WATCH_TIMEOUT_S: int = 60
# Pause between wait() loop iterations after a broken/expired stream so
# a flapping API server is not hot-looped.
_WAIT_RETRY_DELAY_S: float = 1.0

# read_stderr(): a follow-log request against a pod whose container has
# not started yet fails 400; retry briefly instead of surfacing it.
_LOG_RETRY_BUDGET_S: float = 60.0
_LOG_RETRY_INTERVAL_S: float = 0.5


def _api_status(exc: BaseException) -> int | None:
    """HTTP status of a kubernetes ApiException (None for anything else)."""
    status = getattr(exc, "status", None)
    return status if isinstance(status, int) else None


# ---------------------------------------------------------------------------
# ContainerSpec -> Pod manifest (pure functions, unit-testable without a
# cluster). Manifests are plain dicts: kubernetes_asyncio serializes dict
# bodies as-is, and dicts keep the mapping logic free of client imports.
# ---------------------------------------------------------------------------

_TMPFS_SIZE_RE = re.compile(r"(?:^|,)size=([0-9]+[bkmg]?)(?:,|$)")

_QUANTITY_SUFFIX: dict[str, str] = {"b": "", "k": "Ki", "m": "Mi", "g": "Gi"}


def _tmpfs_size_limit(options: str) -> str | None:
    """Extract the size limit of a docker tmpfs option string as a K8s quantity.

    ``"rw,size=64m,uid=1000,gid=1000,mode=700"`` -> ``"64Mi"``. uid/gid/mode
    have no emptyDir equivalent (docs/21 §8: the image runs as UID 1000 and
    the kubelet chowns emptyDir to the pod's runtime user); they are
    intentionally dropped. No size option -> None (unbounded emptyDir).
    """
    m = _TMPFS_SIZE_RE.search(options.lower())
    if m is None:
        return None
    raw = m.group(1)
    suffix = raw[-1]
    if suffix in _QUANTITY_SUFFIX:
        return f"{raw[:-1]}{_QUANTITY_SUFFIX[suffix]}"
    return raw  # plain byte count


def _tmpfs_volume_name(path: str) -> str:
    """Derive a DNS-1123 volume name from a tmpfs mount path."""
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
    return f"tmpfs-{slug}"


def _mount_subpath(mount: VolumeMount, *, data_dir: str) -> str:
    """Translate a VolumeMount source to a data-PVC subPath (docs/21 §7.1).

    Paths are compared lexically after ``normpath`` so ``DATA_DIR/x/../../etc``
    cannot masquerade as "under DATA_DIR". Sources outside DATA_DIR are
    REJECTED: unlike the docker side (pass-through + warning — the host
    dockerd can still resolve them), on Kubernetes the orchestrator has no
    host filesystem at all, so such a mount is physically impossible.
    Fail closed and point at the deployment-layer escape hatch.
    """
    normalized = Path(os.path.normpath(mount.host_path))
    data_root = Path(os.path.normpath(data_dir))
    try:
        rel = normalized.relative_to(data_root)
    except ValueError:
        msg = (
            f"Mount source {mount.host_path!r} (-> {mount.container_path!r}) "
            f"is outside DATA_DIR ({data_dir!r}). On Kubernetes host paths "
            "are not reachable from the orchestrator; only the shared data "
            "PVC is translated (docs/21 §7.1). Declare extra mounts in the "
            "chart's agent.extraVolumes instead."
        )
        raise ValueError(msg) from None
    return str(rel)


def _security_context(spec: ContainerSpec) -> dict[str, Any]:
    """Field-by-field hardening translation (docs/21 §3 mapping table).

    docker -> k8s:
      CapDrop/CapAdd            -> capabilities.drop/add
      ReadonlyRootfs            -> readOnlyRootFilesystem
      no-new-privileges         -> allowPrivilegeEscalation: false
      seccomp default profile   -> seccompProfile: RuntimeDefault
      User "uid:gid"            -> runAsUser/runAsGroup
    runAsNonRoot is always true: agent images run as UID 1000, and the
    namespace is PSA ``restricted`` anyway — an image that needs root must
    fail at admission, not silently get it.
    """
    ctx: dict[str, Any] = {
        "runAsNonRoot": True,
        "readOnlyRootFilesystem": bool(spec.readonly_rootfs),
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": list(spec.cap_drop)},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    if spec.cap_add:
        ctx["capabilities"]["add"] = list(spec.cap_add)
    if spec.user:
        uid_str, _, gid_str = spec.user.partition(":")
        ctx["runAsUser"] = int(uid_str)
        if gid_str:
            ctx["runAsGroup"] = int(gid_str)
    return ctx


def spec_to_pod_manifest(
    spec: ContainerSpec,
    *,
    namespace: str,
    data_dir: str,
    data_pvc: str,
    image_pull_secret: str = "",
    image_pull_policy: str = "IfNotPresent",
    runtime_class: str = "",
) -> dict[str, Any]:
    """Map a ContainerSpec to a bare-Pod manifest (docs/21 §4.4).

    Knowingly NOT mapped (with the design's blessing):
      - pids_limit: pod-level pids limits are a kubelet configuration
        (``podPidsLimit``), not a pod field — docs/21 §8/§10 (Helm NOTES +
        RKE2 kubelet doc carry the operational guidance).
      - memory_swap / memory_swappiness: swap is node-level in K8s; agent
        memory is bounded by resources.limits alone.
      - ulimits: container-runtime-level (CRI) configuration, no pod field.
      - network_name: docker attaches pods to a named bridge; on K8s
        isolation is the chart's NetworkPolicies selecting AGENT_POD_LABELS,
        so the field is ignored here.
      - remove_on_exit: pods are deleted explicitly by stop(), mirroring
        DockerRuntime (AutoRemove races wait/inspect there; here a Pod
        must outlive termination long enough for wait() to read its exit
        code).
    """
    volumes: list[dict[str, Any]] = []
    volume_mounts: list[dict[str, Any]] = []

    if spec.mounts:
        volumes.append(
            {
                "name": "data",
                "persistentVolumeClaim": {"claimName": data_pvc},
            }
        )
        for mount in spec.mounts:
            sub_path = _mount_subpath(mount, data_dir=data_dir)
            vm: dict[str, Any] = {
                "name": "data",
                "mountPath": mount.container_path,
                "readOnly": bool(mount.readonly),
            }
            if sub_path != ".":
                vm["subPath"] = sub_path
            volume_mounts.append(vm)

    # tmpfs -> emptyDir(medium=Memory, sizeLimit). uid/gid/mode options are
    # dropped (docs/21 §8: emptyDir has no ownership knobs; the image's
    # UID-1000 user owns the mount by default).
    for path, options in spec.tmpfs.items():
        name = _tmpfs_volume_name(path)
        empty_dir: dict[str, Any] = {"medium": "Memory"}
        size_limit = _tmpfs_size_limit(options)
        if size_limit is not None:
            empty_dir["sizeLimit"] = size_limit
        volumes.append({"name": name, "emptyDir": empty_dir})
        volume_mounts.append({"name": name, "mountPath": path, "readOnly": False})

    container: dict[str, Any] = {
        "name": "agent",
        "image": spec.image,
        # Default IfNotPresent: kind-loaded local images have no registry to
        # pull from, so the K8s default of "Always" for :latest tags would
        # ImagePullBackOff. IfNotPresent also works behind a registry with
        # explicit tags (docs/21 §10.3).
        "imagePullPolicy": image_pull_policy,
        "env": [{"name": k, "value": v} for k, v in spec.env.items()],
        "securityContext": _security_context(spec),
    }
    if spec.entrypoint:
        container["command"] = list(spec.entrypoint)
    if volume_mounts:
        container["volumeMounts"] = volume_mounts

    limits: dict[str, str] = {}
    if spec.memory_limit:
        # Plain byte count is a valid K8s quantity; reuse the docker-side
        # parser so "512m" means the same number of bytes on both backends.
        limits["memory"] = str(_parse_memory(spec.memory_limit))
    if spec.cpu_limit:
        # NanoCpus analog at millicore resolution.
        limits["cpu"] = f"{int(spec.cpu_limit * 1000)}m"
    if limits:
        container["resources"] = {"limits": limits}

    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "containers": [container],
    }
    if volumes:
        pod_spec["volumes"] = volumes

    # DNS forced through the gateway resolver (docs/21 §3): dnsPolicy None
    # replaces docker's HostConfig.Dns. Empty spec.dns keeps the cluster
    # default (kube-dns) — appropriate only for non-agent pods.
    #
    # dnsPolicy None drops the kubelet resolv.conf wholesale, so the search
    # domains + ndots must be supplied here. Without them a short internal
    # name (``nats``) is sent absolute and never completes to its FQDN; the
    # gateway resolver, in turn, routes anything under cluster.local to
    # kube-dns (the internal-name exemption — egress/dns_internal.py). The
    # two are partners: searches turn ``nats`` into a cluster.local FQDN,
    # the exemption then forwards that FQDN to kube-dns.
    if spec.dns:
        pod_spec["dnsPolicy"] = "None"
        pod_spec["dnsConfig"] = {
            "nameservers": list(spec.dns),
            "searches": [
                f"{namespace}.svc.{_CLUSTER_DOMAIN}",
                f"svc.{_CLUSTER_DOMAIN}",
                _CLUSTER_DOMAIN,
            ],
            "options": [{"name": "ndots", "value": _NDOTS}],
        }

    # extra_hosts -> hostAliases. On K8s the metadata blackhole duty is
    # carried by the default-deny NetworkPolicy, but the mapping is kept
    # so the spec field behaves identically on both backends.
    if spec.extra_hosts:
        by_ip: dict[str, list[str]] = {}
        for host, ip in spec.extra_hosts.items():
            by_ip.setdefault(ip, []).append(host)
        pod_spec["hostAliases"] = [
            {"ip": ip, "hostnames": hostnames} for ip, hostnames in by_ip.items()
        ]

    # gVisor (docs/21 §6.4): docker spec.runtime="runsc" becomes a
    # RuntimeClass reference; ROLEMESH_K8S_RUNTIME_CLASS overrides the
    # conventional class name. "runc"/None keep the cluster default.
    if spec.runtime == "runsc":
        pod_spec["runtimeClassName"] = runtime_class or _DEFAULT_GVISOR_RUNTIME_CLASS

    if image_pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": image_pull_secret}]

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": spec.name,
            "namespace": namespace,
            "labels": dict(AGENT_POD_LABELS),
        },
        "spec": pod_spec,
    }


# ---------------------------------------------------------------------------
# Pod status helpers
# ---------------------------------------------------------------------------


def _terminal_exit_code(pod: Any) -> int | None:
    """Exit code of a finished pod; None while it is still in flight.

    Reads ``containerStatuses[0].state.terminated.exitCode`` — present for
    both Succeeded and Failed phases (docs/21 §4.4). A pod in a terminal
    phase WITHOUT a terminated container state (e.g. evicted before start)
    yields -1, mirroring DockerRuntime's ``result.get("StatusCode", -1)``.
    """
    status = getattr(pod, "status", None)
    if status is None:
        return None
    statuses = getattr(status, "container_statuses", None) or []
    if statuses:
        terminated = getattr(getattr(statuses[0], "state", None), "terminated", None)
        if terminated is not None:
            return int(terminated.exit_code)
    if getattr(status, "phase", None) in ("Succeeded", "Failed"):
        return -1
    return None


async def _retry_within_budget(
    check: Callable[[], Awaitable[None]],
    *,
    what: str,
) -> None:
    """Run *check* until it passes or the k8s retry budget elapses.

    Same shape as docker_runtime's helper; duplicated (not imported) so
    each backend's budget constants can be patched independently by the
    contract suite's fast_verify fixture.
    """
    deadline = time.monotonic() + _VERIFY_RETRY_BUDGET_S
    attempt = 0
    while True:
        attempt += 1
        try:
            await check()
            return
        except RuntimeError as exc:
            if time.monotonic() >= deadline:
                msg = (
                    f"{what}: still failing after {attempt} attempts "
                    f"(~{_VERIFY_RETRY_BUDGET_S:.0f}s budget). {exc}"
                )
                raise RuntimeError(msg) from exc
            logger.debug(
                "verify_infrastructure check not passing yet — retrying",
                what=what,
                attempt=attempt,
                error=str(exc),
            )
            await asyncio.sleep(_VERIFY_RETRY_INTERVAL_S)


# ---------------------------------------------------------------------------
# Handle
# ---------------------------------------------------------------------------


class K8sPodHandle:
    """Handle to a running agent Pod."""

    def __init__(
        self,
        *,
        core: Any,
        namespace: str,
        name: str,
        watch_factory: Callable[[], Any],
    ) -> None:
        self._core = core
        self._namespace = namespace
        self._name = name
        self._watch_factory = watch_factory

    @property
    def name(self) -> str:
        return self._name

    @property
    def pid(self) -> int:
        # Same convention as DockerContainerHandle: a stable positive
        # tracking id derived from the name (pods have no host pid).
        return hash(self._name) & 0x7FFFFFFF

    async def wait(self) -> int:
        """Wait for the pod to finish; return its container exit code.

        Watch-based with two safety nets (docs/21 §10 risk "K8s watch
        drops"): every loop iteration starts from a fresh
        ``read_namespaced_pod`` (the periodic-read fallback — it alone
        guarantees progress even if every watch breaks), and the watch
        resumes from that read's resourceVersion so no event between read
        and watch start is missed. HTTP 410 Gone (resourceVersion expired)
        re-enters the loop for a fresh read instead of failing.

        Returns -1 when the pod disappears underneath us (deleted by an
        operator / another path) — the exit code is unrecoverable then.
        """
        from kubernetes_asyncio.client.exceptions import ApiException

        while True:
            try:
                pod = await self._core.read_namespaced_pod(self._name, self._namespace)
            except ApiException as exc:
                if _api_status(exc) == 404:
                    logger.warning(
                        "Pod vanished while awaited — exit code unrecoverable",
                        pod=self._name,
                    )
                    return -1
                raise
            code = _terminal_exit_code(pod)
            if code is not None:
                return code

            resource_version = pod.metadata.resource_version
            try:
                stream = self._watch_factory().stream(
                    self._core.list_namespaced_pod,
                    namespace=self._namespace,
                    field_selector=f"metadata.name={self._name}",
                    resource_version=resource_version,
                    timeout_seconds=_WATCH_TIMEOUT_S,
                )
                async with stream:
                    async for event in stream:
                        if event.get("type") == "DELETED":
                            logger.warning(
                                "Pod deleted while awaited — exit code unrecoverable",
                                pod=self._name,
                            )
                            return -1
                        code = _terminal_exit_code(event.get("object"))
                        if code is not None:
                            return code
            except ApiException as exc:
                # 410 Gone: our resourceVersion fell out of the server's
                # history window. The next loop iteration re-reads for a
                # fresh one — never fatal.
                if _api_status(exc) != 410:
                    raise
                logger.debug(
                    "Watch resourceVersion expired — re-reading pod",
                    pod=self._name,
                )
            # Stream ended (server-side timeout / connection drop) without
            # a terminal event: fall through to the fresh read above.
            await asyncio.sleep(_WAIT_RETRY_DELAY_S)

    async def stop(self, timeout: int = 1) -> None:
        await _delete_pod(self._core, self._name, self._namespace, grace_period=timeout)

    async def read_stderr(self) -> AsyncIterator[bytes]:
        """Stream the pod log as the diagnostic stream.

        KNOWN SEMANTIC DIFFERENCE (docs/21 §8): Kubernetes merges stdout
        and stderr into a single pod log — there is no stderr-only read.
        Protocol output rides NATS, the log is diagnostics-only, so the
        contract is "diagnostic lines appear here", not "stderr only".

        A follow request against a not-yet-started container fails 400
        (ContainerCreating); retried briefly because run() returns before
        the kubelet pulls/starts the image.
        """
        from kubernetes_asyncio.client.exceptions import ApiException

        deadline = time.monotonic() + _LOG_RETRY_BUDGET_S
        while True:
            try:
                response = await self._core.read_namespaced_pod_log(
                    self._name,
                    self._namespace,
                    follow=True,
                    _preload_content=False,
                )
                break
            except ApiException as exc:
                if _api_status(exc) == 400 and time.monotonic() < deadline:
                    await asyncio.sleep(_LOG_RETRY_INTERVAL_S)
                    continue
                raise
        try:
            async for line in response.content:
                yield line if isinstance(line, bytes) else str(line).encode()
        finally:
            response.close()


async def _delete_pod(core: Any, name: str, namespace: str, *, grace_period: int) -> None:
    """Delete a pod, tolerating its absence."""
    from kubernetes_asyncio.client.exceptions import ApiException

    try:
        await core.delete_namespaced_pod(name, namespace, grace_period_seconds=grace_period)
    except ApiException as exc:
        if _api_status(exc) != 404:
            raise


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class K8sRuntime:
    """Kubernetes implementation of the ContainerRuntime protocol."""

    def __init__(self) -> None:
        try:
            from kubernetes_asyncio import client, config, watch
        except ImportError as exc:
            raise RuntimeError(_INSTALL_HINT) from exc
        self._client_mod = client
        self._config_mod = config
        self._watch_mod = watch
        self._api_client: Any = None
        self._core: Any = None
        self._networking: Any = None
        self._authorization: Any = None

    @property
    def name(self) -> str:
        return "k8s"

    # -- lifecycle ------------------------------------------------------

    async def ensure_available(self) -> None:
        """Load cluster config and check the API server is reachable."""
        try:
            # In-cluster first (production: the orchestrator Deployment
            # carries a ServiceAccount); kubeconfig as the dev fallback
            # (kind from the host).
            try:
                # kubernetes_asyncio.config.load_incluster_config is an
                # untyped function (no stub for this entry point); the call
                # is the documented in-cluster bootstrap path.
                self._config_mod.load_incluster_config()  # type: ignore[no-untyped-call]
            except self._config_mod.ConfigException:
                await self._config_mod.load_kube_config()
        except Exception as exc:
            msg = (
                "Could not load a Kubernetes client configuration "
                f"(in-cluster and kubeconfig both failed): {exc}. "
                "Run the orchestrator in-cluster with a ServiceAccount, or "
                "provide a reachable kubeconfig (KUBECONFIG / ~/.kube/config)."
            )
            raise RuntimeError(msg) from exc

        self._api_client = self._client_mod.ApiClient()
        try:
            version = await self._client_mod.VersionApi(self._api_client).get_code()
        except Exception as exc:
            await self._api_client.close()
            self._api_client = None
            msg = (
                "Kubernetes API server is not reachable. Agents cannot run "
                f"without it: {exc}. Check cluster/kubeconfig connectivity "
                "and credentials, then restart RoleMesh."
            )
            raise RuntimeError(msg) from exc
        self._core = self._client_mod.CoreV1Api(self._api_client)
        self._networking = self._client_mod.NetworkingV1Api(self._api_client)
        self._authorization = self._client_mod.AuthorizationV1Api(self._api_client)
        logger.debug(
            "Kubernetes runtime available",
            server_version=getattr(version, "git_version", "?"),
        )

    def _ensure_core(self) -> Any:
        if self._core is None:
            msg = "K8sRuntime.ensure_available() must be called first"
            raise RuntimeError(msg)
        return self._core

    async def close(self) -> None:
        if self._api_client is not None:
            await self._api_client.close()
            self._api_client = None
            self._core = None
            self._networking = None
            self._authorization = None

    # -- sandbox lifecycle ----------------------------------------------

    async def run(self, spec: ContainerSpec) -> K8sPodHandle:
        """Create the agent pod; same-name pod is replaced (delete first).

        Matches docker's ``create_or_replace`` semantics: delete any
        existing pod with the same name, wait for it to actually be gone
        (pod deletion is asynchronous — create during termination races
        409), then create. A 409 on create (lost race with a concurrent
        creator or a slow finalizer) gets one delete-and-retry.
        """
        from kubernetes_asyncio.client.exceptions import ApiException

        core = self._ensure_core()
        from rolemesh.core.config import (
            DATA_DIR,
            ROLEMESH_K8S_DATA_PVC,
            ROLEMESH_K8S_IMAGE_PULL_POLICY,
            ROLEMESH_K8S_IMAGE_PULL_SECRET,
            ROLEMESH_K8S_NAMESPACE,
            ROLEMESH_K8S_RUNTIME_CLASS,
        )

        manifest = spec_to_pod_manifest(
            spec,
            namespace=ROLEMESH_K8S_NAMESPACE,
            data_dir=str(DATA_DIR),
            data_pvc=ROLEMESH_K8S_DATA_PVC,
            image_pull_secret=ROLEMESH_K8S_IMAGE_PULL_SECRET,
            image_pull_policy=ROLEMESH_K8S_IMAGE_PULL_POLICY,
            runtime_class=ROLEMESH_K8S_RUNTIME_CLASS,
        )

        await self._delete_and_wait_gone(spec.name, ROLEMESH_K8S_NAMESPACE)
        try:
            await core.create_namespaced_pod(namespace=ROLEMESH_K8S_NAMESPACE, body=manifest)
        except ApiException as exc:
            if _api_status(exc) != 409:
                raise
            logger.debug(
                "Pod name conflict on create — deleting and retrying once",
                pod=spec.name,
            )
            await self._delete_and_wait_gone(spec.name, ROLEMESH_K8S_NAMESPACE)
            await core.create_namespaced_pod(namespace=ROLEMESH_K8S_NAMESPACE, body=manifest)

        return K8sPodHandle(
            core=core,
            namespace=ROLEMESH_K8S_NAMESPACE,
            name=spec.name,
            watch_factory=self._watch_mod.Watch,
        )

    async def _delete_and_wait_gone(self, name: str, namespace: str) -> None:
        """Delete a same-name pod (if any) and wait until reads return 404."""
        from kubernetes_asyncio.client.exceptions import ApiException

        core = self._ensure_core()
        try:
            await core.delete_namespaced_pod(name, namespace, grace_period_seconds=0)
        except ApiException as exc:
            if _api_status(exc) == 404:
                return  # nothing to replace
            raise
        deadline = time.monotonic() + _REPLACE_WAIT_BUDGET_S
        while True:
            try:
                await core.read_namespaced_pod(name, namespace)
            except ApiException as exc:
                if _api_status(exc) == 404:
                    return
                raise
            if time.monotonic() >= deadline:
                msg = (
                    f"Pod {name!r} is stuck terminating (still readable "
                    f"{_REPLACE_WAIT_BUDGET_S:.0f}s after delete) — cannot "
                    "replace it. Inspect finalizers/kubelet on its node."
                )
                raise RuntimeError(msg)
            await asyncio.sleep(_REPLACE_WAIT_INTERVAL_S)

    async def stop(self, name: str, timeout: int = 1) -> None:
        # grace_period_seconds plays docker's stop-timeout role: SIGTERM,
        # then SIGKILL after the grace period.
        from rolemesh.core.config import ROLEMESH_K8S_NAMESPACE

        core = self._ensure_core()
        await _delete_pod(core, name, ROLEMESH_K8S_NAMESPACE, grace_period=timeout)

    async def cleanup_orphans(
        self,
        prefix: str,
        *,
        allowed_images: frozenset[str],
    ) -> list[str]:
        """Delete leftover agent pods; triple filter (docs/21 §3).

        INV-3 (cleanup-safety), K8s flavor: a pod is only reaped when ALL
        three hold —
          1. it carries the managed-by label (we stamped it at create);
          2. its name starts with ``prefix``;
          3. its container image is in ``allowed_images``.
        The label selector narrows server-side; name and image re-check
        client-side so an unrelated pod that merely happens to carry our
        label (copied manifest) is still protected by the image allowlist.
        """
        from rolemesh.core.config import ROLEMESH_K8S_NAMESPACE

        core = self._ensure_core()
        pods = await core.list_namespaced_pod(
            ROLEMESH_K8S_NAMESPACE,
            label_selector=f"{AGENT_MANAGED_BY_LABEL}={AGENT_MANAGED_BY_VALUE}",
        )
        normalized_whitelist = {_normalize_image_ref(i) for i in allowed_images}
        removed: list[str] = []
        for pod in pods.items:
            pod_name = str(pod.metadata.name)
            if not pod_name.startswith(prefix):
                continue
            containers = getattr(pod.spec, "containers", None) or []
            image = str(containers[0].image) if containers else ""
            if _normalize_image_ref(image) not in normalized_whitelist:
                continue
            await _delete_pod(core, pod_name, ROLEMESH_K8S_NAMESPACE, grace_period=0)
            removed.append(pod_name)
        if removed:
            logger.info("Deleted orphaned agent pods", count=len(removed), names=removed)
        return removed

    async def list_live(self, prefix: str) -> set[str]:
        """Names of RUNNING agent pods matching ``prefix`` (reaper liveness oracle).

        Filters by the managed-by label server-side, then keeps only pods whose
        name starts with ``prefix`` and whose ``status.phase == 'Running'`` —
        the K8s analogue of "the container process still exists". Read-only.
        """
        from rolemesh.core.config import ROLEMESH_K8S_NAMESPACE

        core = self._ensure_core()
        pods = await core.list_namespaced_pod(
            ROLEMESH_K8S_NAMESPACE,
            label_selector=f"{AGENT_MANAGED_BY_LABEL}={AGENT_MANAGED_BY_VALUE}",
        )
        live: set[str] = set()
        for pod in pods.items:
            pod_name = str(pod.metadata.name)
            if not pod_name.startswith(prefix):
                continue
            phase = str(getattr(getattr(pod, "status", None), "phase", ""))
            if phase == "Running":
                live.add(pod_name)
        return live

    # -- infrastructure verification (docs/21 §4.2 rev4) ------------------

    async def verify_infrastructure(self) -> None:
        """Verify the chart-declared infrastructure invariants.

        Invariants (in check order):
          (a) the four NetworkPolicies exist; the agent-* ones select the
              agent pod label, the component ones do NOT select agent pods;
          (b) the data PVC is Bound;
          (c) the gateway Service exists and its ClusterIP equals
              ``EGRESS_GATEWAY_DNS_IP`` (the value agents get pinned as
              their resolver);
          (d) the gateway answers healthz through that Service IP;
          (e) NATS is TCP-reachable at ``NATS_URL``;
          (f) RBAC self-check: pods create/delete/get/list/watch and
              pods/log get are allowed (SelfSubjectAccessReview).

        There is deliberately no separate "namespace exists" probe: that
        would need a cluster-scoped ``namespaces/get`` a namespaced Role
        cannot grant, which fights the pods-only RBAC the design calls
        for (docs/21 §10.2). A missing namespace surfaces as the first
        namespaced read (the NetworkPolicy check) returning 404 with
        chart-install guidance.

        (d)/(e) race the deployment cold start and retry within a budget;
        everything else is a static object check and fails immediately.
        The deny PROBE (an actual pod whose egress must fail) is NOT here:
        it lives in ``helm test`` and contract case T-NET (§4.2 — pod
        scheduling and image pull would drag every startup into tens of
        seconds).
        """
        from rolemesh.core.config import (
            CREDENTIAL_PROXY_PORT,
            EGRESS_GATEWAY_CONTAINER_NAME,
            EGRESS_GATEWAY_DNS_IP,
            NATS_URL,
            ROLEMESH_K8S_DATA_PVC,
            ROLEMESH_K8S_NAMESPACE,
        )

        self._ensure_core()
        namespace = ROLEMESH_K8S_NAMESPACE

        await self._check_network_policies(namespace)
        await self._check_pvc_bound(namespace, ROLEMESH_K8S_DATA_PVC)
        # The gateway Service shares its name with the docker-side
        # container (default "egress-gateway") — one config knob, both
        # backends.
        await self._check_gateway_service(
            namespace,
            service_name=EGRESS_GATEWAY_CONTAINER_NAME,
            expected_cluster_ip=EGRESS_GATEWAY_DNS_IP,
        )

        healthz_url = f"http://{EGRESS_GATEWAY_DNS_IP}:{CREDENTIAL_PROXY_PORT}/healthz"
        await _retry_within_budget(
            lambda: _check_http_healthz(healthz_url, hint=_HELM_HINT),
            what="egress gateway /healthz",
        )
        await _retry_within_budget(
            lambda: _check_tcp_reachable(NATS_URL, hint=_HELM_HINT),
            what="NATS reachability",
        )

        await self._check_rbac(namespace)

        logger.info(
            "Infrastructure verified",
            namespace=namespace,
            network_policies=REQUIRED_AGENT_NETWORK_POLICIES
            + REQUIRED_COMPONENT_NETWORK_POLICIES,
            data_pvc=ROLEMESH_K8S_DATA_PVC,
            gateway_cluster_ip=EGRESS_GATEWAY_DNS_IP,
        )

    @staticmethod
    def _selector_matches_agent_pods(policy: Any) -> bool:
        """Would this policy's podSelector select our agent pods?

        True when every matchLabels entry is satisfied by AGENT_POD_LABELS
        (an empty matchLabels selects ALL pods — including agents).
        matchExpressions narrow a selector further; when present we treat
        the selector as not provably matching (the component policies the
        chart actually ships use plain matchLabels).
        """
        selector = getattr(getattr(policy, "spec", None), "pod_selector", None)
        if selector is None:
            return True  # empty selector == all pods
        if getattr(selector, "match_expressions", None):
            return False
        match_labels: dict[str, str] = getattr(selector, "match_labels", None) or {}
        return all(AGENT_POD_LABELS.get(k) == v for k, v in match_labels.items())

    async def _check_network_policies(self, namespace: str) -> None:
        from kubernetes_asyncio.client.exceptions import ApiException

        for policy_name in (
            REQUIRED_AGENT_NETWORK_POLICIES + REQUIRED_COMPONENT_NETWORK_POLICIES
        ):
            try:
                policy = await self._networking.read_namespaced_network_policy(
                    policy_name, namespace
                )
            except ApiException as exc:
                if _api_status(exc) == 404:
                    msg = (
                        f"NetworkPolicy {policy_name!r} is missing in "
                        f"namespace {namespace!r} (or the namespace itself "
                        f"does not exist) — agent isolation is not in force. "
                        f"{_HELM_HINT}"
                    )
                    raise RuntimeError(msg) from exc
                raise

            matches_agents = self._selector_matches_agent_pods(policy)
            if policy_name in REQUIRED_AGENT_NETWORK_POLICIES:
                selector = getattr(getattr(policy, "spec", None), "pod_selector", None)
                match_labels = getattr(selector, "match_labels", None) or {}
                if match_labels.get(AGENT_ROLE_LABEL) != AGENT_ROLE_VALUE:
                    msg = (
                        f"NetworkPolicy {policy_name!r} exists but its "
                        f"podSelector does not select "
                        f"{AGENT_ROLE_LABEL}={AGENT_ROLE_VALUE} — agent pods "
                        f"(labeled {AGENT_POD_LABELS}) would not be covered "
                        f"by it. Align the chart's selector with the "
                        f"orchestrator's pod labels. {_HELM_HINT}"
                    )
                    raise RuntimeError(msg)
            elif matches_agents:
                # NetworkPolicies are additive: a component policy whose
                # selector also catches agent pods would grant them the
                # component's wider egress and defeat default-deny.
                msg = (
                    f"NetworkPolicy {policy_name!r} selects agent pods "
                    f"(labeled {AGENT_POD_LABELS}) — its allowances would "
                    "be ADDED to the agent policy set and defeat "
                    "default-deny. Narrow its podSelector to the component "
                    f"it protects. {_HELM_HINT}"
                )
                raise RuntimeError(msg)

    async def _check_pvc_bound(self, namespace: str, pvc_name: str) -> None:
        from kubernetes_asyncio.client.exceptions import ApiException

        try:
            pvc = await self._core.read_namespaced_persistent_volume_claim(
                pvc_name, namespace
            )
        except ApiException as exc:
            if _api_status(exc) == 404:
                msg = (
                    f"Data PVC {pvc_name!r} does not exist in namespace "
                    f"{namespace!r} — agent mounts have no storage. "
                    f"{_HELM_HINT}"
                )
                raise RuntimeError(msg) from exc
            raise
        phase = str(getattr(getattr(pvc, "status", None), "phase", ""))
        if phase != "Bound":
            msg = (
                f"Data PVC {pvc_name!r} is {phase or 'in an unknown phase'} "
                "(expected Bound). Check the StorageClass / provisioner "
                f"(docs/21 §7.2). {_HELM_HINT}"
            )
            raise RuntimeError(msg)

    async def _check_gateway_service(
        self, namespace: str, *, service_name: str, expected_cluster_ip: str
    ) -> None:
        from kubernetes_asyncio.client.exceptions import ApiException

        try:
            service = await self._core.read_namespaced_service(service_name, namespace)
        except ApiException as exc:
            if _api_status(exc) == 404:
                msg = (
                    f"Egress gateway Service {service_name!r} does not exist "
                    f"in namespace {namespace!r}. {_HELM_HINT}"
                )
                raise RuntimeError(msg) from exc
            raise
        actual_ip = str(getattr(getattr(service, "spec", None), "cluster_ip", "") or "")
        if actual_ip != expected_cluster_ip:
            msg = (
                f"Egress gateway Service ClusterIP mismatch: configured "
                f"EGRESS_GATEWAY_DNS_IP={expected_cluster_ip!r} but Service "
                f"{service_name!r} holds {actual_ip!r}. Agents get the "
                "configured value pinned as their DNS resolver, so a drift "
                "silently kills agent DNS. Set EGRESS_GATEWAY_DNS_IP to the "
                "Service's ClusterIP (or give the Service a static "
                f"ClusterIP in values). {_HELM_HINT}"
            )
            raise RuntimeError(msg)

    async def _check_rbac(self, namespace: str) -> None:
        """SelfSubjectAccessReview for every verb the runtime needs.

        pods: create/delete/get/watch per the design, plus list — both
        cleanup_orphans and wait()'s watch go through the list endpoint.
        pods/log: get — read_stderr.
        """
        checks: list[tuple[str, str, str]] = [
            ("create", "pods", ""),
            ("delete", "pods", ""),
            ("get", "pods", ""),
            ("list", "pods", ""),
            ("watch", "pods", ""),
            ("get", "pods", "log"),
        ]
        for verb, resource, subresource in checks:
            attrs: dict[str, Any] = {
                "namespace": namespace,
                "verb": verb,
                "resource": resource,
            }
            if subresource:
                attrs["subresource"] = subresource
            review = await self._authorization.create_self_subject_access_review(
                body={"spec": {"resourceAttributes": attrs}}
            )
            if not bool(getattr(getattr(review, "status", None), "allowed", False)):
                what = f"{resource}/{subresource}" if subresource else resource
                msg = (
                    f"RBAC self-check failed: the orchestrator's "
                    f"ServiceAccount may not {verb!r} {what} in namespace "
                    f"{namespace!r}. Agent sandboxes cannot be managed "
                    "without it — fix the chart's Role/RoleBinding. "
                    f"{_HELM_HINT}"
                )
                raise RuntimeError(msg)
