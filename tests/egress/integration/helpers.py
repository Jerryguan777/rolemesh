"""Shared helpers for the egress integration test suite.

All Docker interactions live here (not in individual test files) so the
topology fixture in conftest.py can be reused across tests and any
future refactor of "how we talk to Docker" lands in one place.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiodocker
import aiodocker.exceptions

PROBE_IMAGE = "rolemesh-agent:latest"
"""Container image used as the per-test probe.

Re-using the agent image (already built for the main project) saves a
build step — it has python3.12 + curl + git + its own pip packages.
The test overrides ENTRYPOINT to ``sleep infinity`` so agent_runner
doesn't try to talk to NATS on startup.
"""

GATEWAY_IMAGE = "rolemesh-egress-gateway:latest"
NATS_IMAGE = "nats:latest"
FAKE_UPSTREAM_IMAGE = "rolemesh-fake-upstream:test"


def rand_suffix() -> str:
    """Short random hex for per-test resource names. Avoids collisions
    when pytest-xdist runs tests in parallel or when a previous run
    didn't clean up.
    """
    return secrets.token_hex(4)


@dataclass
class ContainerHandle:
    """Tracks enough to stop/remove a container we launched."""

    name: str
    docker: aiodocker.Docker

    async def delete(self) -> None:
        with contextlib.suppress(aiodocker.exceptions.DockerError):
            c = self.docker.containers.container(self.name)
            await c.delete(force=True)

    async def exec_sh(
        self, cmd: str, *, timeout_s: float = 30.0
    ) -> tuple[int, str]:
        """Run a shell command inside the container and return (exit_code, combined_output).

        Combined stdout+stderr because most of our asserts want to grep
        the full output without caring which stream the line came from
        — keeps the test code readable.
        """
        c = self.docker.containers.container(self.name)
        exec_inst = await c.exec(
            ["sh", "-c", cmd],
            stdout=True,
            stderr=True,
            tty=False,
        )
        buf: list[bytes] = []
        stream = exec_inst.start(detach=False)
        async with stream as s:
            deadline = asyncio.get_event_loop().time() + timeout_s
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return 124, b"".join(buf).decode(errors="replace") + "\n[EXEC TIMEOUT]"
                try:
                    msg = await asyncio.wait_for(s.read_out(), timeout=remaining)
                except TimeoutError:
                    return 124, b"".join(buf).decode(errors="replace") + "\n[EXEC TIMEOUT]"
                if msg is None:
                    break
                buf.append(msg.data)
        info = await exec_inst.inspect()
        exit_code = int(info.get("ExitCode") or 0)
        return exit_code, b"".join(buf).decode(errors="replace")

    async def inspect(self) -> dict[str, Any]:
        c = self.docker.containers.container(self.name)
        return await c.show()

    async def ip_on(self, network_name: str) -> str:
        info = await self.inspect()
        net = info["NetworkSettings"]["Networks"].get(network_name, {}) or {}
        return str(net.get("IPAddress") or "")


@dataclass
class Topology:
    """Handle to the running egress integration topology."""

    docker: aiodocker.Docker
    agent_network: str
    egress_network: str
    gateway: ContainerHandle
    nats: ContainerHandle
    fake_upstream: ContainerHandle
    nats_url: str
    gateway_ip_on_agent_net: str
    fake_upstream_name: str
    containers_to_cleanup: list[ContainerHandle] = field(default_factory=list)

    async def spawn_probe(
        self,
        *,
        extra_env: dict[str, str] | None = None,
        dns: list[str] | None = None,
    ) -> ContainerHandle:
        """Spawn a probe container on the internal agent network.

        ``extra_env`` merges onto the baseline proxy env (``HTTP_PROXY``
        etc.); ``dns`` overrides the container resolver, defaulting to
        the gateway's authoritative resolver so DNS tests actually
        exercise the gateway path.
        """
        env: dict[str, str] = {
            "HTTP_PROXY": "http://egress-gateway:3128",
            "HTTPS_PROXY": "http://egress-gateway:3128",
            "NO_PROXY": "egress-gateway,localhost,127.0.0.1",
        }
        if extra_env:
            env.update(extra_env)

        dns_servers = dns if dns is not None else [self.gateway_ip_on_agent_net]

        name = f"rolemesh-probe-{rand_suffix()}"
        config: dict[str, Any] = {
            "Image": PROBE_IMAGE,
            "Entrypoint": ["sleep", "infinity"],
            "Env": [f"{k}={v}" for k, v in env.items()],
            "HostConfig": {
                "NetworkMode": self.agent_network,
                "AutoRemove": False,
                "Dns": list(dns_servers),
                # Match the production agent hardening so probe
                # behaviour mirrors what real agents see.
                "CapDrop": ["ALL"],
                "ReadonlyRootfs": False,
                "SecurityOpt": ["no-new-privileges:true"],
                "Tmpfs": {"/tmp": "rw,size=16m"},
            },
            "User": "0:0",  # keep probe runnable with `sh` — don't need agent UID
        }
        container = await self.docker.containers.create_or_replace(name=name, config=config)
        await container.start()
        handle = ContainerHandle(name=name, docker=self.docker)
        self.containers_to_cleanup.append(handle)
        return handle

    async def publish_lifecycle_started(self, probe: ContainerHandle, identity: dict[str, Any]) -> None:
        """Tell the gateway's identity resolver about a probe container.

        We are standing in for the orchestrator here — in production
        ContainerAgentExecutor publishes lifecycle events; in tests we
        do it ourselves with identity fields under our control.
        """
        import nats  # type: ignore[import-not-found]

        ip = await probe.ip_on(self.agent_network)
        assert ip, f"probe {probe.name} has no IP on {self.agent_network}"
        payload = {
            "event": "started",
            "container_name": probe.name,
            "ip": ip,
            **identity,
        }
        nc = await nats.connect(self.nats_url)
        try:
            await nc.publish(
                "orchestrator.agent.lifecycle",
                json.dumps(payload).encode("utf-8"),
            )
            await nc.flush()
        finally:
            await nc.close()
        # Give the gateway's subscriber a beat to consume.
        await asyncio.sleep(0.5)

    async def seed_rules_responder(self, rules: list[dict[str, Any]]) -> Any:
        """Stand up a responder for ``egress.rules.snapshot.request``.

        The orchestrator does this in production. In tests we publish
        our own canned rule list so the gateway's policy cache seed
        path is exercised without bringing postgres into the loop.
        Caller must keep the returned NATS connection alive (subscribe
        is a background callback) and close it at the end of the test.
        """
        import nats  # type: ignore[import-not-found]

        nc = await nats.connect(self.nats_url)

        async def _on_request(msg: Any) -> None:
            body = json.dumps({"rules": rules}).encode("utf-8")
            with contextlib.suppress(Exception):
                await msg.respond(body)

        await nc.subscribe("egress.rules.snapshot.request", cb=_on_request)
        return nc

    async def publish_rule_changed(self, action: str, rule: dict[str, Any]) -> None:
        """Mimic the webui admin CRUD rule.changed publish.

        Longer post-publish sleep (2s) than feels necessary because the
        gateway's in-process apply_event path is fast but NATS core
        delivery scheduling + the gateway's single asyncio loop can
        introduce small jitter; shorter sleeps were flaky under CI.
        """
        import nats  # type: ignore[import-not-found]

        payload = {"action": action, "rule_id": rule.get("id"), **rule}
        nc = await nats.connect(self.nats_url)
        try:
            await nc.publish("safety.rule.changed", json.dumps(payload).encode("utf-8"))
            await nc.flush()
        finally:
            await nc.close()
        await asyncio.sleep(2.0)

    async def capture_safety_events(self, *, duration_s: float) -> list[dict[str, Any]]:
        """Subscribe to ``agent.*.safety_events`` for a window.

        Returns the decoded payloads. Used by audit-row assertions —
        avoids plumbing postgres into the integration harness just to
        read back audit rows.
        """
        import nats  # type: ignore[import-not-found]

        captured: list[dict[str, Any]] = []
        nc = await nats.connect(self.nats_url)

        async def _on_event(msg: Any) -> None:
            with contextlib.suppress(Exception):
                captured.append(json.loads(msg.data))

        await nc.subscribe("agent.*.safety_events", cb=_on_event)
        try:
            await asyncio.sleep(duration_s)
        finally:
            await nc.close()
        return captured

    async def stop_gateway(self) -> None:
        c = self.docker.containers.container(self.gateway.name)
        with contextlib.suppress(aiodocker.exceptions.DockerError):
            await c.stop(t=1)

    async def cleanup(self) -> None:
        for handle in self.containers_to_cleanup:
            with contextlib.suppress(Exception):
                await handle.delete()
        for handle in (self.gateway, self.fake_upstream, self.nats):
            with contextlib.suppress(Exception):
                await handle.delete()
        for net in (self.agent_network, self.egress_network):
            with contextlib.suppress(aiodocker.exceptions.DockerError):
                network = await self.docker.networks.get(net)
                await network.delete()


_LOCAL_ONLY_IMAGES = {GATEWAY_IMAGE, "rolemesh-fake-upstream:test", PROBE_IMAGE}


async def ensure_image_pulled(docker: aiodocker.Docker, image: str) -> None:
    """Pull ``image`` if missing; no-op if present locally.

    Locally-built images (gateway, fake-upstream, agent) raise
    RuntimeError with a build-instruction hint instead of trying a
    registry pull that would fail with a cryptic auth error.
    """
    try:
        await docker.images.inspect(image)
        return
    except aiodocker.exceptions.DockerError:
        pass
    if image in _LOCAL_ONLY_IMAGES:
        raise RuntimeError(
            f"Image {image} is missing locally. Build it first:\n"
            "  rolemesh-agent:latest           → ./container/build.sh\n"
            "  rolemesh-egress-gateway:latest  → ./container/build-egress-gateway.sh\n"
            "  rolemesh-fake-upstream:test     → "
            "docker build -t rolemesh-fake-upstream:test "
            "-f tests/egress/integration/Dockerfile.fake_upstream tests/egress/integration/"
        )
    await docker.images.pull(image)


async def wait_for_http_ok(
    docker: aiodocker.Docker,
    *,
    network: str,
    url: str,
    attempts: int = 30,
    interval_s: float = 0.5,
) -> None:
    """Poll ``url`` from a throwaway probe on ``network`` until 200."""
    name = f"rolemesh-healthprobe-{rand_suffix()}"
    cmd = [
        "sh",
        "-c",
        (
            f"for i in $(seq 1 {attempts}); do "
            f"  python -c \"import urllib.request; urllib.request.urlopen('{url}', timeout=2)\" "
            f"  && exit 0 || sleep {interval_s}; "
            "done; exit 1"
        ),
    ]
    config = {
        "Image": PROBE_IMAGE,
        # Override agent_runner's ENTRYPOINT so this short-lived probe
        # doesn't go hunting for NATS/JOB_ID at startup.
        "Entrypoint": ["sh"],
        "Cmd": ["-c", cmd[-1]],
        "HostConfig": {"NetworkMode": network, "AutoRemove": False},
    }
    c = await docker.containers.create_or_replace(name=name, config=config)
    try:
        await c.start()
        result = await c.wait()
        if int(result.get("StatusCode", -1)) != 0:
            logs = await c.log(stdout=True, stderr=True)
            raise RuntimeError(f"{url} never became healthy. Logs: {''.join(logs)[-500:]}")
    finally:
        with contextlib.suppress(aiodocker.exceptions.DockerError):
            await c.delete(force=True)


FAKE_UPSTREAM_SCRIPT = Path(__file__).parent / "fake_upstream.py"
