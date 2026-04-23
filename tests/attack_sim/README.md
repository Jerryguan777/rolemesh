# Attack Simulation Suite

Every test in this directory models a **concrete attack** against the
running system and asserts that one of the three defense layers
(container hardening, approval, safety framework) neutralizes it.

**Not a fuzzer.** Each test is a named, deterministic scenario with a
clear attacker goal and a clear pass/fail criterion. If a test starts
failing, a known defense just regressed.

## Attack categories

| File | Category | Defenses exercised |
|---|---|---|
| `test_A_container_escape_spec.py` | Container escape / sandbox breakout | Container hardening R1-R9 |
| `test_B_secret_exfil.py` | Credential / secret theft | Env allowlist + safety `secret_scanner` |
| `test_C_prompt_injection.py` | Prompt injection / jailbreak | Safety `llm_guard.prompt_injection`, `llm_guard.jailbreak` |
| `test_D_data_exfil.py` | Data exfiltration (PII, URL, DNS) | Safety `pii.regex`, `presidio.pii`, `domain_allowlist` |
| `test_E_tenant_isolation.py` | Cross-tenant forgery and leakage | Approval engine `_tenant_matches`, IPC dispatcher, REST |
| `test_F_approval_abuse.py` | Approval flow race / bypass | Approval engine atomic CAS, audit trigger |
| `test_G_dos.py` | Denial of service | Pipeline resilience, worker queueing, rate limits |
| `test_H_config_attack.py` | Configuration-layer attack | Pydantic validation, AgentPermissions |

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

Some tests are explicit `xfail` markers documenting **known undefended
attacks**. They will start passing once the matching defense layer is
shipped. Do not silence them — they are the success metric for the
next security layer.

- `test_D_data_exfil::test_dns_exfiltration_via_dig` → blocked by
  egress control (EC-1/EC-2, not yet implemented)
- `test_E_tenant_isolation::test_nats_subject_sidechannel` → blocked
  by NATS subject ACL (per-tenant NATS accounts, not yet implemented)
- `test_B_secret_exfil::test_read_other_coworker_env_via_proc` →
  blocked by seccomp (verified manually via `verify-hardening.sh`)

## Companion manual runbook

Attacks that require a real running container and host-level
operations live in `scripts/verify-hardening.sh`. That script is the
ground-truth verification that defenses actually engage at run-time;
this Python suite is the **regression net** that catches config /
code changes that would weaken them.
