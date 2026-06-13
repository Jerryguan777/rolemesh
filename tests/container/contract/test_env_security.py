"""T-SEC: environment hygiene + process privileges (docs/21 §3
"Hardening" row and §6.2: EGRESS_TOKEN_SECRET is delivered to
orchestrator and gateway, NEVER to agents).
"""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING

import pytest

from .conftest import make_tmpfs

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .conftest import Topology

pytestmark = pytest.mark.integration

_DUMP_ENV = "import os, json, sys; sys.stderr.write(json.dumps(dict(os.environ)))"

# Values that must never be observable inside an agent sandbox under
# ANY key. Includes the platform secrets (docs/21 §6.2) and the
# orchestrator-side credentials the gateway resolves per-request.
_FORBIDDEN_ENV_KEYS = frozenset({
    "EGRESS_TOKEN_SECRET",
    "CREDENTIAL_VAULT_KEY",
    "WS_TICKET_SECRET",
    "ROLEMESH_TOKEN_SECRET",
    "EXTERNAL_JWT_SECRET",
    "ADMIN_BOOTSTRAP_TOKEN",
    "DATABASE_URL",
    "ADMIN_DATABASE_URL",
    "ROLEMESH_HOST_DATA_DIR",
    "DOCKER_GID",
})


async def test_container_env_is_exactly_spec_env_plus_declared_injections(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
    topology: Topology,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-SEC-1: the sandbox env is spec.env verbatim plus only the
    DECLARED injections (image ENV layer + runtime identity vars) —
    nothing inherited from the orchestrator process. EGRESS_TOKEN_SECRET
    in particular must never appear: an agent holding the signing secret
    could mint its own egress identities (docs/21 §6.2).

    The host process exports a sentinel EGRESS_TOKEN_SECRET before the
    spawn so 'never leaks' is exercised against a present secret, not a
    conveniently absent one.
    """
    monkeypatch.setenv("EGRESS_TOKEN_SECRET", "contract-sentinel-must-not-leak")

    spec_env = {"JOB_ID": "contract-job", "CONTRACT_MARKER": "t-sec-1"}
    exit_code, stderr = await run_python("sec-env", _DUMP_ENV, env=spec_env)
    assert exit_code == 0
    observed: dict[str, str] = json.loads(stderr)

    # spec.env arrives verbatim.
    for key, value in spec_env.items():
        assert observed.get(key) == value

    # No forbidden key under any name.
    leaked_keys = _FORBIDDEN_ENV_KEYS & observed.keys()
    assert not leaked_keys, f"platform secrets leaked into agent env: {leaked_keys}"

    # No forbidden VALUE either (a rename would dodge the key check).
    leaked_values = [
        k for k, v in observed.items() if v == "contract-sentinel-must-not-leak"
    ]
    assert not leaked_values, f"secret value leaked under: {leaked_values}"

    # Whatever else is present must be on the declared injection list —
    # an unexplained extra variable is a leak until proven otherwise.
    extras = observed.keys() - spec_env.keys()
    undeclared = extras - topology.injected_env_keys
    assert not undeclared, (
        f"undeclared env injected into the sandbox: {sorted(undeclared)} — "
        "either a leak, or the deployment contract changed and "
        "Topology.injected_env_keys must be updated deliberately"
    )


async def test_capabilities_are_dropped(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
    topology: Topology,
) -> None:
    """T-SEC-2: with the spec default cap_drop=ALL the capability
    BOUNDING set is empty — no exec/setuid path can ever regain a
    capability.

    Mutation note: a chown-fails-with-EPERM probe would pass even
    without CapDrop (a non-root uid has no effective caps anyway), so
    it cannot detect the hardening being dropped. CapBnd from
    /proc/self/status is the discriminator: docker's default bounding
    set is non-zero for every process regardless of uid. The chown
    probe is kept as the user-visible consequence of the same fact.
    """
    code = textwrap.dedent("""
        import json, os, sys
        caps = {}
        for line in open("/proc/self/status", encoding="ascii"):
            if line.startswith(("CapBnd:", "CapEff:", "CapPrm:")):
                key, _, value = line.partition(":")
                caps[key] = int(value.strip(), 16)
        path = "/scratch/own"
        open(path, "w").close()
        try:
            os.chown(path, 0, 0)
            chown = {"chowned": True}
        except PermissionError as exc:
            chown = {"chowned": False, "errno": exc.errno}
        sys.stderr.write(json.dumps({"caps": caps, **chown}))
    """)
    exit_code, stderr = await run_python(
        "sec-caps",
        code,
        tmpfs=make_tmpfs(
            "/scratch", size_mb=8, uid=topology.agent_uid, gid=topology.agent_gid
        ),
    )
    assert exit_code == 0
    result = json.loads(stderr)
    assert result["caps"]["CapBnd"] == 0, (
        f"capability bounding set not empty: {result['caps']}"
    )
    assert result["caps"]["CapEff"] == 0
    assert result["caps"]["CapPrm"] == 0
    assert result["chowned"] is False
    assert result["errno"] == 1  # EPERM


async def test_process_runs_as_unprivileged_uid(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
    topology: Topology,
) -> None:
    """T-SEC-3: the sandbox process runs as the image's unprivileged
    user (uid 1000), not root (docs/21 §8: K8s PSA `restricted` forbids
    root, so non-root is the cross-runtime contract)."""
    code = (
        "import json, os, sys; "
        "sys.stderr.write(json.dumps({'uid': os.getuid(), 'gid': os.getgid()}))"
    )
    exit_code, stderr = await run_python("sec-uid", code)
    assert exit_code == 0
    result = json.loads(stderr)
    assert result["uid"] == topology.agent_uid
    assert result["uid"] != 0
