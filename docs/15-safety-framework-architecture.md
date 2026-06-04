# Safety Framework Architecture

This document explains RoleMesh's Safety Framework — a policy framework that lets administrators, without writing code, impose runtime detection and interception on an agent's inputs, tool calls, and outputs.

It covers why this is a **framework** rather than a handful of hard-coded checks, which abstraction levels were considered and rejected, the design intent behind the Stage / Context / Check / Rule quartet, and what V1 deliberately leaves out.

Target audience: developers adding new Checks (PII detectors, prompt-injection classifiers, egress rules, etc.), debugging why a rule did not fire, porting Safety to a new stage, or understanding "why not OPA/CEL." Prerequisite: [`13-safety-overview.md`](13-safety-overview.md) §2.4 / §2.5 / §2.7 / §2.8.

---

## Background: Scattered Safety-Detection Needs

Container Hardening blocks "container escape," but business-wise an agent inside its container can still:

- Stuff SSNs / credit-card numbers from the prompt into tool parameters and send them outside
- Be prompt-injected into invoking unauthorized high-risk tools
- Hand LLM output (possibly containing upstream-leaked API keys) directly to users
- Burn tokens in loops, hammer the same tool, be abused for free workload

These problems have varied shapes but the same essence: **"at a clearly defined event point (before a tool call, before a prompt enters, after model output), run a piece of detection logic and decide allow / block based on the result."**

Historically such needs ended up implemented one-off: each detection becomes an independent slab of code that adds hooks, DB tables, REST endpoints, and audit pipelines. The result:

- Inconsistent interception semantics across detections (one `raise`s, one returns `None`, one logs and lets through)
- Each invents its own fail-mode (one fail-close, one fail-safe, nobody remembers which is which)
- Inconsistent audit formats (detection A writes audit log, detection B writes only stderr)
- Every new detection requires touching agent core code

The Safety Framework exists to **abstract this work into a unified shape**, turning "adding a new safety capability" into "write a Check class + register one config line" — no touching agent core, no new tables, no new REST endpoints.

---

## Design Goals

1. **Check is the only extension unit.** A Check class = one detection + its own action semantics. Adding a detection does not touch the pipeline, the DB schema, or REST routing.
2. **Zero overhead when inactive.** A tenant with no rules behaves bit-identically to a world where the framework does not exist — the hook handler is not registered, the imports do not happen, no extra DB queries on the hot path.
3. **Unified fail-mode semantics.** Control stages (pre tool call, input prompt, model output) fail-close; observational stages (post tool result, pre compaction) fail-safe. Every Check obeys this — it is not the Check's call to make.
4. **Unified audit shape.** Every Check's decision lands in the same `safety_decisions` table with `triggered_rule_ids` / `findings` / `context_digest`. One table for an auditor to look at.
5. **Native multi-tenant isolation.** Every rule is enforced through `tenant_id`; `coworker_id=NULL` means tenant-wide; no cross-tenant inheritance allowed.
6. **Data minimization.** The audit stores `context_digest` (SHA-256) + a short summary, **not the raw payload** — preventing PII from leaking through the audit table (see [`13-safety-overview.md`](13-safety-overview.md) §2.7).
7. **Hot update (next-job-applies).** When an admin changes a rule, the next agent invocation picks up the new snapshot; in-flight jobs continue with the snapshot they started with — avoiding mid-flight behavior drift that is hard to debug.

---

## Considered Alternatives

### Alternative A — Hard-code Each Detection

For every new safety detection, write an if/else directly inside agent_runner:

```python
async def on_pre_tool_use(event):
    if has_pii(event.params): return block(...)
    if hits_prompt_injection(event.params): return block(...)
    if hits_rate_limit(event.tenant_id): return block(...)
    ...
```

**Pros**: Straightforward, no abstraction layers.

**Cons**: Scattered fail-modes, inconsistent audit, changing one detection may affect the hit order of another, adding a new detection means changing main flow. **Fundamentally conflicts with the "declarative policy" shape this framework targets** — admins cannot configure rules without writing code.

**Rejected** — does not scale.

### Alternative B — Bring in OPA / CEL Policy DSL

Write each rule as a Rego or CEL expression:

```rego
deny[reason] {
    input.stage == "pre_tool_call"
    input.params.amount > 1000
    reason := "amount exceeds threshold, blocked"
}
```

**Pros**: Highly expressive; admins can write complex conditions; industry-proven (Kubernetes Admission, Envoy RBAC both use it).

**Cons**:
- Steep operator-learning curve — admins must learn a new DSL
- The DSL engine is itself an external dependency (OPA sidecar or celpy library)
- Poor debugging experience — when a Rego policy does not fire, hard to locate the cause
- To do PII detection, LLM Guard prompt injection, etc., **you still have to write Python code** — the DSL can only write if-then, it cannot call ML models
- Every real current need is covered by "a Check class + a few boolean parameters" — **not yet at the complexity that requires a DSL**

**Rejected (V1/V2)** — explicitly deferred to "when composite Check classes become inadequate." Note this rejects "the DSL expression form," not "a policy framework" — the latter is the Safety Framework itself.

### Alternative C — Use an Off-the-Shelf LLM Safety Framework (NeMo Guardrails, LangChain Guardrails, etc.)

Plug into some open-source framework directly.

**Pros**: Less code written.

**Cons**:
- Their default scenario is "single-LLM application," not "multi-tenant agent orchestrator"
- Audit / multi-tenancy / NATS IPC all need deep customization — you end up wrapping it anyway
- Locked to that framework's release cadence

**Rejected** — building a 1500-LoC custom skeleton + adapting existing detector libraries (Presidio, LLM Guard, Lakera, OpenAI Moderation) is the more stable path.

### Alternative D — Quartet Abstraction + Check as the Sole Extension Unit (Selected)

- **Stage** enumerates every decision point (pre tool call, input prompt, model output, post tool result, pre compaction)
- **SafetyContext** is a read-only data carrier, holding a stage-specific payload
- **SafetyCheck** Protocol: each check class declares the stages it supports, its cost class, a stable Finding code set, and an optional pydantic config schema
- **Rule** is a DB row: pick a check + configure its config + bind to a stage + bind to scope (tenant + optional coworker)

**Pros**: All goals met; adding a capability = writing a Check class + registering one line; no DSL introduced.

**Selected.** This is the shape the rest of this document describes.

---

## Core Abstractions

Only 4 concepts, **deliberately**. Any extension that wants to introduce a fifth must first prove it cannot be expressed via the existing four.

### Stage

A `StrEnum` listing every "event point where a safety decision may occur":

```
INPUT_PROMPT       — before a user prompt enters the agent
PRE_TOOL_CALL      — before a tool call executes
POST_TOOL_RESULT   — before a tool result is fed back to the LLM
MODEL_OUTPUT       — before the final model output reaches the user
PRE_COMPACTION     — before long-conversation compaction (prevents loss
                     of sensitive-context detection window after compaction)
```

**Control vs Observational split**:
- `{INPUT_PROMPT, PRE_TOOL_CALL, MODEL_OUTPUT}` are control stages — check exception = fail-close = whole event is blocked
- `{POST_TOOL_RESULT, PRE_COMPACTION}` are observational stages — check exception = fail-safe = skip that check + log, event continues

This split is **enforced in the pipeline**, not the Check's call. Future additions like `EGRESS_REQUEST` (see [`16-egress-control-architecture.md`](16-egress-control-architecture.md)) must explicitly declare which side they belong to.

### SafetyContext

A read-only frozen dataclass carrying everything about one decision:

```
stage              current stage
tenant_id / coworker_id / user_id / job_id / conversation_id
payload            stage-specific fields (see types.py comments)
tool               ToolInfo | None  (only for PRE_TOOL_CALL / POST_TOOL_RESULT)
metadata           dict, reserved for extension
```

Payload schema per stage:

- `INPUT_PROMPT` → `{prompt: str}`
- `PRE_TOOL_CALL` → `{tool_name: str, tool_input: dict}`
- `POST_TOOL_RESULT` → `{tool_name, tool_input, tool_result, is_error}`
- `MODEL_OUTPUT` → `{text: str}`
- `PRE_COMPACTION` → `{transcript_path, messages}`

New stages must extend this schema rather than dumping fields into `metadata`.

### SafetyCheck (Protocol)

```python
class SafetyCheck(Protocol):
    id: str                          # "pii.regex", "egress.domain_rule"
    version: str                     # schema version, bump on change
    stages: frozenset[Stage]         # which stages this check supports
    cost_class: CostClass            # "cheap" | "slow"
    supported_codes: frozenset[str]  # stable Finding codes this check may emit
    action_model: ActionModel        # "fixed" | "config_routed" | "aggregated"
    natural_actions: Mapping[Stage, Action]            # default action per stage
    supported_actions: Mapping[Stage, frozenset[Action]]  # offerable actions per stage
    config_model: type[BaseModel] | None  # pydantic schema, REST validates rule.config

    async def check(ctx: SafetyContext, config: dict) -> Verdict: ...
```

**Critical constraints** ("adapter discipline" — checks that wrap third-party libraries must follow these):
- `supported_codes` is a **stable enum**, decoupled from the external library's internal taxonomy — a library upgrade introducing new categories is silently dropped at the mapping layer, never leaking into Finding
- `version` starts at "1", **must bump on backward-incompatible code changes**
- Every new adapter check (e.g. `presidio.pii`) must include a unit test that "mocks the external library returning an unknown type → should be dropped"

#### Action metadata (`action_model` / `natural_actions` / `supported_actions`)

These three fields are **descriptive, not prescriptive**. They report what a check's current code does so the rule-editor UI can (1) show the default action a hit produces and (2) grey out actions that cannot be carried out for a given `(check, stage)`. **The pipeline does not consume them** — they never change how a verdict is routed, and declaring them must never drive a change to `check()` behaviour. The single readable source of truth is the global matrix comment at the top of `src/rolemesh/safety/checks/__init__.py`; each check repeats its own slice next to the field declarations.

- **`action_model`** — how the check decides its action, which maps onto three real architectures:
  - `fixed` — the action is hardcoded in `check()`; a hit always returns the same action (today every fixed check returns `block`). `natural_actions[stage]` is that action. UI shows *"This check defaults to: BLOCK"*.
  - `config_routed` — the action is chosen per-finding by the rule config (e.g. `presidio.pii` `block_codes` vs `redact_codes`). Under the default/empty config a hit is **inert** (`allow`), so `natural_actions` is `allow`; UI shows *"configure per-category below"* rather than a default badge.
  - `aggregated` — the check only **votes**; a later layer decides the effective verdict (`egress.domain_rule` returns `allow` on a match and the gateway blocks when no rule allowed). `natural_actions` is the check's own return (`allow`).
- **`natural_actions: Mapping[Stage, Action]`** — the action a hit produces under a config that enables detection but does **not** override the action. The UI uses it for the default badge.
- **`supported_actions: Mapping[Stage, frozenset[Action]]`** — the actions a rule on that `(check, stage)` can meaningfully produce, gated by real capabilities: `redact` only where the check can emit a `modified_payload` (today `presidio.pii` alone); `warn` only where a stage consumes `appended_context` (excluded on `MODEL_OUTPUT` / `EGRESS_REQUEST`); `require_approval` only where the stage has an approval surface (excluded on `POST_TOOL_RESULT` and `EGRESS_REQUEST`); `block` / `allow` everywhere.

Three invariants are enforced by `tests/safety/test_action_matrix.py`: (1) `natural_actions.keys() == supported_actions.keys() == stages`; (2) `natural_actions[stage] ∈ supported_actions[stage]`; (3) running `check()` on a firing input (config enables detection, does not override the action) returns `natural_actions[stage]` — the **runtime anchor** that fails the test if a check's behaviour drifts from its declared matrix. Plus a legality check that every `supported_actions` value is in the pipeline's `_V2_ALLOWED_ACTIONS`. The REST `/safety/checks` endpoints project all three fields (frozensets serialised as sorted lists for cache stability).

### Rule

A row in DB table `safety_rules`:

```
id, tenant_id, coworker_id (None=tenant-wide)
stage, check_id, config (JSONB)
priority, enabled, description
created_at, updated_at
```

**Rule is a frozen dataclass** — immutable once loaded from DB; serialized via `Rule.to_snapshot_dict()` and sent to the container.

### Verdict

The Check's return value:

```python
@dataclass(frozen=True)
class Verdict:
    action: "allow" | "block" | "redact" | "warn" | "require_approval"
    reason: str | None
    modified_payload: Any | None     # for action="redact"
    findings: list[Finding]          # audit details
    appended_context: str | None     # for action="warn", appended to agent context
```

**V1 only allows `allow / block`** — the pipeline rejects other actions on control stages (V2 opens them). This is intentional staged rollout; V1 must prove the skeleton first before opening more boundary cases.

### Finding

A granular record produced on each hit, written to audit:

```python
@dataclass(frozen=True)
class Finding:
    code: str          # stable enum ("PII.SSN", "EGRESS.DOMAIN_DENIED")
    severity: "info" | "low" | "medium" | "high" | "critical"
    message: str
    metadata: dict     # check-specific
```

---

## Architecture

### Process Topology

```
┌── Orchestrator (host process) ──────────────────────────┐
│                                                          │
│  Container Executor                                      │
│    ├─ load_safety_rules_snapshot(tid, cid)               │
│    │    → list[Rule.to_snapshot_dict()]                 │
│    └─ → AgentInitData.safety_rules                       │
│                                                          │
│  REST API (/api/admin/tenants/{tid}/safety/rules)        │
│    ├─ POST/GET/PATCH/DELETE                              │
│    └─ pydantic validation (check.config_model)           │
│                                                          │
│  Safety Engine                                           │
│    ├─ NATS subscribe: agent.*.safety_events              │
│    └─ DbAuditSink.write → safety_decisions               │
│                                                          │
│  CheckRegistry (singleton)                               │
│    └─ orchestrator-side: all checks (cheap + slow)       │
└──────────────────────────────────────────────────────────┘
                   │
                   │ AgentInitData (carrying safety_rules snapshot)
                   ▼
┌── Agent Container (per job) ────────────────────────────┐
│                                                          │
│  agent_runner/main.py                                    │
│    if init.safety_rules: register SafetyHookHandler      │
│                                                          │
│  SafetyHookHandler                                       │
│    on_pre_tool_use → pipeline_run(rules, registry, ctx)  │
│                                                          │
│  pipeline_run                                            │
│    1. filter rules by stage + coworker_id                │
│    2. sort by priority desc                              │
│    3. for each rule: check.check(ctx, rule.config)       │
│       - block → publish audit + short-circuit            │
│       - allow → publish audit + continue                 │
│    4. fail-mode handling (control vs observational)      │
│                                                          │
│  CheckRegistry (container-side)                          │
│    └─ cheap checks only                                  │
│                                                          │
│  AuditPublisher → NATS: agent.{job_id}.safety_events     │
└──────────────────────────────────────────────────────────┘
```

### Data Flow (a complete PRE_TOOL_CALL path)

```
Claude / Pi backend decides to call a tool
  ↓
HookRegistry.emit_pre_tool_use(event)
  ↓
SafetyHookHandler.on_pre_tool_use(event)
  ↓
Build SafetyContext (stage=PRE_TOOL_CALL, ...)
  ↓
pipeline_run(snapshot_rules, registry, ctx, publisher):
  1. Filter applicable rules (stage + enabled + coworker scope)
  2. Sort by priority desc
  3. for each rule:
       check = registry.get(rule.check_id)
       verdict = await check.check(ctx, rule.config)
       publisher.publish(audit_event)   ← async NATS, non-blocking
       if verdict.action == "block": break
  4. on control stage check exception → raise (fail-close → BLOCK)
     on observational → log + skip
  ↓
Return ToolCallVerdict (block / allow) to the backend
  ↓
Backend executes or cancels the tool call accordingly
```

The orchestrator side independently subscribes the NATS and writes to DB:

```
Orchestrator: SafetyEngine
  ↓ NATS subscribe agent.*.safety_events
  ↓
DbAuditSink.write(AuditEvent)
  ↓
INSERT INTO safety_decisions:
  tenant_id, coworker_id, stage, verdict_action,
  triggered_rule_ids[], findings[], context_digest (SHA-256),
  context_summary (first 80 chars)
```

### Config Flow (rule creation → effect)

```
admin: POST /api/admin/tenants/{tid}/safety/rules
  ↓
REST validation:
  - check_id in orchestrator registry?
  - stage in check.stages?
  - config passes check.config_model?
  ↓
INSERT INTO safety_rules (+ trigger writes safety_rules_audit)
  ↓
... (new rule is now stored, but does not affect in-flight jobs) ...
  ↓
On next ContainerAgentExecutor startup:
  load_safety_rules_snapshot(tid, cid) queries latest rules
  → packed into AgentInitData
  ↓
Container receives snapshot, immutable for the duration of the job
```

### Database Schema Overview

| Table | Purpose |
|---|---|
| `safety_rules` | Rule configuration (with audit trigger auto-writing to `safety_rules_audit`) |
| `safety_rules_audit` | Rule-change timeline (not UPDATE/DELETE-able by application layer) |
| `safety_decisions` | Per-decision audit (stores digest only, not raw payload) |

---

## V1 Implemented vs V2 To-Do

### V1 (merged on the `safety/framework` branch)

- 5 Stage enums; PRE_TOOL_CALL is wired
- Only built-in check: `pii.regex` (SSN / credit card / Email / US phone / IP regex)
- Three tables: `safety_rules / safety_rules_audit / safety_decisions`
- Complete REST CRUD
- Pipeline allows only `allow / block` actions
- Container-side zero-overhead guarantee (no rules → no hook registration)
- Audit via fire-and-forget NATS + DB write
- Snapshot-based hot update (next-job-applies)

### V2 (designed, pending implementation)

- All stages wired (INPUT_PROMPT, MODEL_OUTPUT, POST_TOOL_RESULT, PRE_COMPACTION)
- New actions: `redact / warn / require_approval`
- Slow-check RPC channel (container-side runs cheap checks synchronously; orchestrator-side runs slow checks via NATS request-reply)
- Third-party adapter checks: `presidio.pii`, `llm_guard.prompt_injection`, `llm_guard.jailbreak`, `llm_guard.toxicity`, `openai_moderation`, `secret_scanner` (detect-secrets)
- `rate_limit` check (per-tenant / per-tool counters)
- `domain_allowlist` check (tool input URL allowlist — complementary to Egress Control)
- Time-window scheduling (`active_hours` / `active_days`)
- Audit CSV / Webhook export
- Admin UI

### Never

- **CEL / Rego policy DSL** — see alternative B rejection
- **Replace existing sender_allowlist / mount_security** — they continue to exist independently
- **Cross-tenant policy inheritance / template marketplace** — the antithesis of native multi-tenant isolation
- **Self-hosted local GPU inference (Llama Guard, etc.)** — deployment cost too high, external APIs suffice

---

## Tradeoffs and Boundaries

### Accepted Tradeoffs

- **Snapshot-based hot update (not real-time)**: in-flight jobs do not see rule changes — avoids hard-to-debug behavior drift, at the cost of admins needing a new job to see effect
- **Stores digest not raw payload**: auditors cannot see specific content — avoids PII secondary leak, at the cost of having to go through conversation history for root-cause investigation
- **V1 only `allow/block`**: UX is coarse — but V1 must prove the skeleton; V2 opens action expressiveness
- **No DSL**: complex conditions require writing Python classes — but avoids the operator and debugging cost of OPA/Rego

### Boundaries (not part of Safety Framework)

- **Credential injection** → standalone `credential_proxy` module
- **Container network isolation** → Container Hardening + Egress Control
- **Mount path allowlist** → standalone `mount_security` module (neither V1 nor V2 migrates it in)
- **Channel-message sender allowlist** → standalone `sender_allowlist` module (neither V1 nor V2 migrates it in)

Keeping these outside the Safety Framework is because each is **a mature, independently-shaped subsystem** — folding them into the framework is over-abstraction. The Safety Framework solves the class of "**continuously-emerging new LLM-safety detections**."

---

## In One Sentence

**The Safety Framework is a policy framework where "adding a new LLM-safety capability = writing a Check class + configuring one line of rule in admin."** It uses Stage + Context + Check + Rule to set a unified runtime-decision contract; sticks to five principles — fail-closed, zero overhead, data minimization, native multi-tenant isolation, no DSL; V1 proves the skeleton via `pii.regex`, V2 extends capabilities by adapting mature libraries (Presidio / LLM Guard / OpenAI Moderation) — **the framework itself does no detection algorithm**.
