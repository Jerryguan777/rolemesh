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

    if runtime_name != "docker":  # pragma: no cover — P2 fills this in
        msg = f"Topology for runtime {runtime_name!r} not defined yet"
        raise NotImplementedError(msg)

    return Topology(
        agent_image=CONTAINER_IMAGE,
        agent_network=CONTAINER_NETWORK_NAME,
        dns_servers=(EGRESS_GATEWAY_DNS_IP,),
        gateway_host=EGRESS_GATEWAY_CONTAINER_NAME,
        forward_port=EGRESS_GATEWAY_FORWARD_PORT,
        reverse_port=CREDENTIAL_PROXY_PORT,
        nats_host="nats",
        data_dir=DATA_DIR,
        # The unprivileged user baked into the agent image
        # (container/Dockerfile `useradd -u 1000`). Deliberately a
        # literal, not an import of runner.AGENT_UID: the suite asserts
        # the published contract, and must FAIL — not silently follow —
        # if the product constant drifts.
        agent_uid=1000,
        agent_gid=1000,
        # The gateway image is guaranteed present once
        # verify_infrastructure passed (the gateway container is running
        # from it) — no registry pull needed.
        foreign_image="rolemesh-egress-gateway:latest",
        injected_env_keys=frozenset({
            # Runtime-injected (docker).
            "HOSTNAME",
            "HOME",
            # Agent image ENV layer (container/Dockerfile + python base
            # image). GPG_KEY is the python release-signing PUBLIC key
            # fingerprint baked by the upstream python image — not a
            # secret despite the name.
            "PATH",
            "LANG",
            "LC_ALL",
            "PYTHONPATH",
            "PYTHONUNBUFFERED",
            "PYTHON_VERSION",
            "PYTHON_SHA256",
            "GPG_KEY",
        }),
        # Production metadata blackhole (runner._METADATA_BLACKHOLE).
        # Same literal-not-import rationale as agent_uid above.
        metadata_extra_hosts={
            "metadata.google.internal": "127.0.0.1",
            "169.254.169.254": "127.0.0.1",
        },
    )


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
    else:  # pragma: no cover — P2 adds the k8s knob here
        msg = f"fast_verify not wired for runtime {runtime_name!r}"
        raise NotImplementedError(msg)


@pytest.fixture
def host_mount_dir(topology: Topology) -> Iterator[Path]:
    """Host-side scratch directory under DATA_DIR for VolumeMount tests.

    Lives under DATA_DIR because that subtree is the storage contract
    both runtimes translate (docs/21 §7.1); the session gate already
    asserted translation is off/identity for this host-run process.
    World-writable so the container's non-root uid can create files
    regardless of whether it matches the invoking user's uid.
    """
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
