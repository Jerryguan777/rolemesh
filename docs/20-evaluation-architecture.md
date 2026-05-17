# Evaluation Architecture

This document describes RoleMesh's evaluation module — the mechanism that lets coworker authors measure, compare, and iterate on the quality of an AI coworker against a domain-specific task dataset.

It covers why evaluation lives outside the business database, why we reuse Inspect AI as a library instead of adopting it whole, the split between the rolemesh-specific runner and the borrowed scoring layer, and the staged rollout that keeps the first cut small.

Target audience: developers adding new scorers, building the CLI commands, debugging why a case scored unexpectedly, integrating a new dataset source, or extending the analysis tooling.

---

## Background: Why an Evaluation Module

A coworker in RoleMesh is the composition of a system prompt, a set of MCP tool bindings, a list of skills, an agent backend, and a permission profile. Once all five are wired up, the only honest question for the author is: **does this thing actually do the job?**

Today there is no quantitative answer. A coworker designer iterates by hand: rewrite a skill, chat with the agent in the WebUI, eyeball the reply, repeat. That loop has three failure modes:

- **No regression signal.** Improving one task often breaks another. Without a baseline to diff against, the breakage is invisible until production.
- **No shared vocabulary.** "It feels better" is not a number that survives a sprint review or a model upgrade.
- **No CI gate.** A model swap, a skill edit, an MCP server change — any of these can silently degrade behaviour. Without an automatic check on a fixed dataset, the regression ships.

What is needed is a way to run a coworker against a curated set of tasks, score the results on the dimensions that matter (did it answer correctly, did it call the right tools, how expensive was it), and compare runs over time. That is what this module provides.

The shape that fits RoleMesh:

- Reuses the existing `ContainerAgentExecutor` so eval runs are bit-identical to production runs.
- Subscribes to the existing `BackendEvent` stream — no new IPC.
- Snapshots the coworker per run so historical runs remain reproducible.
- Stores results outside the business database — eval is a development-time concern, not a tenant business artefact.
- Reuses Inspect AI's scoring primitives and log format, so we get a mature web viewer for free.

---

## Goals

1. **Quantify the two things that matter.** Primary metrics are *result accuracy* (did the agent's reply satisfy the task) and *tool call accuracy* (did it invoke the right MCP tools with the right arguments in the right order). Latency, token count, and cost are secondary but always recorded.
2. **Reproducible runs.** Re-running the same dataset version against the same coworker snapshot must yield comparable scores. Datasets are immutable + versioned; coworker config is snapshotted into the run directory at start.
3. **Compare runs.** The iteration loop demands a diff: "did my skill edit improve `crud` tasks without regressing `query` tasks?". Aggregate deltas plus per-case regression highlighting are first-class.
4. **CI-friendly.** A `--fail-if "result_accuracy<0.7"` flag returns non-zero so the regression suite can gate a merge.
5. **Multi-tenant by construction.** All paths are partitioned by `tenant_id`. Eval data never crosses tenants.
6. **No business DB pollution.** Eval is a dev artefact. It uses the filesystem (and only the filesystem) until a future need proves otherwise.
7. **Reuse, don't rebuild.** Where Inspect AI already has a mature solution (scorer functions, log format, web viewer), use it as a library dependency. Build only what is rolemesh-specific (container orchestration, MCP integration, multi-tenant paths, CLI UX for agent builders).

---

## Non-Goals

- **No WebUI in this iteration.** All interaction is via CLI; deep inspection is delegated to `inspect view` (Inspect AI's bundled web viewer). A rolemesh-native dashboard may follow later but is not in scope here.
- **No production monitoring.** Eval is for the design loop, not for runtime SLO tracking. There is no "eval continuously against live traffic" path.
- **No automatic prompt tuning.** This module measures; it does not optimise.
- **No dataset management UI.** Datasets live in a separate git repository as plain JSONL + YAML manifest, edited with normal git workflow (PR review, tags for versioning). See `Datasets` below.
- **No cross-tenant analytics.** Each tenant analyses its own runs in isolation.

---

## Alternatives Considered

### Framework Choice: Adopt vs Reuse vs Build

#### Option A — Adopt Inspect AI Whole

Define rolemesh evaluations as Inspect AI tasks (`@task`, `@solver`). The orchestration is theirs; we plug in a `solver` that calls our container.

**Pros**

- Inherit their entire ecosystem (scorers, viewer, logs, eval-as-code patterns).
- Smaller surface to maintain.

**Cons**

- Their `solver` model assumes a function that calls a model; our solver needs to spawn a Docker container, set up MCP bindings, wire the credential proxy, drive the NATS event stream. The impedance mismatch is severe.
- Their CLI (`inspect eval task.py`) is designed for safety researchers who write a Python task per evaluation. Our users are agent builders who want declarative datasets and a one-shot `eval run` command.
- No first-class concept of multi-tenant paths, coworker snapshots, or MCP override.

**Rejected.** The fit is poor at the orchestration layer where it matters most.

#### Option B — Adopt LangSmith / Braintrust

Commercial eval platforms with dataset management, run viewer, and diff support.

**Pros**

- Mature UX, hosted infra, no in-house build.

**Cons**

- SaaS-only — sends coworker prompts, tool calls, and (transitively) tenant data to a third party. Incompatible with the on-prem deployment shape RoleMesh targets.
- Vendor lock-in on dataset and run format.
- Hard to integrate with a container-based execution model — the platforms assume their SDK is in the agent process.

**Rejected.** Conflicts with the self-hosted, AGPL-licensed posture of the project.

#### Option C — Build From Scratch

Implement scorers, log format, viewer, all of it in-tree.

**Pros**

- Full control over schema and UX.

**Cons**

- Scoring primitives (exact match, includes, LLM-as-judge with model-graded fact) are well-understood and not worth reimplementing.
- Building a web log viewer is a multi-week project on its own.
- No interop with the broader eval tooling ecosystem.

**Rejected.** Wasteful where Inspect AI already provides solid primitives.

#### Option D — Reuse Inspect AI as a Library, Build the rolemesh-Specific Layer (Chosen)

- Import `inspect_ai.scorer` for scoring functions.
- Write to Inspect AI's `EvalLog` format as a secondary output so `inspect view` works on our runs out of the box.
- Build everything else (orchestrator, recorder, CLI, dataset loader, diff, doctor) in-tree, shaped around rolemesh's execution model.

**Pros**

- Free web viewer via `inspect view <run>/run.eval`.
- Battle-tested scoring primitives.
- Full control over the orchestration layer where our model differs.
- No SaaS dependency.

**Cons**

- Two log formats to keep in sync (the rolemesh canonical JSON and the derived Inspect `.eval`).
- Dependency on a third-party library's evolution.

**Chosen.** Optimal split between buy and build for our shape.

### Result Storage: Database vs Filesystem

The business database holds tenant artefacts: coworkers, conversations, scheduled tasks, audit logs. Adding eval runs to it would mix two unrelated lifecycles — runtime business state versus development-time experiment artefacts. The two also have different access patterns: runs are write-mostly, read-rarely, and (for large transcripts) bulky.

Industry practice (MLflow, W&B, Langfuse, Inspect AI) consistently separates metadata from artefacts and keeps both out of the business OLTP database. Specifically:

- **MLflow** — metadata in a dedicated backing store, artefacts on filesystem or object store.
- **Inspect AI** — single self-describing `.eval` file per run, pure filesystem.
- **Langfuse** — separate Postgres + ClickHouse, never the application DB.

For our scale (tens to hundreds of runs per coworker per month, tens of cases per dataset) a pure filesystem layout suffices. A future need for cross-run aggregation can be served by a side index (DuckDB or SQLite over the JSON files) without ever touching the business Postgres.

**Decision**: `~/.rolemesh/eval/{tenant_id}/runs/{run_id}/` is the canonical storage. One run is one directory; archival, sharing, and cleanup all operate on directories. No DB schema is added.

### Dataset Storage: In-Repo vs External

A dataset is a body of expert-curated tasks that evolves independently of code. Embedding it in the rolemesh repository would couple two lifecycles that should be separate: dataset edits should not require a rolemesh deploy; rolemesh changes should not invalidate a dataset version.

Every mature agent benchmark in the ecosystem (GAIA, SWE-bench, τ-bench, HumanEval, AgentBench) stores datasets in an independent repository or registry — usually plain JSONL plus a manifest, occasionally a HuggingFace Hub entry. None of them embed datasets in the framework code.

**Decision**: Datasets live in an external git repository (e.g. `rolemesh-eval-datasets`). The rolemesh loader resolves URIs of the form `file://path/`, `<local-path>`, or `git+https://repo#subpath@version`. Versioning is by git tag or manifest `version` field. A future HuggingFace Hub loader can be added without protocol changes.

### Judge Location: In-Container vs Host

The LLM-as-judge (when used) is a host-side analysis step, not part of the agent under test. It runs in the rolemesh CLI process, after the agent's run has completed. The credential proxy exists to keep API keys out of agent containers — it is not relevant to host-process code.

**Decision**: The judge uses the official Anthropic SDK directly, reading `ANTHROPIC_API_KEY` from environment or rolemesh's host-side config. It does *not* go through the credential proxy.

---

## Architecture

### Module Layout

```
src/rolemesh/eval/
├── types.py              # Pure dataclasses (Case, Run, CaseResult, Snapshot)
├── paths.py              # Path conventions for run/case files
├── loader/               # DatasetLoader, dispatched by URI scheme
│   ├── local.py          # file:// and local paths
│   └── git.py            # git+https://
├── orchestrator.py       # EvalOrchestrator: per-case dispatch, concurrency, retry
├── case_executor.py      # EvalCaseExecutor: wraps ContainerAgentExecutor
├── recorder.py           # EvalEventRecorder: subscribes BackendEvent, writes case files
├── scorers/              # Scorer Protocol + implementations
│   ├── result.py         # exact / contains / regex / json_match / llm_judge
│   ├── tool_calls.py     # precision / recall / order / args alignment
│   └── perf.py           # latency / tokens / cost
├── judge.py              # LLM-as-judge client (host-side, direct Anthropic SDK)
├── aggregator.py         # Run-level aggregation, groupby tag, failure modes
├── failure_mode.py       # Failure classification
├── inspect_export.py     # Convert to Inspect AI EvalLog format
├── doctor.py             # Pre-flight checks
├── validator.py          # Dataset schema validation
└── cli.py                # `rolemesh eval` subcommand dispatcher
```

Only one change touches existing code: a `UsageEvent` is added to `src/agent_runner/backend.py`'s event union, and both backends emit it. Everything else is additive.

### Data Flow

```
  rolemesh eval run --coworker <id> --dataset <uri>
            │
            ▼
  ┌──────────────────────────────────────────────────────────┐
  │  EvalOrchestrator                                         │
  │   1. Snapshot Coworker (system_prompt, MCP servers,       │
  │      skill file contents, permissions) → run dir.         │
  │   2. DatasetLoader.load(uri) → EvalDataset (immutable).   │
  │   3. asyncio.Semaphore(N) for per-case concurrency.       │
  └────────────┬─────────────────────────────────────────────┘
               │ per case
               ▼
  ┌──────────────────────────────────────────────────────────┐
  │  EvalCaseExecutor                                         │
  │   Construct AgentInput, instantiate ContainerAgentExecutor│
  │   (same code path as production), register EventRecorder, │
  │   wait for terminal status.                               │
  └────────────┬─────────────────────────────────────────────┘
               │ BackendEvent stream
               ▼
  ┌──────────────────────────────────────────────────────────┐
  │  EvalEventRecorder                                        │
  │   ToolUseEvent   → actual_tool_calls (with ts offsets)    │
  │   ResultEvent    → actual_result                          │
  │   UsageEvent     → tokens, cost                           │
  │   monotonic time → latency_ms, ttft_ms                    │
  │   On case complete: write cases/<id>.result.json,         │
  │   cases/<id>.trace.jsonl                                  │
  └────────────┬─────────────────────────────────────────────┘
               │
               ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Scorers (per case)                                       │
  │   ResultScorer        (mode from case.expected_result)    │
  │   ToolCallScorer      (precision / recall / order / args) │
  │   PerformanceScorer   (latency / tokens / cost)           │
  │   FailureModeClassifier                                   │
  └────────────┬─────────────────────────────────────────────┘
               │
               ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Aggregator                                               │
  │   Compose run.json (aggregate metrics, per-tag slice).    │
  │   Write Inspect-compatible run.eval (secondary output).   │
  └──────────────────────────────────────────────────────────┘
```

### Reuse of Existing Infrastructure

The runner does not invent any new execution path. Specifically:

- **`ContainerAgentExecutor`** is instantiated unchanged. Eval runs use the same Docker image, the same MCP credential proxy, the same NATS bridge as production runs.
- **`BackendEvent`** is the only event stream used; the recorder is just a new subscriber.
- **`structlog`** is the only logger.
- **Tenant scoping** uses the existing `tenant_id` propagation through `AgentInput`.

The single new event type, `UsageEvent`, carries token counts and cost per assistant message. It is emitted by both backends (Pi reads from `AssistantMessageEvent.usage`; Claude reads from the SDK's result-message metadata). Existing subscribers ignore it (the `BackendEvent` union is extended, not replaced).

### Run Directory Layout

```
~/.rolemesh/eval/{tenant_id}/runs/{run_id}/
├── run.json                   # Run metadata + aggregate metrics
├── coworker_snapshot.json     # Coworker config snapshot (incl. skill contents)
├── dataset_snapshot.json      # Manifest + sha256 of cases.jsonl
├── cases/
│   ├── <case_id>.result.json  # Per-case scores, perf, actual vs expected
│   └── <case_id>.trace.jsonl  # Raw BackendEvent stream
├── judge/
│   └── <case_id>.judge.json   # LLM judge prompt + response + rationale
├── run.eval                   # Inspect AI compatible format (derived)
└── logs/
    └── orchestrator.log
```

One run is one directory. Sharing a run means tarring a directory; archiving means moving one; cleanup means `rm -rf`.

---

## Datasets

A dataset is a directory in an external git repository:

```
datasets/jira-ops/
├── manifest.yaml
├── cases.jsonl
└── attachments/   # optional auxiliary files
```

The manifest declares dataset metadata, the judge model (optional override), tags, and a `requires_sandbox` flag indicating that running this dataset against a live (non-staging) MCP would mutate production data. Each line in `cases.jsonl` is a case with:

- `prompt` — the task input given to the agent
- `expected_result` — a discriminated union over scoring modes (`exact`, `contains`, `regex`, `json_match`, `llm_judge`)
- `expected_tool_calls` — a list of expected MCP tool invocations with argument matchers (`args_contains`, `args_matches_regex`) and an `order` index
- `tags`, `weight`, `metadata`

Datasets are immutable per version. Editing a case requires bumping the manifest `version` (and ideally a new git tag). The loader records a sha256 of `cases.jsonl` into the run snapshot so reproducibility can be verified post-hoc.

---

## Scoring

### Result Accuracy

The mode is chosen per-case in `expected_result.mode`. The implementations are thin wrappers — `exact`, `contains`, `regex` are small in-tree functions; `json_match` uses `jsonschema`; `llm_judge` defers to `judge.py`, which calls Claude via the official SDK with a fixed prompt template (rubric + expected + actual) and returns a 0-1 score plus a free-text rationale.

The judge audit (prompt, raw response, parsed score, rationale) is written to `judge/<case_id>.judge.json` for inspection.

### Tool Call Accuracy

Four sub-metrics, computed by aligning the recorded `actual_tool_calls` against the case's `expected_tool_calls`:

| Metric | Computation |
|---|---|
| `tool_precision` | `\|hits\| / \|actual\|` — fraction of called tools that were expected |
| `tool_recall` | `\|hits\| / \|expected_required\|` — fraction of required expected tools that were called |
| `tool_order` | LCS length / `\|expected_required\|` — adherence to expected sequencing |
| `tool_args_match` | mean per-call argument match rate, where each call's score is the fraction of `args_contains` / `args_matches_regex` clauses that matched |

The alignment is permissive about extra non-required tools (no precision penalty unless `required=true` is violated) and uses argument matchers rather than literal value equality to tolerate non-deterministic outputs.

### Failure Mode Classification

For each failed case the aggregator emits zero or more failure tags (`missing_tool:<name>`, `extra_tool:<name>`, `wrong_args:<tool>:<key>`, `wrong_order`, `wrong_result`, `timeout`, `error:<prefix>`). The aggregator counts these across the run so the analysis step ("which failure mode dominates?") is direct, not a manual transcript trawl.

### Performance

Recorded verbatim, not scored: `latency_ms` (total), `ttft_ms` (first `ResultEvent`), token counts split into input/output/cache-read/cache-write, and computed `cost_usd` using the per-model price table that already exists in `src/pi/ai/models.py`.

---

## Iteration Loop

The intended use is a five-step analysis loop:

1. **Aggregate view** — look at `run.json` to find the weakest dimension (often `tool_recall` for under-instructed skills).
2. **Tag slice** — group by `case.tags` to find which task family is weakest.
3. **Failure mode** — within that family, look at the failure mode histogram to find the dominant pattern.
4. **Single-case transcript** — open `inspect view <run>/run.eval` on a representative failed case to find the root cause.
5. **Edit, re-run, diff** — modify the skill / system prompt / MCP wiring, re-run on the same dataset version, then `rolemesh eval diff <before> <after>` to verify the fix without regressions.

The whole loop is designed to fit in a single CLI session.

---

## Why Not Just Use the Inspect Viewer

Inspect AI's web viewer is excellent for deep inspection of a single run: full transcript, tool call timeline, scores, side-by-side view of two logs. We use it for exactly that. What it does not provide, and why we still need rolemesh-side CLI:

- **No regression highlighting.** Side-by-side ≠ diff. Inspect can show you two transcripts; it does not tell you "case `jira-031` regressed from 0.85 to 0.65".
- **No pre-flight checks.** Inspect does not know about MCP servers, Docker images, the credential proxy, or coworker configs. A missing API key or an offline MCP is not surfaced until cases start failing.
- **No tag-based aggregation in CLI.** Inspect's viewer can group; its CLI cannot. Agent builders want a one-line summary in a terminal, not a browser.
- **No prescriptive workflow.** Inspect philosophy is "we give you the log, you analyse it in a notebook". rolemesh users want the analysis baked in — `eval diff`, `eval show --groupby tag`, `eval list`.

The split is therefore: Inspect viewer for one-run deep inspection (transcript browsing, tool timeline, judge rationale), rolemesh CLI for run management, pre-flight, aggregation, and comparison.

---

## Phased Rollout

The module ships in three phases. Each phase is independently useful, each commits and tests independently, and the user can stop at any phase without leaving the module in a half-built state.

### Phase 1 — Minimum Viable Evaluation

Goal: end-to-end pipeline that runs a coworker against a local dataset and writes complete result files.

In scope: all data types; local dataset loader; serial orchestrator (no concurrency, no retry); event recorder; `contains` result scorer; full tool-call scorer; performance recording; basic aggregator; `UsageEvent` added to both backends; one CLI command — `rolemesh eval run`. Output goes to JSON files; users inspect with `cat` and `jq`.

Out of scope: everything else.

Approximately 1,600 LOC including tests. The point is to land the execution path early and let users start producing data while the analysis tooling is still being built.

### Phase 2a — Analysis Tooling

Goal: turn the JSON output into a workflow.

Adds `eval doctor`, `eval validate`, `eval show` (with `--case`, `--groupby`), `eval list`, `eval diff`; failure mode classifier; git dataset loader; concurrency with semaphore; retry; result scorer extensions (`exact`, `regex`, `json_match`); `--fail-if` expression; `--mcp-override` and the `requires_sandbox` guard; rich terminal output.

Approximately 2,450 LOC.

### Phase 2b — LLM Judge and Inspect Integration

Goal: subjective scoring and ecosystem interop.

Adds the LLM-as-judge implementation (direct Anthropic SDK, host-side); `llm_judge` result mode; `inspect_export.py` for `run.eval` files; `eval rescore` (re-grade an existing run without re-running the model); `eval bundle` (offline-viewable zip).

Approximately 1,150 LOC.

**Total**: roughly 5,200 LOC across all three phases.

---

## Pitfalls

### Coworker Drift Between Runs

If the coworker is edited between two runs being compared, the diff conflates "I improved the skill" with "the underlying config changed". The snapshot mitigates this for reproducibility (you can verify what config a given run used), but the comparison itself is only meaningful if both runs share the same coworker baseline or the difference is intentional and documented.

The convention is: when iterating, freeze the coworker except for the variable being tested. The snapshot then becomes the audit trail showing what the variable change was.

### Non-Determinism

LLM outputs are non-deterministic. The same coworker against the same dataset will not produce identical scores on two runs. Expect 5–10% noise on aggregate metrics. Per-case scores can vary more. This means:

- Small score differences in a diff are not significant; the regression-detection threshold is configurable.
- Failing CI on tiny score changes is a false-positive generator; `--fail-if` should be set with realistic thresholds.
- For high-stakes comparisons, run each side multiple times and average.

### MCP Side Effects

A dataset that calls `update_issue`, `send_message`, `create_user`, etc., will mutate whatever MCP server the coworker is bound to. Running such a dataset against production is destructive. The `requires_sandbox: true` manifest flag plus `--mcp-override` CLI flag exist for this; they should be used whenever the dataset has any write operations. The default behaviour in Phase 2a refuses to start a run that targets a `requires_sandbox` dataset without an override, with an opt-out for the rare case of intentionally running against production.

### Inspect AI Log Format Coupling

We write to Inspect AI's `EvalLog` format as a secondary output for viewer compatibility. The format is owned by an external project; major-version bumps could break our exporter. The mitigation is that our canonical format is the rolemesh JSON layout — the `.eval` file is derived, regenerable, and skipping it does not lose data. The `inspect_export.py` boundary is small (~200 LOC) and easy to update against a new Inspect AI release.

### Judge Cost and Determinism

Each LLM-as-judge call is a Claude API request. A 200-case run with judge scoring adds ~$2–5 in API cost and 2–5 minutes of wall-clock at typical pricing. The judge runs at `temperature=0.0` but the model itself is non-deterministic; for a noisy borderline case the judge score can flip between runs. `eval rescore` (Phase 2b) lets you re-grade without re-running the model, partially offsetting the cost when rubrics evolve.

---

## Pointers

- Phased work happens on branch `claude/add-rolemesh-evaluation-m7d2L`.
- Coworker model and execution path: `src/rolemesh/core/types.py`, `src/rolemesh/agent/executor.py`, `src/rolemesh/agent/container_executor.py`.
- Backend event protocol that the recorder subscribes to: `src/agent_runner/backend.py`.
- Existing token / cost machinery the recorder reuses: `src/pi/ai/models.py`, `src/pi/ai/types.py`.
- Mock MCP server used by the end-to-end test: `tests/mock_mcp_server.py`.
- Inspect AI documentation: [https://inspect.aisi.org.uk/](https://inspect.aisi.org.uk/) — particularly the Log API and Scorer reference.

Per-coworker auth model and credential proxy behaviour are documented in [`6-auth-architecture.md`](6-auth-architecture.md) and [`7-external-mcp-architecture.md`](7-external-mcp-architecture.md); the evaluation module does not interact with the proxy directly (the judge runs host-side; the agent's MCP calls flow through it exactly as they do in production).
