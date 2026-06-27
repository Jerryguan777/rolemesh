"""Configuration constants and paths.

Where does a new env-driven setting go? ONE authoritative read site per
env var — never two. Pick by consumer scope:

1. Read by MULTIPLE modules, non-sensitive, no parse-time validation
   beyond a cast → module-level constant HERE (ports, image names,
   bridge names).

2. Read by ONE component, or needs construction-time validation /
   fail-closed semantics, or is a SECRET → a ``from_env()`` on that
   component's own config object (see egress/token_identity.py
   TokenAuthority, egress/dns_policy.py GlobalDnsPolicy). Do NOT
   mirror it here: a second reader of the same env var silently
   drifts from the real one (we shipped exactly that bug once —
   a dead EGRESS_TOKEN_TTL_SECONDS constant nobody imported).
   Other modules that need the value take the constructed object
   via dependency injection, not a re-read of os.environ.

3. Deployment-level override with a single use site and no fan-out
   (e.g. ANTHROPIC_BASE_URL upstream override in reverse_proxy) →
   lazy read at the use site, with a comment saying why.

Module-level constants here are frozen at import; tests overriding
them must patch every importing module's binding (a documented trap —
see tests/container/test_startup_order.py). Category-2 objects are
monkeypatch.setenv-testable, which is one of the reasons they exist.
"""

from __future__ import annotations

import os
from pathlib import Path

# ``.env`` → ``os.environ`` loading is handled by
# ``rolemesh.bootstrap`` at the process entry (imported first in
# rolemesh.main / webui.main). Here we read ``os.environ`` directly
# and the same lookup works whether the value was set by a shell
# export, systemd, docker --env-file, or a local ``.env`` file.

# Legacy: ASSISTANT_NAME is no longer the global trigger source.
# Kept for backward compatibility; new code should use coworker.name.
ASSISTANT_NAME: str = os.environ.get("ASSISTANT_NAME") or "Andy"
ASSISTANT_HAS_OWN_NUMBER: bool = os.environ.get("ASSISTANT_HAS_OWN_NUMBER", "") == "true"

POLL_INTERVAL: float = 2.0  # seconds
SCHEDULER_POLL_INTERVAL: float = 60.0  # seconds

PROJECT_ROOT: Path = Path.cwd()
HOME_DIR: Path = Path.home()

# Mount allowlist location. Default (host dev flow): the operator's
# ~/.config/rolemesh. ROLEMESH_MOUNT_ALLOWLIST overrides it for the
# containerized orchestrator, where the deployment layer bind-mounts the
# operator's config dir to a fixed in-container path (compose) or ships
# it as a ConfigMap (K8s) — see deploy/compose/compose.yaml.
_MOUNT_ALLOWLIST_ENV: str = os.environ.get("ROLEMESH_MOUNT_ALLOWLIST", "")
MOUNT_ALLOWLIST_PATH: Path = (
    Path(_MOUNT_ALLOWLIST_ENV)
    if _MOUNT_ALLOWLIST_ENV
    else HOME_DIR / ".config" / "rolemesh" / "mount-allowlist.json"
)
STORE_DIR: Path = PROJECT_ROOT / "store"
GROUPS_DIR: Path = PROJECT_ROOT / "groups"
DATA_DIR: Path = PROJECT_ROOT / "data"

# DooD path translation (docs/21 §7.1). When the orchestrator itself runs
# in a container, the bind sources it assembles (DATA_DIR / ...) are paths
# in ITS OWN filesystem, but the host dockerd that spawns agent sandboxes
# interprets bind sources against the HOST filesystem. This variable holds
# the host path that the deployment layer bind-mounted onto DATA_DIR
# (compose: ../../data -> /app/data), and DockerRuntime.run() rewrites
# every bind source under DATA_DIR to ROLEMESH_HOST_DATA_DIR/<relpath>.
# Empty (the default) = translation disabled — the host-process dev flow
# and the test suite keep their unchanged semantics.
ROLEMESH_HOST_DATA_DIR: str = os.environ.get("ROLEMESH_HOST_DATA_DIR", "")

DATABASE_URL: str = os.environ.get("DATABASE_URL", "postgresql://rolemesh:rolemesh@localhost:5432/rolemesh")
# RLS rollout (PR-B): a separate pool for cross-tenant maintenance,
# resolvers, and DDL connects under a BYPASSRLS role. In production
# this is its own DSN so the business pool can drop privileges. If
# unset, falls back to ``DATABASE_URL`` (acceptable for dev/test where
# the bootstrap user is also used for admin work).
ADMIN_DATABASE_URL: str = os.environ.get("ADMIN_DATABASE_URL", "")
NATS_URL: str = os.environ.get("NATS_URL", "nats://localhost:4222")

CONTAINER_IMAGE: str = os.environ.get("CONTAINER_IMAGE", "rolemesh-agent:latest")
CONTAINER_TIMEOUT: int = int(os.environ.get("CONTAINER_TIMEOUT", "1800000"))
CONTAINER_MAX_OUTPUT_SIZE: int = int(os.environ.get("CONTAINER_MAX_OUTPUT_SIZE", "10485760"))  # 10MB
CREDENTIAL_PROXY_PORT: int = int(os.environ.get("CREDENTIAL_PROXY_PORT", "3001"))
# Warm-pool dwell (slot-follows-turn rework): how long a completed container is
# kept warm for the next message before graceful idle reaping. It no longer
# pins an admission slot, so this is a pure warm-reuse / memory trade-off —
# default 5 min. Lowering it is safe: idle reaping is WARM-only and can never
# kill a processing turn.
IDLE_TIMEOUT: int = int(os.environ.get("IDLE_TIMEOUT", "300000"))  # 5 min

# Per-turn inactivity bound for the container watchdog: a processing turn that
# streams no output for this long is treated as hung and force-stopped. This is
# the FORCEFUL backstop, distinct from the (graceful, warm-only) IDLE_TIMEOUT.
# The watchdog floors it at APPROVAL_TIMEOUT + 30_000 at runtime so it can never
# pre-empt a pending HITL approval — replacing the old startup invariant.
TURN_INACTIVITY_TIMEOUT: int = int(os.environ.get("TURN_INACTIVITY_TIMEOUT", "420000"))  # 7 min

# HITL tool approval (docs/12-hitl-approval-architecture.md §5). The container's
# approval-decision await and the DB row's ``expires_at`` share this single
# bound. Default 5 min: the approver is the task creator (self-approval), so
# decisions resolve in seconds-to-minutes, and a short hold keeps the
# container-hold cost low.
APPROVAL_TIMEOUT: int = int(os.environ.get("APPROVAL_TIMEOUT", "300000"))  # 5 min

# Approval safety is now enforced at runtime, not by a startup invariant: the
# container watchdog (container_executor.py) floors its per-turn inactivity
# bound at ``APPROVAL_TIMEOUT + 30_000``, so it can never pre-empt a pending
# approval regardless of IDLE_TIMEOUT / TURN_INACTIVITY_TIMEOUT / per-coworker
# overrides. The former ``APPROVAL_TIMEOUT < IDLE_TIMEOUT + 30_000`` guard is
# therefore retired.

MCP_PROXY_PREFIX: str = "mcp-proxy"
MAX_CONCURRENT_CONTAINERS: int = max(1, int(os.environ.get("MAX_CONCURRENT_CONTAINERS", "5")))
GLOBAL_MAX_CONTAINERS: int = max(1, int(os.environ.get("GLOBAL_MAX_CONTAINERS", "20")))

# Runtime-abstraction backend selector: "docker" | "k8s" (not OCI runtime).
# Pairs with CONTAINER_OCI_RUNTIME below: ROLEMESH_CONTAINER_RUNTIME picks
# "which Python client talks to which orchestrator", OCI_RUNTIME picks "which
# binary actually runs the container process".
ROLEMESH_CONTAINER_RUNTIME: str = os.environ.get("ROLEMESH_CONTAINER_RUNTIME", "docker")

# Kubernetes backend settings (ROLEMESH_CONTAINER_RUNTIME=k8s; docs/21 §4.1).
# Category 1 of the module-docstring rules: non-sensitive deployment-shape
# values with no parse-time validation, read by container/k8s_runtime and
# by the contract suite's k8s Topology (tests/container/contract). The
# Helm chart declares the actual objects; these only tell the orchestrator
# where to find them.
#
#   ROLEMESH_K8S_NAMESPACE        namespace holding all RoleMesh objects
#   ROLEMESH_K8S_DATA_PVC         PVC bound to DATA_DIR (subPath translation,
#                                 docs/21 §7.1)
#   ROLEMESH_K8S_IMAGE_PULL_SECRET  optional imagePullSecrets name for agent
#                                 pods (private registries); empty = none
#   ROLEMESH_K8S_IMAGE_PULL_POLICY  imagePullPolicy for spawned agent pods.
#                                 Default "IfNotPresent" works for both
#                                 kind-loaded local images (no registry to
#                                 pull from — "Always" would ImagePullBackOff)
#                                 and registries with explicit tags.
#   ROLEMESH_K8S_RUNTIME_CLASS    RuntimeClass used when a spec asks for the
#                                 gVisor OCI runtime (spec.runtime="runsc");
#                                 empty = the conventional name "gvisor"
ROLEMESH_K8S_NAMESPACE: str = os.environ.get("ROLEMESH_K8S_NAMESPACE", "rolemesh")
ROLEMESH_K8S_DATA_PVC: str = os.environ.get("ROLEMESH_K8S_DATA_PVC", "rolemesh-data")
ROLEMESH_K8S_IMAGE_PULL_SECRET: str = os.environ.get("ROLEMESH_K8S_IMAGE_PULL_SECRET", "")
ROLEMESH_K8S_IMAGE_PULL_POLICY: str = os.environ.get(
    "ROLEMESH_K8S_IMAGE_PULL_POLICY", "IfNotPresent"
)
ROLEMESH_K8S_RUNTIME_CLASS: str = os.environ.get("ROLEMESH_K8S_RUNTIME_CLASS", "")

# OCI runtime selection (R1). "runc" is the default; "runsc" enables gVisor
# syscall-level sandboxing and requires runsc to be registered in
# /etc/docker/daemon.json on the host. Per-coworker overrides live in
# ContainerConfig.runtime.
#
# Named OCI to disambiguate from ROLEMESH_CONTAINER_RUNTIME (docker vs k8s):
# this variable selects the OCI runtime binary (runc/runsc), not the
# runtime-abstraction backend.
CONTAINER_OCI_RUNTIME: str = os.environ.get("CONTAINER_OCI_RUNTIME", "runc")

# Per-container resource ceilings (R7). Overrides come from ContainerConfig
# on each coworker and are clamped to CONTAINER_MAX_* in runner.build_container_spec.
CONTAINER_MEMORY_LIMIT: str = os.environ.get("CONTAINER_MEMORY_LIMIT", "2g")
CONTAINER_CPU_LIMIT: float = float(os.environ.get("CONTAINER_CPU_LIMIT", "2.0"))
CONTAINER_PIDS_LIMIT: int = int(os.environ.get("CONTAINER_PIDS_LIMIT", "512"))
CONTAINER_MAX_MEMORY: str = os.environ.get("CONTAINER_MAX_MEMORY", "8g")
CONTAINER_MAX_CPU: float = float(os.environ.get("CONTAINER_MAX_CPU", "4.0"))

# Custom bridge network for agent containers (R5).
#
# Egress Control V1 (EC-1) turns this network into a Docker --internal
# bridge. Containers on it physically cannot route to the public internet
# — all outbound traffic must flow through the egress gateway which sits
# on a second bridge (CONTAINER_EGRESS_NETWORK_NAME) with a real default
# route. Egress control is always on (docs/21 §1: the EC=off runtime
# branch was removed); an empty value is a hard configuration error at
# startup.
CONTAINER_NETWORK_NAME: str = os.environ.get("CONTAINER_NETWORK_NAME", "rolemesh-agent-net")


# Outbound bridge used by the egress gateway (EC-1). Regular bridge with
# icc=false. The gateway container is dual-homed: agent-net (internal) on
# one side, egress-net on the other, so it acts as the only exit the
# agent bridge has.
CONTAINER_EGRESS_NETWORK_NAME: str = os.environ.get(
    "CONTAINER_EGRESS_NETWORK_NAME", "rolemesh-egress-net"
)

# Egress gateway container. Name is fixed so agents can resolve it by
# service name on the agent-net bridge (Docker embedded DNS binds the
# container name to its bridge IP). Image tag is overridable so operators
# can pin to a digest for reproducible deploys.
EGRESS_GATEWAY_CONTAINER_NAME: str = os.environ.get(
    "EGRESS_GATEWAY_CONTAINER_NAME", "egress-gateway"
)
EGRESS_GATEWAY_IMAGE: str = os.environ.get(
    "EGRESS_GATEWAY_IMAGE", "rolemesh-egress-gateway:latest"
)
# Static address of the egress gateway on the agent bridge. The
# deployment layer declares it (compose ipam fixed IP today; K8s
# Service ClusterIP later) and the orchestrator only VERIFIES it at
# startup (ContainerRuntime.verify_infrastructure) — replacing the old
# runtime discovery via docker-inspect after gateway launch. The
# default matches the compose subnet in deploy/compose/compose.yaml
# (agent-net 172.28.100.0/24); override the env var if you override
# the subnet. Consumers: runner (pins it as each agent's DNS resolver)
# and docker_runtime (verifies the gateway actually holds this IP).
EGRESS_GATEWAY_DNS_IP: str = os.environ.get(
    "EGRESS_GATEWAY_DNS_IP", "172.28.100.53"
)
# HTTP forward-proxy port (CONNECT) — agents see this via HTTPS_PROXY env.
# EC-2 wires the CONNECT handler; EC-1 ships the port as a placeholder so
# the gateway container declaration is complete.
EGRESS_GATEWAY_FORWARD_PORT: int = int(
    os.environ.get("EGRESS_GATEWAY_FORWARD_PORT", "3128")
)
# Authoritative DNS resolver port inside the gateway container. EC-2
# binds the resolver; EC-1 keeps the default in config so the launcher
# contract is final from the start.
EGRESS_GATEWAY_DNS_PORT: int = int(
    os.environ.get("EGRESS_GATEWAY_DNS_PORT", "53")
)

# Egress identity tokens: EGRESS_TOKEN_SECRET and EGRESS_TOKEN_TTL_SECONDS
# are deliberately NOT mirrored here — category 2 of the module-docstring
# rules (single consumer + validation + secret). Their one read site is
# rolemesh.egress.token_identity.TokenAuthority.from_env().

# Allowlist for env vars that the orchestrator dynamically injects into
# containers (R8). Anything produced by build_container_spec() or passed
# via AgentBackendConfig.extra_env must be in this set; unknown keys are
# dropped with a structured warning.
#
# The following are intentionally NOT in the allowlist:
#   PATH             — Docker does not inherit parent PATH; the container's
#                      PATH is set by the image's ENV layer and must stay
#                      fixed there.
#   LANG / LC_ALL    — Container locale must not track host locale; pinned
#                      to C.UTF-8 in the Dockerfile.
#   PYTHONUNBUFFERED — Image property, not a per-tenant knob; set to 1 in
#                      the Dockerfile.
#   HTTP_PROXY etc.  — Now allowlisted by EC-1. Orchestrator injects
#                      them pointing at the egress gateway so every
#                      library that honours the standard proxy env vars
#                      (urllib, requests, httpx, aiohttp with trust_env,
#                      curl, wget, pip, git) automatically routes through
#                      the gateway. Agent override is not a concern
#                      because CapDrop:ALL + no-new-privileges prevent
#                      the agent from re-executing with different env.
CONTAINER_ENV_ALLOWLIST: frozenset[str] = frozenset({
    "TZ",
    "NATS_URL",
    "JOB_ID",
    "AGENT_BACKEND",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    # EC-1: standard outbound-proxy env. All three are conventional
    # uppercase-only; lowercase variants (http_proxy, …) are NOT
    # injected — most SDKs honour either, but keeping the set minimal
    # reduces the surface we have to reason about.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    # Redirects Claude Code CLI's `.claude.json` from $HOME/.claude.json to
    # $CLAUDE_CONFIG_DIR/.claude.json. Required under ReadonlyRootfs=True
    # because /home/agent/ itself is read-only; without this env the CLI
    # tries to write /home/agent/.claude.json, hits EROFS, and agent
    # initialization times out.
    # Value is injected in runner.build_container_spec pointing at the
    # per-coworker bind mount /home/agent/.claude, which also makes the
    # config file naturally persist (scoped per coworker) across container
    # spawns.
    "CLAUDE_CONFIG_DIR",
    "HOME",
    "PI_MODEL_ID",
    # Bedrock — only the placeholder bearer + synthesized proxy URL
    # ever reach the container. The real ``ABSK...`` token lives on
    # the host and is overwritten on every request by the credential
    # proxy (see ``rolemesh.egress.reverse_proxy._build_provider_registry``).
    # ``AWS_REGION`` is needed by boto3 to construct model ARNs.
    # ``BEDROCK_BASE_URL`` is written directly in
    # ``runner.build_container_spec`` and bypasses the filter today;
    # listed here as a guard if a future refactor moves URL synthesis
    # back into ``backend_config.extra_env``.
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_REGION",
    "BEDROCK_BASE_URL",
})

# Default AWS region for Bedrock when ``AWS_REGION`` is unset on the
# host. Single source of truth: the credential proxy uses it to build
# the upstream URL (``bedrock-runtime.{region}.amazonaws.com``) and
# ``_pi_extra_env`` uses the same fallback when synthesising the
# container's ``AWS_REGION`` env so boto3 model-ARN resolution lines
# up with the proxy's endpoint. Drift between the two would resolve
# model ARNs in one region while routing requests to another.
BEDROCK_DEFAULT_REGION: str = "us-east-1"

# Agent backend: "claude" or "pi"
AGENT_BACKEND_DEFAULT: str = os.environ.get("ROLEMESH_AGENT_BACKEND", "claude")

# Auth configuration
AUTH_MODE: str = os.environ.get("AUTH_MODE", "external")
ROLEMESH_TOKEN_SECRET: str = os.environ.get("ROLEMESH_TOKEN_SECRET", "")

# SAFETY_FAIL_MODE governs the Safety Framework boot-time behaviour:
# on DB unreachable at container start, "closed" (default) refuses the
# job; "open" runs the agent without safety rules and logs ERROR. The
# safety hook itself is already fail-closed at runtime (check
# exceptions propagate to a block) — this flag only governs the
# boot-time rule snapshot load.
SAFETY_FAIL_MODE: str = os.environ.get("SAFETY_FAIL_MODE", "closed")

# Timezone for scheduled tasks — needs IANA name (e.g. "America/New_York"), not abbreviation ("EST").
TIMEZONE: str = os.environ.get("TZ", "")

if not TIMEZONE or "/" not in TIMEZONE:
    # Try /etc/timezone (Debian/Ubuntu)
    try:
        _tz = Path("/etc/timezone").read_text().strip()
        if "/" in _tz:
            TIMEZONE = _tz
    except OSError:
        pass

if not TIMEZONE or "/" not in TIMEZONE:
    # Try /etc/localtime symlink (most Linux distros + macOS)
    try:
        _link = os.readlink("/etc/localtime")
        _tz = _link.split("zoneinfo/")[-1]
        if "/" in _tz:
            TIMEZONE = _tz
    except OSError:
        pass

if not TIMEZONE or "/" not in TIMEZONE:
    TIMEZONE = "UTC"
