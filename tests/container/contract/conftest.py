"""Container-runtime contract tests (docs/21-container-runtime-decoupling §9).

This suite is the executable definition of the ContainerRuntime contract
(design principle: "Contract tests are the executable definition of the
contract"). One parameterized suite runs against every runtime backend;
test bodies contain ZERO ``if runtime == ...`` branches — every
runtime-specific detail lives in the fixtures/helpers of this conftest.

Run it against the live docker deployment:

    uv run pytest tests/container/contract/ -m integration --runtime=docker

Preconditions (the tests do NOT build infrastructure — that is itself a
test of the "declarative infrastructure" principle, docs/21 §9):

  * docker mode: ``docker compose --env-file .env -f
    deploy/compose/compose.yaml up -d`` is running and the agent image
    (CONTAINER_IMAGE, default rolemesh-agent:latest) is built.
  * k8s mode: not implemented yet — ``get_runtime("k8s")`` raises
    NotImplementedError and the whole directory skips. P2 (docs/21 §12,
    step S2: K8sRuntime + verify-k8s + kind) unlocks it.

The session fixture verifies the deployment layer ONCE via the real
``runtime.verify_infrastructure()``; if that fails the run aborts with
compose-up guidance instead of producing a wall of misleading failures.

In-container observation convention: tests run a python one-liner via
``spec.entrypoint`` (the agent image ships python; ContainerSpec has no
separate command field) and the script reports through STDERR — the one
stream ``ContainerHandle.read_stderr`` promises on every runtime
(docs/21 §8: K8s merges stdout/stderr; stderr-only keeps one contract).
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from rolemesh.container.runtime import ContainerSpec, get_runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

    from rolemesh.container.runtime import ContainerHandle, ContainerRuntime

# Name prefix for every container this suite spawns. Distinct from the
# production "rolemesh-<coworker>-" spawn names and from the compose
# service containers (egress-gateway, rolemesh-orchestrator, ...) so a
# crashed run never collides with — and cleanup can never reap — the
# development stack that is running next to the tests.
CONTRACT_PREFIX = "rolemesh-contract-"

_COMPOSE_UP_GUIDANCE = (
    "Contract tests verify a RUNNING deployment; they never build "
    "infrastructure themselves (docs/21 §9). Bring the stack up first:\n"
    "  docker compose --env-file .env -f deploy/compose/compose.yaml up -d\n"
    "and build the agent image (container/build.sh) if missing."
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--runtime",
        action="store",
        default="docker",
        choices=("docker", "k8s"),
        help="ContainerRuntime backend to run the contract suite against",
    )


@pytest.fixture(scope="session")
def runtime_name(request: pytest.FixtureRequest) -> str:
    try:
        return str(request.config.getoption("--runtime"))
    except ValueError:
        # Option not registered (suite invoked from a directory whose
        # conftest chain didn't load this file early enough) — default
        # matches the addoption default.
        return "docker"


@dataclass(frozen=True)
class Topology:
    """Deployment-layer facts the tests assert against.

    Built once per session from the same configuration the product code
    reads, so the suite and the orchestrator can never disagree about
    where the gateway/NATS live. Runtime-specific values (foreign image
    for the orphan-cleanup test, env keys the runtime itself injects)
    are decided HERE, never in test bodies.
    """

    agent_image: str
    agent_network: str
    dns_servers: tuple[str, ...]
    gateway_host: str
    forward_port: int
    reverse_port: int
    nats_host: str
    data_dir: Path
    agent_uid: int
    agent_gid: int
    # An image that exists in the deployment but is NOT an agent image —
    # used to prove cleanup_orphans's image allowlist refuses to reap
    # containers it did not launch.
    foreign_image: str
    # Env keys the container legitimately carries beyond spec.env:
    # the agent image's baked ENV layer plus what the runtime itself
    # injects (docker: HOSTNAME + HOME).
    injected_env_keys: frozenset[str] = field(default_factory=frozenset)
    # Production metadata-blackhole /etc/hosts entries (docs/21 §3).
    metadata_extra_hosts: dict[str, str] = field(default_factory=dict)


# Agent image ENV layer baked by container/Dockerfile + the python base
# image. GPG_KEY is the python release-signing PUBLIC key fingerprint —
# not a secret despite the name. Shared by both runtime topologies: the
# image is identical, only the runtime-injected extras differ.
_AGENT_IMAGE_ENV_KEYS: frozenset[str] = frozenset({
    "PATH",
    "LANG",
    "LC_ALL",
    "PYTHONPATH",
    "PYTHONUNBUFFERED",
    "PYTHON_VERSION",
    "PYTHON_SHA256",
    "GPG_KEY",
})

# Production metadata blackhole (runner._METADATA_BLACKHOLE). compute_egress_
# routing injects it identically on both runtimes — on docker as ExtraHosts,
# on k8s as pod hostAliases — so the agent-visible /etc/hosts content (and
# this Topology field) is the same. Literal, not an import, so the suite
# FAILS if the product constant drifts.
_METADATA_EXTRA_HOSTS: dict[str, str] = {
    "metadata.google.internal": "127.0.0.1",
    "169.254.169.254": "127.0.0.1",
}


def _build_topology(runtime_name: str) -> Topology:
    from rolemesh.core.config import (
        CONTAINER_IMAGE,
        CONTAINER_NETWORK_NAME,
        CREDENTIAL_PROXY_PORT,
        DATA_DIR,
        EGRESS_GATEWAY_CONTAINER_NAME,
        EGRESS_GATEWAY_DNS_IP,
        EGRESS_GATEWAY_FORWARD_PORT,
    )

    # Fields shared verbatim by both backends: the agent image, the gateway
    # DNS resolver IP (compose fixed IP / Service ClusterIP — one config
    # knob), the gateway service name (compose container / k8s Service —
    # one config knob), and the proxy ports. The product reads the same
    # config on both runtimes, so the suite and the orchestrator cannot
    # disagree about where the gateway/NATS live.
    common = {
        "agent_image": CONTAINER_IMAGE,
        "dns_servers": (EGRESS_GATEWAY_DNS_IP,),
        "gateway_host": EGRESS_GATEWAY_CONTAINER_NAME,
        "forward_port": EGRESS_GATEWAY_FORWARD_PORT,
        "reverse_port": CREDENTIAL_PROXY_PORT,
        "data_dir": DATA_DIR,
        # The unprivileged user baked into the agent image
        # (container/Dockerfile `useradd -u 1000`; k8s securityContext
        # runAsUser 1000). Deliberately a literal, not an import of
        # runner.AGENT_UID: the suite asserts the published contract and
        # must FAIL — not silently follow — if the product constant drifts.
        "agent_uid": 1000,
        "agent_gid": 1000,
        # nats is reachable by the bare name `nats` on BOTH runtimes:
        # compose names the service `nats`; the Helm chart exposes a
        # Service named literally `nats` (not release-scoped) for exactly
        # this parity. Agents resolve it through the gateway resolver.
        "nats_host": "nats",
        "metadata_extra_hosts": dict(_METADATA_EXTRA_HOSTS),
    }

    if runtime_name == "docker":
        return Topology(
            agent_network=CONTAINER_NETWORK_NAME,
            # The gateway image is guaranteed present once
            # verify_infrastructure passed (the gateway container is
            # running from it) — no registry pull needed.
            foreign_image="rolemesh-egress-gateway:latest",
            injected_env_keys=frozenset({
                # Runtime-injected by docker.
                "HOSTNAME",
                "HOME",
            })
            | _AGENT_IMAGE_ENV_KEYS,
            **common,  # type: ignore[arg-type]
        )

    if runtime_name == "k8s":
        return Topology(
            # K8s has no per-pod "agent network" — isolation is the chart's
            # NetworkPolicies selecting the agent role label, not a named
            # bridge. spec_to_pod_manifest ignores ContainerSpec.network_name
            # outright (k8s_runtime docstring). Empty string is the sentinel:
            # make_spec passes it through harmlessly and no k8s object keys
            # off it.
            agent_network="",
            # An image present in the cluster that is NOT an agent image, to
            # prove cleanup_orphans's allowlist refuses to reap foreign pods.
            # The egress-gateway Deployment runs from this image and the
            # chart `kind load`s it, so it is guaranteed pullable/present.
            foreign_image="rolemesh-egress-gateway:latest",
            # On k8s the agent pod sets enableServiceLinks:false (no
            # *_SERVICE_HOST/PORT injection) and automountServiceAccountToken
            # :false, so the only env beyond spec.env is the image's own ENV
            # layer. K8s does not auto-inject HOSTNAME/HOME the way docker
            # does; they are listed anyway because injected_env_keys is used
            # as an ALLOW set (extras - injected_env_keys must be empty), so
            # a superset can only relax the leak assertion, never mask a real
            # leak — every forbidden key/value is checked separately in
            # test_env_security regardless of this set.
            injected_env_keys=frozenset({
                "HOSTNAME",
                "HOME",
            })
            | _AGENT_IMAGE_ENV_KEYS,
            **common,  # type: ignore[arg-type]
        )

    msg = f"Topology for runtime {runtime_name!r} not defined"
    raise NotImplementedError(msg)


@pytest.fixture(scope="session")
def topology(runtime_name: str, _infra_verified: None) -> Topology:
    return _build_topology(runtime_name)


# ---------------------------------------------------------------------------
# Session gate: real runtime obtainable + deployment layer verified once.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _infra_verified(runtime_name: str) -> None:
    """Verify the deployment layer once per session, fail-closed.

    Sync fixture with its own short-lived event loop: aiodocker clients
    are bound to the loop they are created on, so the per-test async
    ``runtime`` fixture cannot be session-scoped — but the (idempotent,
    read-only) infrastructure verification only needs to run once.
    """
    try:
        probe = get_runtime(runtime_name)
    except NotImplementedError as exc:
        pytest.skip(
            f"{runtime_name} runtime not implemented yet ({exc}) — "
            "the P2 step (docs/21 §12 S2: K8sRuntime + verify-k8s + kind) "
            "unlocks this suite for --runtime=k8s"
        )

    # Mount-translation precondition. This suite runs OUTSIDE the
    # orchestrator container, so the bind sources it builds under
    # DATA_DIR are already host paths: DooD translation must be off
    # (ROLEMESH_HOST_DATA_DIR unset) or the identity mapping. Anything
    # else means the test process inherited a container-view config and
    # every T-FS assertion about host-visible content would be testing
    # the wrong directory.
    from rolemesh.core.config import DATA_DIR, ROLEMESH_HOST_DATA_DIR

    if ROLEMESH_HOST_DATA_DIR and Path(ROLEMESH_HOST_DATA_DIR) != DATA_DIR:
        pytest.exit(
            "Contract tests must run as a host process: "
            f"ROLEMESH_HOST_DATA_DIR={ROLEMESH_HOST_DATA_DIR!r} differs "
            f"from DATA_DIR={str(DATA_DIR)!r}, so DooD bind-source "
            "translation would rewrite the suite's host paths. Unset "
            "ROLEMESH_HOST_DATA_DIR in the test environment.",
            returncode=3,
        )

    async def _verify() -> None:
        await probe.ensure_available()
        try:
            await probe.verify_infrastructure()
        finally:
            await probe.close()

    try:
        asyncio.run(_verify())
    except RuntimeError as exc:
        pytest.exit(
            f"Deployment-layer verification failed: {exc}\n\n"
            f"{_COMPOSE_UP_GUIDANCE}",
            returncode=3,
        )


@pytest.fixture
async def runtime(
    runtime_name: str, _infra_verified: None
) -> AsyncIterator[ContainerRuntime]:
    """Fresh real runtime per test (clients are event-loop-bound)."""
    rt = get_runtime(runtime_name)
    await rt.ensure_available()
    try:
        yield rt
    finally:
        await rt.close()


# ---------------------------------------------------------------------------
# Spec construction + spawn helpers
# ---------------------------------------------------------------------------


def make_tmpfs(path: str, *, size_mb: int, uid: int, gid: int) -> dict[str, str]:
    """One tmpfs mount in the production shape (runner._default_tmpfs)."""
    return {path: f"rw,size={size_mb}m,uid={uid},gid={gid},mode=700"}


@pytest.fixture
def make_spec(topology: Topology) -> Callable[..., ContainerSpec]:
    """Factory for a minimal agent-shaped ContainerSpec.

    Defaults mirror what the production spawn path guarantees for every
    agent sandbox: agent image, isolated agent network, DNS pinned to
    the gateway resolver, and the ContainerSpec hardening defaults
    (cap_drop=ALL, readonly rootfs, pids limit) left untouched.
    """

    def _make(
        label: str,
        *,
        python: str | None = None,
        entrypoint: list[str] | None = None,
        **overrides: object,
    ) -> ContainerSpec:
        if python is not None:
            assert entrypoint is None, "pass either python= or entrypoint="
            entrypoint = ["python", "-c", python]
        name = f"{CONTRACT_PREFIX}{label}-{uuid.uuid4().hex[:8]}"
        kwargs: dict[str, object] = {
            "name": name,
            "image": topology.agent_image,
            "entrypoint": entrypoint,
            "network_name": topology.agent_network,
            "dns": list(topology.dns_servers),
        }
        kwargs.update(overrides)
        return ContainerSpec(**kwargs)  # type: ignore[arg-type]

    return _make


@pytest.fixture
async def spawn(
    runtime: ContainerRuntime,
) -> AsyncIterator[Callable[[ContainerSpec], Awaitable[ContainerHandle]]]:
    """Spawn a container and guarantee teardown, also on failure paths."""
    spawned: list[str] = []

    async def _spawn(spec: ContainerSpec) -> ContainerHandle:
        spawned.append(spec.name)
        return await runtime.run(spec)

    try:
        yield _spawn
    finally:
        for name in spawned:
            with contextlib.suppress(Exception):
                await runtime.stop(name)


async def collect_stderr(handle: ContainerHandle, *, timeout: float = 30.0) -> bytes:
    """Drain the handle's diagnostic stream with an overall timeout."""

    async def _drain() -> bytes:
        chunks: list[bytes] = []
        async for chunk in handle.read_stderr():
            chunks.append(chunk)
        return b"".join(chunks)

    return await asyncio.wait_for(_drain(), timeout=timeout)


@pytest.fixture
def run_python(
    make_spec: Callable[..., ContainerSpec],
    spawn: Callable[[ContainerSpec], Awaitable[ContainerHandle]],
) -> Callable[..., Awaitable[tuple[int, str]]]:
    """Run a python one-liner in a fresh agent sandbox.

    Returns ``(exit_code, stderr_text)``. The script must report via
    stderr (see module docstring for why stderr is the contract stream).
    """

    async def _run(
        label: str,
        code: str,
        *,
        timeout: float = 60.0,
        **spec_overrides: object,
    ) -> tuple[int, str]:
        spec = make_spec(label, python=code, **spec_overrides)
        handle = await spawn(spec)
        exit_code = await asyncio.wait_for(handle.wait(), timeout=timeout)
        stderr = await collect_stderr(handle)
        return exit_code, stderr.decode(errors="replace")

    return _run


# ---------------------------------------------------------------------------
# Per-area helpers that hide runtime-specific knobs from test bodies
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_verify(monkeypatch: pytest.MonkeyPatch, runtime_name: str) -> None:
    """Shrink verify_infrastructure's cold-start retry budget.

    The fail-closed T-VER cases deliberately break one invariant and
    expect verify to give up; waiting out the production ~60s budget
    per case is pointless. The budget knob is backend-specific, so the
    patch lives here rather than in the test bodies (design principle:
    zero runtime branches in tests).
    """
    if runtime_name == "docker":
        from rolemesh.container import docker_runtime

        monkeypatch.setattr(docker_runtime, "_VERIFY_RETRY_BUDGET_S", 0.3)
        monkeypatch.setattr(docker_runtime, "_VERIFY_RETRY_INTERVAL_S", 0.05)
    elif runtime_name == "k8s":
        # Same knobs, separate module: k8s_runtime duplicates (does not
        # import) the retry constants so each backend's budget is patchable
        # independently (see its _retry_within_budget docstring). Only the
        # racing checks (gateway healthz, NATS) honour the budget; static
        # object checks (policies, PVC, Service) fail immediately regardless.
        from rolemesh.container import k8s_runtime

        monkeypatch.setattr(k8s_runtime, "_VERIFY_RETRY_BUDGET_S", 0.3)
        monkeypatch.setattr(k8s_runtime, "_VERIFY_RETRY_INTERVAL_S", 0.05)
    else:  # pragma: no cover
        msg = f"fast_verify not wired for runtime {runtime_name!r}"
        raise NotImplementedError(msg)


@pytest.fixture
def host_mount_dir(topology: Topology, runtime_name: str) -> Iterator[Path]:
    """Host-side scratch directory under DATA_DIR for VolumeMount tests.

    DOCKER: lives under DATA_DIR because that subtree is the storage
    contract both runtimes translate (docs/21 §7.1); the session gate
    already asserted translation is off/identity for this host-run
    process. World-writable so the container's non-root uid can create
    files regardless of whether it matches the invoking user's uid. The
    test asserts the host can SEE what the container wrote (and vice
    versa) at this path — which holds because the bind source IS this
    host path.

    K8S: this fixture cannot work unchanged. The agent pod mounts the
    `rolemesh-data` PVC by subPath, not a host path; the suite runs
    OUTSIDE the cluster, so a path on the suite's local filesystem is NOT
    the bytes the agent pod sees. The host<->container visibility
    assertion T-FS relies on therefore has no host side here.

    Disposition (k8s, NOT yet runnable — flagged for the kind run, docs/21
    §7.2): the kind cluster maps `./data` into the node via the cluster
    config's extraMounts and a hostPath/local-path PV backs the PVC, so a
    path under DATA_DIR on the host DOES surface in the PVC. To make T-FS
    pass on kind the fixture must (1) create the scratch dir at the same
    relative subPath the agent pod will mount, and (2) translate between
    the suite's view of DATA_DIR and the node's mount point if they
    differ. Until that mapping is wired and validated on a live kind
    cluster, the k8s branch skips rather than silently testing the wrong
    directory — exactly the failure mode the docker session gate guards
    against. Wiring it is a kind-validation task (the user runs the
    cluster); the docker path is untouched.
    """
    if runtime_name == "k8s":
        pytest.skip(
            "host_mount_dir is not wired for k8s yet: the suite runs "
            "outside the cluster, so a local path is not the PVC bytes the "
            "agent pod sees. Needs the kind extraMounts<->PVC subPath "
            "mapping (docs/21 §7.2), validated on a live kind cluster."
        )
    base = topology.data_dir / "contract-tests" / uuid.uuid4().hex[:12]
    base.mkdir(parents=True, exist_ok=False)
    base.chmod(0o777)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)
        # Drop the shared parent when this was the last scratch dir so
        # repeated runs leave DATA_DIR exactly as they found it.
        with contextlib.suppress(OSError):
            base.parent.rmdir()
