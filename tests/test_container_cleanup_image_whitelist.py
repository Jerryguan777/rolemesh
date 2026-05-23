"""INV-3 pinned test: ``cleanup_orphans`` deletes only containers that
match BOTH a name prefix AND an image whitelist.

Anti-mirror discipline: the test asserts the behavior we want from
the public contract, not the implementation path. We never read the
internals of the cleanup loop — we observe ``stop()`` invocations
and the returned list of removed names.

We mock aiodocker at the SDK boundary (the runtime's ``_client``)
because hitting a real dockerd would make CI flaky on machines
without it; the seam is explicit ("inject a fake docker client",
exactly as the SDK exposes).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from rolemesh.container.docker_runtime import (
    DockerRuntime,
    _normalize_image_ref,
)


def _container(name: str, image: str) -> MagicMock:
    """Return a fake container row mirroring the shape aiodocker hands
    back from ``containers.list`` — keys we depend on are ``Names`` and
    ``Image``.
    """
    c = MagicMock()
    c._container = {"Names": [f"/{name}"], "Image": image}
    return c


def _runtime_with_list(rows: list[Any]) -> tuple[DockerRuntime, MagicMock]:
    rt = DockerRuntime()
    mock_client = MagicMock()
    mock_client.containers = MagicMock()
    mock_client.containers.list = AsyncMock(return_value=rows)
    # Stop calls instantiate a wrapper via containers.container(name);
    # return a mock that supports stop/delete so the call resolves
    # without ever talking to dockerd.
    target = MagicMock()
    target.stop = AsyncMock()
    target.delete = AsyncMock()
    mock_client.containers.container = MagicMock(return_value=target)
    rt._client = mock_client
    return rt, target


# ---------------------------------------------------------------------------
# Three scenarios mandated by the session prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_in_whitelist_and_prefix_match_is_removed() -> None:
    rt, target = _runtime_with_list(
        [_container("rolemesh-job-abc", "rolemesh-agent:latest")]
    )
    removed = await rt.cleanup_orphans(
        "rolemesh-", allowed_images=frozenset({"rolemesh-agent:latest"})
    )
    assert removed == ["rolemesh-job-abc"]
    target.stop.assert_awaited()  # actually stopped


@pytest.mark.asyncio
async def test_image_not_in_whitelist_is_left_alone_even_with_prefix() -> None:
    # The exact scenario from the design doc: a user running their
    # own kindest/node container that happens to have a rolemesh-
    # like name must not be deleted.
    rt, target = _runtime_with_list(
        [_container("rolemesh-foreign", "kindest/node:v1.27.3")]
    )
    removed = await rt.cleanup_orphans(
        "rolemesh-", allowed_images=frozenset({"rolemesh-agent:latest"})
    )
    assert removed == []
    target.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_in_whitelist_but_no_prefix_match_is_left_alone() -> None:
    # The docker name filter is substring-based; the runtime must
    # re-check the prefix explicitly. Without that re-check, a
    # container named e.g. ``foo-rolemesh-bar`` could be killed.
    rt, target = _runtime_with_list(
        [_container("foo-rolemesh-bar", "rolemesh-agent:latest")]
    )
    removed = await rt.cleanup_orphans(
        "rolemesh-", allowed_images=frozenset({"rolemesh-agent:latest"})
    )
    assert removed == []
    target.stop.assert_not_awaited()


# ---------------------------------------------------------------------------
# Additional pin: image-ref normalization across docker.io variants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_qualified_image_matches_bare_whitelist() -> None:
    rt, _ = _runtime_with_list(
        [_container("rolemesh-1", "docker.io/library/rolemesh-agent:latest")]
    )
    removed = await rt.cleanup_orphans(
        "rolemesh-", allowed_images=frozenset({"rolemesh-agent:latest"})
    )
    assert removed == ["rolemesh-1"]


@pytest.mark.asyncio
async def test_bare_image_matches_registry_qualified_whitelist() -> None:
    rt, _ = _runtime_with_list(
        [_container("rolemesh-1", "rolemesh-agent:latest")]
    )
    removed = await rt.cleanup_orphans(
        "rolemesh-",
        allowed_images=frozenset({"docker.io/library/rolemesh-agent:latest"}),
    )
    assert removed == ["rolemesh-1"]


# ---------------------------------------------------------------------------
# Mutation-thinking: what if someone inverts the membership check?
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_list_only_whitelist_matches_are_removed() -> None:
    # If a future refactor inverts ``if image not in whitelist: skip``
    # to ``if image in whitelist: skip``, this batch shows the bug
    # straight away: only the agent runs in the removed list.
    rt, _ = _runtime_with_list(
        [
            _container("rolemesh-1", "rolemesh-agent:latest"),
            _container("rolemesh-2", "user-image:v1"),
            _container("rolemesh-3", "rolemesh-egress-gateway:latest"),
            _container("rolemesh-4", "alpine:3.18"),
        ]
    )
    removed = await rt.cleanup_orphans(
        "rolemesh-",
        allowed_images=frozenset(
            {"rolemesh-agent:latest", "rolemesh-egress-gateway:latest"}
        ),
    )
    assert sorted(removed) == ["rolemesh-1", "rolemesh-3"]


# ---------------------------------------------------------------------------
# Boundary: empty whitelist is a no-op (not "delete everything")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_whitelist_removes_nothing() -> None:
    rt, target = _runtime_with_list(
        [_container("rolemesh-1", "rolemesh-agent:latest")]
    )
    removed = await rt.cleanup_orphans("rolemesh-", allowed_images=frozenset())
    assert removed == []
    target.stop.assert_not_awaited()


# ---------------------------------------------------------------------------
# Direct pin on the normalizer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("rolemesh-agent:latest", "rolemesh-agent:latest"),
        ("docker.io/library/rolemesh-agent:latest", "rolemesh-agent:latest"),
        ("docker.io/myorg/img:1", "myorg/img:1"),
        ("index.docker.io/library/img:1", "img:1"),
        ("index.docker.io/org/img:1", "org/img:1"),
        ("ghcr.io/org/img:1", "ghcr.io/org/img:1"),  # other registries untouched
        ("", ""),
    ],
)
def test_normalize_image_ref(ref: str, expected: str) -> None:
    assert _normalize_image_ref(ref) == expected
