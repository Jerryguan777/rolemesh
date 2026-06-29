# Attack Simulation Suite

Every test in this directory models a **concrete attack** against the
running system and asserts that one of the defense layers
(container hardening, safety framework) neutralizes it.

**Not a fuzzer.** Each test is a named, deterministic scenario with a
clear attacker goal and a clear pass/fail criterion. If a test starts
failing, a known defense just regressed.

## Attack categories

| File | Category | Defenses exercised |
|---|---|---|
| `test_A_container_escape_spec.py` | Container escape / sandbox breakout (Docker) | Container hardening R1-R9 (`HostConfig`) |
| `test_A_container_escape_k8s_spec.py` | Container escape / sandbox breakout (Kubernetes) | Pod `securityContext` + agent NetworkPolicy label contract |
| `test_B_secret_exfil.py` | Credential / secret theft | Env allowlist + safety `secret_scanner` |
| `test_C_prompt_injection.py` | Prompt injection / jailbreak | Safety `llm_guard.prompt_injection`, `llm_guard.jailbreak` |
| `test_D_data_exfil.py` | Data exfiltration (PII, URL, DNS) | Safety `pii.regex`, `domain_allowlist`, egress `GlobalDnsPolicy` |
| `test_E_tenant_isolation.py` | Tenant isolation | `SafetyRpcServer` authoritative-tenant guard |
| `test_G_dos.py` | Denial of service | Pipeline resilience, worker queueing, rate limits |
| `test_H_config_attack.py` | Configuration-layer attack | Pydantic validation, AgentPermissions |
| `test_I_egress_gateway.py` | Network egress (gateway plane) | `egress.domain_rule` allowlist + `GlobalDnsPolicy` |

## Running

```bash
# Full suite (spec-level + engine-level):
pytest tests/attack_sim/ -v

# Only the spec-level / fast tests:
pytest tests/attack_sim/ -v -m "not requires_db"

# Include ML-backed corpus tests:
uv sync --extra safety-ml --extra dev
pytest tests/attack_sim/ -v
```

## Expected failures (xfail)

Some tests are explicit `strict=True xfail` markers documenting **known
undefended attacks**. They will start passing (XPASS, which strict mode
turns into a failure) once the matching defense layer is shipped. Do not
silence them — they are the success metric for the next security layer.

- `test_B_secret_exfil::test_B2_credential_proxy_path_enumeration_rate_limited`
  → closes with a rate-limit or per-agent auth on the credential proxy
  (today an attacker inside a compromised container can enumerate
  registered MCP endpoints by probing paths).
- `test_E_tenant_isolation::test_E6_consistent_cross_tenant_forge_is_rejected`
  → closes with NATS account-per-tenant / tenant-scoped credentials; the
  safety-plane guard catches an *inconsistent* identity forge but not a
  fully consistent one on core NATS.

(The previous D4 DNS-exfil xfail was retired — EC-2 shipped the
authoritative DNS resolver, so `test_D4_*` now asserts the defense holds.)

## Companion manual runbook

Attacks that require a real running container and host-level
operations live in `scripts/verify-hardening.sh`. That script is the
ground-truth verification that defenses actually engage at run-time;
this Python suite is the **regression net** that catches config /
code changes that would weaken them.
