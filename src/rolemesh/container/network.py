"""Agent-bridge network helpers.

Historical note: this module used to own the imperative side of the
egress-control topology — idempotent creation of the agent and egress
bridges plus throwaway probe containers checking gateway/NATS
reachability from inside the bridge. The declarative-infrastructure
refactor (docs/21 §1) retired all of that: networks and the gateway are
declared by the deployment layer (deploy/compose/compose.yaml) and the
orchestrator only verifies the invariants at startup via
``DockerRuntime.verify_infrastructure`` (read-only, fail-closed).

What remains is the single pure helper shared by the spawn path.
"""

from __future__ import annotations


def agent_facing_nats_url(nats_url: str) -> str:
    """Rewrite a loopback NATS URL to the ``nats`` service name that
    resolves on the internal agent bridge.

    Agent containers sit on an ``Internal=true`` bridge with no route to
    the host, so the orchestrator's own ``nats://localhost:4222`` is
    meaningless to them. They reach NATS by the ``nats`` service name
    attached to the bridge (compose attaches the nats container to
    agent-net). ``runner.compute_egress_routing`` injects the rewritten
    URL into each agent container's env.
    """
    return nats_url.replace("://localhost:", "://nats:").replace(
        "://127.0.0.1:", "://nats:"
    )
