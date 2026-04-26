"""Tests for rolemesh.egress.launcher — gateway container orchestration.

The launcher is pure Docker-API plumbing; each happy-path assertion
targets a specific operational invariant we want to catch in review
rather than a free-form coverage number.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiodocker.exceptions
import pytest

from rolemesh.egress.launcher import (
    launch_egress_gateway,
    wait_for_gateway_ready,
)


def _docker_error(status: int, reason: str = "") -> aiodocker.exceptions.DockerError:
    return aiodocker.exceptions.DockerError(status, {"message": reason})


def _make_gateway_container() -> MagicMock:
    """Mock container handle that looks like aiodocker's DockerContainer."""
    c = MagicMock()
    c._id = "gateway-abc123"
    c.start = AsyncMock()
    c.delete = AsyncMock()
    return c


def _make_client(
    *,
    has_image: bool = True,
    has_stale: bool = False,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build a client mock with the shape launch_egress_gateway drives."""
    stale_container = MagicMock()
    stale_container.delete = AsyncMock()

    egress_network_obj = MagicMock()
    egress_network_obj.connect = AsyncMock()

    client = MagicMock()
    client.images = MagicMock()
    if has_image:
        client.images.inspect = AsyncMock()
    else:
        client.images.inspect = AsyncMock(side_effect=_docker_error(404, "image missing"))
    client.containers = MagicMock()
    if has_stale:
        client.containers.container = MagicMock(return_value=stale_container)
    else:
        client.containers.container = MagicMock(side_effect=_docker_error(404, "no stale"))
    client.networks = MagicMock()
    client.networks.get = AsyncMock(return_value=egress_network_obj)
    return client, egress_network_obj, stale_container


class TestLaunchEgressGateway:
    async def test_raises_clearly_when_image_missing(self) -> None:
        """Clean error message with build instruction — catches a real
        operator mistake (forgetting to run build-egress-gateway.sh)."""
        client, _, _ = _make_client(has_image=False)

        with (
            patch(
                "rolemesh.egress.launcher._optional_env_bind",
                return_value=[],
            ),
            pytest.raises(RuntimeError, match="Egress gateway image not found"),
        ):
            await launch_egress_gateway(
                client,
                agent_network="rolemesh-agent-net",
                egress_network="rolemesh-egress-net",
                image="rolemesh-egress-gateway:latest",
            )
        # No container should have been created / deleted.
        client.containers.container.assert_not_called()

    async def test_removes_stale_gateway_before_create(self) -> None:
        """Idempotency: re-running the launcher after an orchestrator
        crash must not fail with 'container already exists'."""
        client, _egress_network_obj, stale = _make_client(has_stale=True)
        container = _make_gateway_container()
        client.containers.create_or_replace = AsyncMock(return_value=container)

        with patch("rolemesh.egress.launcher._optional_env_bind", return_value=[]):
            await launch_egress_gateway(
                client,
                agent_network="rolemesh-agent-net",
                egress_network="rolemesh-egress-net",
            )

        stale.delete.assert_awaited_once_with(force=True)

    async def test_attaches_to_both_networks_before_start(self) -> None:
        """The gateway needs both networks live from its first moment.
        Attaching after start creates a window where egress fails."""
        client, egress_network_obj, _ = _make_client()
        container = _make_gateway_container()
        captured: dict[str, Any] = {}

        async def _capture_create(name: str, config: dict[str, Any]) -> MagicMock:
            captured["config"] = config
            return container

        client.containers.create_or_replace = AsyncMock(side_effect=_capture_create)

        # Track relative order: the second network must connect BEFORE
        # container.start(). Use a shared counter — simpler than nesting
        # mocks that track call_args_list with timestamps.
        call_order: list[str] = []
        egress_network_obj.connect = AsyncMock(
            side_effect=lambda _: call_order.append("connect")  # type: ignore[func-returns-value]
        )
        container.start = AsyncMock(
            side_effect=lambda: call_order.append("start")  # type: ignore[func-returns-value]
        )

        with patch("rolemesh.egress.launcher._optional_env_bind", return_value=[]):
            await launch_egress_gateway(
                client,
                agent_network="rolemesh-agent-net",
                egress_network="rolemesh-egress-net",
            )

        assert call_order == ["connect", "start"], (
            "egress network must be attached before the gateway starts"
        )
        # Primary network in HostConfig is the agent bridge (so the
        # container's primary DNS hostname resolves on the agent bridge).
        assert captured["config"]["HostConfig"]["NetworkMode"] == "rolemesh-agent-net"
        # Restart policy survives orchestrator restarts — a dead gateway
        # causes a full-tenant outage, so Docker should auto-recover.
        assert captured["config"]["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"

    async def test_rolls_back_container_on_egress_attach_failure(self) -> None:
        """If the egress bridge attach fails, the partially-started
        container is a stale liability; remove it so the next launcher
        run doesn't trip on it."""
        client, egress_network_obj, _ = _make_client()
        container = _make_gateway_container()
        client.containers.create_or_replace = AsyncMock(return_value=container)
        egress_network_obj.connect = AsyncMock(
            side_effect=_docker_error(409, "already connected")
        )

        with (
            patch("rolemesh.egress.launcher._optional_env_bind", return_value=[]),
            pytest.raises(aiodocker.exceptions.DockerError),
        ):
            await launch_egress_gateway(
                client,
                agent_network="rolemesh-agent-net",
                egress_network="rolemesh-egress-net",
            )

        container.delete.assert_awaited_once_with(force=True)
        container.start.assert_not_called()


class TestWaitForGatewayReady:
    async def test_succeeds_when_probe_eventually_passes(self) -> None:
        """Cold-start path: first few probes fail (listener binding),
        then one succeeds. Exercised with a side_effect list."""
        calls: list[int] = []

        async def _probe_mock(*args: Any, **kwargs: Any) -> None:
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("probe failed")

        with patch(
            "rolemesh.egress.launcher.verify_egress_gateway_reachable",
            side_effect=_probe_mock,
        ):
            await wait_for_gateway_ready(
                MagicMock(),
                agent_network="rolemesh-agent-net",
                attempts=5,
                interval_s=0.0,  # no real sleep in the test
            )

        assert len(calls) == 3

    async def test_raises_when_attempts_exhausted(self) -> None:
        """When the gateway never comes up, we raise with the most
        recent probe error in the chain — preserves diagnostic detail."""
        async def _always_fail(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("permanent probe failure")

        with (
            patch(
                "rolemesh.egress.launcher.verify_egress_gateway_reachable",
                side_effect=_always_fail,
            ),
            pytest.raises(RuntimeError, match="did not become ready after"),
        ):
            await wait_for_gateway_ready(
                MagicMock(),
                agent_network="rolemesh-agent-net",
                attempts=3,
                interval_s=0.0,
            )


# Note: ``rewrite_loopback_to_host_gateway`` was promoted to
# rolemesh.container.runtime in the Bug 5 fix; its tests live in
# tests/container/test_runtime.py. The launcher only references the
# helper at one site (NATS_URL injection) and the integration is
# exercised by tests/egress/integration.


# ---------------------------------------------------------------------------
# _gateway_env — base-URL loopback rewrite + token pass-through
# ---------------------------------------------------------------------------


class TestGatewayEnvBaseUrlRewrite:
    """Bug-5 family: every URL forwarded into the gateway container's
    Env block must go through ``rewrite_loopback_to_host_gateway``,
    because container-internal ``localhost`` is the container's own
    loopback. Tokens forwarded alongside must NOT be touched — string
    ``replace`` on a secret could silently corrupt it.
    """

    def _env_dict(self, env_pairs: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for pair in env_pairs:
            k, _, v = pair.partition("=")
            out[k] = v
        return out

    def test_anthropic_base_url_localhost_is_rewritten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Direct regression for the bug: operator pointed Anthropic
        # at a local proxy; without rewrite, the gateway dials its
        # own loopback and the entire Anthropic path 503s.
        from rolemesh.egress.launcher import _gateway_env

        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:11434")
        env = self._env_dict(_gateway_env())
        assert env["ANTHROPIC_BASE_URL"] == "http://host.docker.internal:11434"

    def test_openai_base_url_localhost_is_rewritten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same bug class, different forwarder. Must apply to every
        # *_BASE_URL key, not just Anthropic.
        from rolemesh.egress.launcher import _gateway_env

        monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:8080/v1")
        env = self._env_dict(_gateway_env())
        assert env["OPENAI_BASE_URL"] == "http://host.docker.internal:8080/v1"

    def test_google_base_url_localhost_is_rewritten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from rolemesh.egress.launcher import _gateway_env

        monkeypatch.setenv("GOOGLE_BASE_URL", "http://localhost:9000")
        env = self._env_dict(_gateway_env())
        assert env["GOOGLE_BASE_URL"] == "http://host.docker.internal:9000"

    def test_remote_base_url_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Negative case: production URLs (api.anthropic.com etc.)
        # must pass through unchanged.
        from rolemesh.egress.launcher import _gateway_env

        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        env = self._env_dict(_gateway_env())
        assert env["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"

    def test_token_with_localhost_substring_is_NOT_rewritten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Tokens are forwarded verbatim — the rewrite path is opt-in
        # via _URL_FORWARDABLE_KEYS, NOT a blanket replace. This test
        # pins the asymmetry contract so a future "let's just rewrite
        # every value" refactor doesn't quietly mangle a secret.
        # (Concrete token strings shouldn't contain "://localhost:"
        # in practice; this test forces the case to lock the contract.)
        from rolemesh.egress.launcher import _gateway_env

        weird_token = "sk-prefix-://localhost:1234-suffix"
        monkeypatch.setenv("ANTHROPIC_API_KEY", weird_token)
        env = self._env_dict(_gateway_env())
        assert env["ANTHROPIC_API_KEY"] == weird_token

    def test_unset_base_url_is_not_emitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Empty string shouldn't pollute the container Env. The
        # registry on the gateway side falls back to the public URL
        # default when *_BASE_URL is absent.
        from rolemesh.egress.launcher import _gateway_env

        for key in (
            "ANTHROPIC_BASE_URL",
            "OPENAI_BASE_URL",
            "GOOGLE_BASE_URL",
        ):
            monkeypatch.delenv(key, raising=False)
        env = self._env_dict(_gateway_env())
        for key in (
            "ANTHROPIC_BASE_URL",
            "OPENAI_BASE_URL",
            "GOOGLE_BASE_URL",
        ):
            assert key not in env

    def test_nats_url_still_rewritten_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # NATS_URL has its own line of code (not in forwardable_keys
        # because it's required, not optional). Sanity-check the
        # refactor didn't break it.
        from rolemesh.egress.launcher import _gateway_env

        env = self._env_dict(_gateway_env())
        assert "NATS_URL" in env
        # Default is nats://localhost:4222 in dev → must be rewritten.
        if "localhost" in env["NATS_URL"] or "127.0.0.1" in env["NATS_URL"]:
            raise AssertionError(
                f"NATS_URL not rewritten: {env['NATS_URL']!r}"
            )


# ---------------------------------------------------------------------------
# _FORWARDABLE spec contract — single-source-of-truth structural tests
# ---------------------------------------------------------------------------


class TestForwardableSpec:
    """``_FORWARDABLE`` is the single source of truth for "what crosses
    the gateway env boundary AND which entries need loopback rewrite".
    These tests pin the structural contract so a future refactor can't
    silently drop the rewrite flag from a URL forwarder (re-introducing
    the Bug 5 family).
    """

    def test_every_base_url_key_is_marked_url(self) -> None:
        # Heuristic but load-bearing: any key matching ``*_BASE_URL``
        # MUST have ``is_url=True``. This catches the most common
        # drift mode — adding a new ``MISTRAL_BASE_URL`` forwarder
        # but forgetting to flip the flag.
        from rolemesh.egress.launcher import _FORWARDABLE

        offenders = [
            spec for spec in _FORWARDABLE
            if spec.key.endswith("_BASE_URL") and not spec.is_url
        ]
        assert offenders == [], (
            f"_FORWARDABLE entries match *_BASE_URL but is_url=False: "
            f"{[s.key for s in offenders]}. Loopback rewrite will not "
            f"fire for these — Bug 5 family will return."
        )

    def test_no_token_key_is_marked_url(self) -> None:
        # Inverse of the above: ``API_KEY`` / ``OAUTH_TOKEN`` /
        # ``AUTH_TOKEN`` shaped keys must NEVER carry ``is_url=True``,
        # because string.replace on a secret could corrupt it.
        from rolemesh.egress.launcher import _FORWARDABLE

        token_suffixes = ("_API_KEY", "_OAUTH_TOKEN", "_AUTH_TOKEN")
        offenders = [
            spec for spec in _FORWARDABLE
            if any(spec.key.endswith(s) for s in token_suffixes)
            and spec.is_url
        ]
        assert offenders == [], (
            f"_FORWARDABLE marks token-shaped keys as URLs: "
            f"{[s.key for s in offenders]}. Tokens must never go through "
            f"loopback rewrite — string.replace could corrupt the secret."
        )

    def test_keys_are_unique(self) -> None:
        # Cheap sanity: spec is a tuple, so duplicates wouldn't error
        # at construction. A duplicate would emit the env var twice in
        # the gateway container (last write wins) and obscure intent.
        from rolemesh.egress.launcher import _FORWARDABLE

        keys = [spec.key for spec in _FORWARDABLE]
        assert len(keys) == len(set(keys)), (
            f"_FORWARDABLE has duplicate keys: {keys}"
        )

    def test_known_url_forwarders_carry_is_url_true(self) -> None:
        # Direct positive coverage of the three known URL forwarders
        # at the time of this PR. New URL forwarders should be added
        # here as they ship.
        from rolemesh.egress.launcher import _FORWARDABLE

        url_keys = {spec.key for spec in _FORWARDABLE if spec.is_url}
        assert {
            "ANTHROPIC_BASE_URL",
            "OPENAI_BASE_URL",
            "GOOGLE_BASE_URL",
        }.issubset(url_keys)
