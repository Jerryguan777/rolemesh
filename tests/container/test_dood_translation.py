"""DooD path translation + loopback self-check (docs/21 §7.1, §11).

Spec under test (not the implementation):

* When ``ROLEMESH_HOST_DATA_DIR`` is set, every bind SOURCE under
  ``DATA_DIR`` that ``DockerRuntime.run()`` hands to the Docker API must
  be rewritten to ``ROLEMESH_HOST_DATA_DIR/<relpath>`` — the host dockerd
  resolves bind sources against the HOST filesystem, not the
  orchestrator container's. Readonly flags and container paths must
  survive translation untouched.
* Sources outside ``DATA_DIR`` pass through unchanged (dockerd
  interprets them host-side) with a prominent warning.
* When ``ROLEMESH_HOST_DATA_DIR`` is empty (host dev flow), nothing
  changes at all.
* ``verify_infrastructure`` must prove the translation is correct at
  startup via a probe container that reads a sentinel back through the
  translated path, and must refuse to start on a mismatch. This is the
  single allowed exception to "verify never spawns containers".

Mock boundary: aiodocker only. The probe fakes simulate dockerd's actual
DooD semantics: bind sources are resolved against a "host" directory
tree, and a missing source behaves like dockerd — an empty directory
appears and the read fails (it does NOT error at mount time).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiodocker.exceptions
import pytest

from rolemesh.container import docker_runtime
from rolemesh.container.docker_runtime import (
    DockerRuntime,
    _translate_bind_source,
    _translate_mounts,
)
from rolemesh.container.runtime import ContainerSpec, VolumeMount
from rolemesh.core import config

DATA = "/app/data"
HOST = "/home/op/rolemesh/data"


# ---------------------------------------------------------------------------
# Pure translation function
# ---------------------------------------------------------------------------


def test_nested_path_under_data_dir_is_rewritten_to_host_relpath() -> None:
    # Asserting the exact result catches both a reversed relative_to()
    # and a translation that returns the input unchanged.
    out = _translate_bind_source(
        f"{DATA}/tenants/t1/sessions", data_dir=DATA, host_data_dir=HOST
    )
    assert out == f"{HOST}/tenants/t1/sessions"


def test_data_dir_root_itself_maps_to_host_data_dir() -> None:
    assert _translate_bind_source(DATA, data_dir=DATA, host_data_dir=HOST) == HOST


def test_path_outside_data_dir_passes_through_unchanged() -> None:
    out = _translate_bind_source(
        "/home/op/projects/x", data_dir=DATA, host_data_dir=HOST
    )
    assert out == "/home/op/projects/x"


def test_sibling_with_data_dir_string_prefix_is_not_translated() -> None:
    # /app/database shares the string prefix "/app/data" but is NOT
    # under it — naive startswith() translation would corrupt it.
    out = _translate_bind_source(
        "/app/database/x", data_dir=DATA, host_data_dir=HOST
    )
    assert out == "/app/database/x"


def test_empty_host_data_dir_disables_translation() -> None:
    path = f"{DATA}/tenants/t1"
    assert _translate_bind_source(path, data_dir=DATA, host_data_dir="") == path


def test_relative_input_path_passes_through() -> None:
    out = _translate_bind_source("tenants/t1", data_dir=DATA, host_data_dir=HOST)
    assert out == "tenants/t1"


def test_dotdot_escape_is_not_smuggled_under_host_data_dir() -> None:
    # Lexically /app/data/../secrets is NOT under DATA_DIR. Without
    # normalization, relative_to() happily yields "../secrets" and the
    # "translated" path escapes ROLEMESH_HOST_DATA_DIR.
    sneaky = f"{DATA}/../secrets/key"
    out = _translate_bind_source(sneaky, data_dir=DATA, host_data_dir=HOST)
    assert out == sneaky  # passthrough, untranslated
    assert not out.startswith(HOST)


def test_dotdot_that_stays_inside_data_dir_translates_normalized() -> None:
    out = _translate_bind_source(
        f"{DATA}/a/../b", data_dir=DATA, host_data_dir=HOST
    )
    assert out == f"{HOST}/b"


# ---------------------------------------------------------------------------
# _translate_mounts — flag preservation + warning
# ---------------------------------------------------------------------------


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, msg: str, **kw: Any) -> None:
        self.warnings.append((msg, kw))

    def info(self, msg: str, **kw: Any) -> None:  # pragma: no cover
        pass

    def debug(self, msg: str, **kw: Any) -> None:  # pragma: no cover
        pass


def test_translation_preserves_readonly_flag_and_container_path() -> None:
    mounts = [
        VolumeMount(f"{DATA}/skills", "/skills", readonly=True),
        VolumeMount(f"{DATA}/sessions", "/sessions", readonly=False),
    ]
    out = _translate_mounts(mounts, data_dir=DATA, host_data_dir=HOST)
    # A mutation that drops (or inverts) readonly during the rewrite
    # must fail here.
    assert [(m.host_path, m.container_path, m.readonly) for m in out] == [
        (f"{HOST}/skills", "/skills", True),
        (f"{HOST}/sessions", "/sessions", False),
    ]


def test_outside_mount_passes_through_with_prominent_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _RecordingLogger()
    monkeypatch.setattr(docker_runtime, "logger", rec)
    mounts = [VolumeMount("/home/op/projects", "/extra", readonly=True)]
    out = _translate_mounts(mounts, data_dir=DATA, host_data_dir=HOST)
    assert out[0].host_path == "/home/op/projects"
    assert len(rec.warnings) == 1
    msg, kw = rec.warnings[0]
    # The DooD caveat the design requires operators to see: dockerd
    # silently creates an empty root-owned dir for a missing source.
    assert "silently creates" in msg
    assert kw["host_path"] == "/home/op/projects"


def test_translate_mounts_noop_when_disabled() -> None:
    mounts = [VolumeMount(f"{DATA}/x", "/x", readonly=True)]
    assert _translate_mounts(mounts, data_dir=DATA, host_data_dir="") == mounts


# ---------------------------------------------------------------------------
# run() applies translation to the Docker API payload
# ---------------------------------------------------------------------------


class _FakeRunContainer:
    def __init__(self) -> None:
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def delete(self, force: bool = False) -> None:
        raise aiodocker.exceptions.DockerError(404, {"message": "gone"})


class _FakeRunContainers:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, Any]]] = []

    def container(self, name: str) -> _FakeRunContainer:
        return _FakeRunContainer()

    async def create_or_replace(
        self, name: str, config: dict[str, Any]
    ) -> _FakeRunContainer:
        self.created.append((name, config))
        return _FakeRunContainer()


class _FakeRunClient:
    def __init__(self) -> None:
        self.containers = _FakeRunContainers()


async def test_run_translates_data_dir_binds_and_keeps_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "DATA_DIR", Path(DATA))
    monkeypatch.setattr(config, "ROLEMESH_HOST_DATA_DIR", HOST)
    client = _FakeRunClient()
    rt = DockerRuntime()
    rt._client = client  # type: ignore[assignment]

    spec = ContainerSpec(
        name="rolemesh-agent-x",
        image="rolemesh-agent:latest",
        mounts=[
            VolumeMount(f"{DATA}/tenants/t1/skills", "/skills", readonly=True),
            VolumeMount(f"{DATA}/tenants/t1/sessions", "/sessions", readonly=False),
            VolumeMount("/home/op/projects", "/workspace/extra/p", readonly=True),
        ],
    )
    await rt.run(spec)

    (_, api_config), = client.containers.created
    assert api_config["HostConfig"]["Binds"] == [
        f"{HOST}/tenants/t1/skills:/skills:ro",
        f"{HOST}/tenants/t1/sessions:/sessions:rw",
        "/home/op/projects:/workspace/extra/p:ro",
    ]


async def test_run_untouched_when_host_data_dir_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "DATA_DIR", Path(DATA))
    monkeypatch.setattr(config, "ROLEMESH_HOST_DATA_DIR", "")
    client = _FakeRunClient()
    rt = DockerRuntime()
    rt._client = client  # type: ignore[assignment]

    spec = ContainerSpec(
        name="rolemesh-agent-x",
        image="rolemesh-agent:latest",
        mounts=[VolumeMount(f"{DATA}/tenants/t1", "/t", readonly=False)],
    )
    await rt.run(spec)
    (_, api_config), = client.containers.created
    assert api_config["HostConfig"]["Binds"] == [f"{DATA}/tenants/t1:/t:rw"]


# ---------------------------------------------------------------------------
# Loopback self-check. The fakes model dockerd's real DooD semantics:
# the bind source is resolved against a "host" tree (host_root), and a
# missing source does NOT fail the mount — an empty directory appears
# and the read inside fails, exactly like dockerd's silent mkdir.
# ---------------------------------------------------------------------------


class _FakeProbeContainer:
    def __init__(self, api_config: dict[str, Any], host_root: Path, data_dir: Path) -> None:
        self._config = api_config
        self._host_root = host_root
        self._data_dir = data_dir
        self.deleted = False
        self._exit = -1
        self._out = ""

    async def start(self) -> None:
        pass

    async def wait(self) -> dict[str, Any]:
        bind = self._config["HostConfig"]["Binds"][0]
        src = bind.rsplit(":", 2)[0]
        # dockerd-view resolution: host_root is the real storage behind
        # DATA_DIR. A source under host_root maps onto the orchestrator-
        # written tree; anything else is a missing source -> silent
        # empty dir -> the probe's read fails.
        try:
            rel = Path(src).relative_to(self._host_root)
        except ValueError:
            self._exit = 1
            self._out = "cat: /dood-probe/sentinel: No such file or directory"
            return {"StatusCode": self._exit}
        sentinel = self._data_dir / rel / "sentinel"
        if sentinel.is_file():
            self._exit = 0
            self._out = sentinel.read_text(encoding="utf-8")
        else:
            self._exit = 1
            self._out = "cat: /dood-probe/sentinel: No such file or directory"
        return {"StatusCode": self._exit}

    async def log(self, stdout: bool = False, stderr: bool = False) -> list[str]:
        return [self._out]

    async def delete(self, force: bool = False) -> None:
        self.deleted = True


class _FakeProbeContainers:
    def __init__(self, host_root: Path, data_dir: Path, *, fail_create: bool = False) -> None:
        self._host_root = host_root
        self._data_dir = data_dir
        self._fail_create = fail_create
        self.spawned: list[_FakeProbeContainer] = []

    async def create_or_replace(
        self, name: str, config: dict[str, Any]
    ) -> _FakeProbeContainer:
        if self._fail_create:
            raise aiodocker.exceptions.DockerError(
                404, {"message": "No such image"}
            )
        c = _FakeProbeContainer(config, self._host_root, self._data_dir)
        self.spawned.append(c)
        return c


class _FakeProbeClient:
    def __init__(self, containers: _FakeProbeContainers) -> None:
        self.containers = containers


def _probe_runtime(containers: _FakeProbeContainers) -> DockerRuntime:
    rt = DockerRuntime()
    rt._client = _FakeProbeClient(containers)  # type: ignore[assignment]
    return rt


async def test_loopback_passes_when_translation_is_correct(tmp_path: Path) -> None:
    data_dir = tmp_path / "app-data"
    data_dir.mkdir()
    host_root = tmp_path / "host-data"  # dockerd-side name of data_dir

    containers = _FakeProbeContainers(host_root, data_dir)
    rt = _probe_runtime(containers)
    await rt._verify_dood_translation(
        data_dir=str(data_dir), host_data_dir=str(host_root)
    )

    assert len(containers.spawned) == 1
    probe = containers.spawned[0]
    # Cleanup contract: probe container removed, sentinel dir removed.
    assert probe.deleted
    assert list(data_dir.iterdir()) == []
    # The probe must have been given the TRANSLATED (host) path, not the
    # orchestrator-view path.
    bind_src = probe._config["HostConfig"]["Binds"][0].rsplit(":", 2)[0]
    assert bind_src.startswith(str(host_root))


async def test_loopback_fails_closed_on_misconfigured_host_dir(
    tmp_path: Path,
) -> None:
    # ROLEMESH_HOST_DATA_DIR points somewhere that is NOT the storage
    # behind DATA_DIR. dockerd would silently create an empty dir there;
    # the self-check must turn that silence into a startup refusal.
    data_dir = tmp_path / "app-data"
    data_dir.mkdir()
    real_host_root = tmp_path / "host-data"
    wrong_host_root = tmp_path / "wrong-host-data"

    containers = _FakeProbeContainers(real_host_root, data_dir)
    rt = _probe_runtime(containers)
    with pytest.raises(RuntimeError, match="ROLEMESH_HOST_DATA_DIR"):
        await rt._verify_dood_translation(
            data_dir=str(data_dir), host_data_dir=str(wrong_host_root)
        )

    # Cleanup must run on the failure path too.
    assert containers.spawned[0].deleted
    assert list(data_dir.iterdir()) == []


async def test_loopback_probe_spawn_failure_is_wrapped_and_cleaned(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "app-data"
    data_dir.mkdir()
    containers = _FakeProbeContainers(
        tmp_path / "host-data", data_dir, fail_create=True
    )
    rt = _probe_runtime(containers)
    with pytest.raises(RuntimeError, match="probe"):
        await rt._verify_dood_translation(
            data_dir=str(data_dir), host_data_dir=str(tmp_path / "host-data")
        )
    assert list(data_dir.iterdir()) == []
