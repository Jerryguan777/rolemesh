"""rolemesh-eval CLI — run.

Manual / nightly tool. Assumes external infrastructure is already up:
PostgreSQL reachable from ``DATABASE_URL`` (coworker lookup only),
NATS reachable from ``NATS_URL``, Docker daemon available. Eval does
not launch the gateway / orchestrator on its own — operators run those
separately if MCP tools or egress filtering matters for the dataset.

Run records live on the filesystem, not in the business database:
Inspect AI writes the canonical ``.eval`` log per run (browse with
``inspect view --log-dir <dir>``; aggregate across runs with
``inspect_ai.analysis``), and this CLI drops a ``<run_id>.run.json``
sidecar next to it carrying what Inspect can't know — the frozen
coworker config snapshot, dataset sha, and rolemesh-side metrics.

Tenant resolution is intentionally strict: ``--tenant`` flag wins over
``ROLEMESH_TENANT_ID`` env, and missing both is fatal. Silently
defaulting under RLS produces zero-row queries that look like "the
coworker doesn't exist" — far worse than a clear error.

Exit codes:
  0 — run completed and (if --threshold given) all thresholds met
  1 — infrastructure / configuration error
  2 — run completed but at least one threshold violated
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Side-effect import: runs load_env() so ``.env`` lands in os.environ
# BEFORE rolemesh.agent.executor's PI_BACKEND module-level constructor
# captures PI_MODEL_ID. Without this, the Pi backend's extra_env freezes
# empty and every Pi-backed eval sample silently no-ops. See
# ``rolemesh.bootstrap`` for the exact ordering rationale.
import rolemesh.bootstrap  # noqa: F401
from rolemesh.core.config import NATS_URL
from rolemesh.core.logger import get_logger
from rolemesh.db import (
    get_coworker,
    get_coworker_by_folder,
    get_user,
    init_database,
    list_coworker_mcp_configs,
)
from rolemesh.evaluation.dataset import load_dataset
from rolemesh.evaluation.freeze import freeze_coworker_config

logger = get_logger()


# ---------------------------------------------------------------------------
# Tenant resolution
# ---------------------------------------------------------------------------


def _resolve_tenant(args: argparse.Namespace) -> str:
    explicit = getattr(args, "tenant", None)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    env = os.environ.get("ROLEMESH_TENANT_ID", "").strip()
    if env:
        return env
    print(
        "ERROR: tenant_id required. Pass --tenant <uuid> or set "
        "ROLEMESH_TENANT_ID=<uuid>.",
        file=sys.stderr,
    )
    raise SystemExit(1)


async def _resolve_coworker(coworker_arg: str, tenant_id: str) -> Any:
    """Look up a coworker by id (UUID) or by folder name."""
    # UUIDs have hyphens at fixed positions; folder names are alnum/dash
    # but are unlikely to be 36 chars with the UUID layout. Cheap test.
    looks_like_uuid = (
        len(coworker_arg) == 36
        and coworker_arg.count("-") == 4
    )
    if looks_like_uuid:
        cw = await get_coworker(coworker_arg, tenant_id=tenant_id)
        if cw is not None:
            return cw
    # Fall back to folder name lookup. Matches what users see on disk
    # under data/tenants/<t>/coworkers/.
    return await get_coworker_by_folder(tenant_id, coworker_arg)


def _user_mode_mcp_servers(mcp_configs: Any) -> list[str]:
    """Names of MCP servers in ``mcp_configs`` that need a user identity.

    ``user`` and ``both`` modes both call out to the credential proxy
    expecting an ``X-RoleMesh-User-Id`` header so an OIDC bearer can
    be looked up; ``service`` mode uses static per-server headers and
    is safe under ``user_id=""``. ``None`` is tolerated (some
    fixtures hand a stub coworker whose binding lookup raised) so the
    --user pre-flight check never crashes on a malformed input.
    """
    return [
        t.name for t in (mcp_configs or [])
        if getattr(t, "auth_mode", None) in ("user", "both")
    ]


# ---------------------------------------------------------------------------
# Metric aggregation
# ---------------------------------------------------------------------------


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    # Inclusive percentile — len(s)-1 to keep the upper bound at the
    # final element; avoids extrapolating past the dataset.
    k = (len(s) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def _aggregate_metrics(
    *,
    inspect_results: Any,
    sample_count: int,
    epochs: int = 1,
) -> dict[str, Any]:
    """Walk per-sample EvalSample objects and produce summary metrics.

    Inspect AI's EvalLog has both top-level ``results.scores`` (one per
    scorer, per reducer when epochs > 1) and per-sample
    ``samples[i].scores`` + ``metadata``. We pull latency / cost out of
    metadata since they're not Inspect scorers, and accuracy from the
    scorer summary. With epochs > 1 the log carries one EvalSample per
    trial, so latency/cost stats naturally cover every trial;
    ``sample_count`` stays the number of distinct dataset samples and
    ``trial_count`` is the expected total executions.
    """
    samples = getattr(inspect_results, "samples", None) or []
    trial_count = sample_count * max(epochs, 1)
    latencies: list[float] = []
    costs: list[float] = []
    cost_seen = 0
    cost_total = 0.0
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    for s in samples:
        meta = getattr(s, "metadata", {}) or {}
        lat = meta.get("latency_ms")
        if isinstance(lat, (int, float)):
            latencies.append(float(lat))
        usage = meta.get("usage") or {}
        if isinstance(usage, dict):
            cost = usage.get("cost_usd")
            if isinstance(cost, (int, float)):
                cost_total += float(cost)
                costs.append(float(cost))
                cost_seen += 1
            for key, var in (
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("cache_read_tokens", "cache_read_tokens"),
                ("cache_write_tokens", "cache_write_tokens"),
            ):
                v = usage.get(key)
                if isinstance(v, (int, float)):
                    if var == "input_tokens":
                        input_tokens += int(v)
                    elif var == "output_tokens":
                        output_tokens += int(v)
                    elif var == "cache_read_tokens":
                        cache_read_tokens += int(v)
                    elif var == "cache_write_tokens":
                        cache_write_tokens += int(v)

    # Pull scorer summaries — Inspect EvalLog.results.scores is a list
    # of EvalScore objects. Each carries ``name`` and ``metrics``; with
    # epochs > 1 the same scorer appears once per reducer, so the key
    # must include the reducer or later entries would silently
    # overwrite earlier ones. Single-epoch logs have reducer=None and
    # keep the bare name — existing --threshold specs stay valid.
    scorer_summary: dict[str, dict[str, Any]] = {}
    results = getattr(inspect_results, "results", None)
    scores_list = getattr(results, "scores", None) if results else None
    for sc in scores_list or []:
        name = getattr(sc, "name", None)
        if not isinstance(name, str):
            continue
        reducer = getattr(sc, "reducer", None)
        key = f"{name}/{reducer}" if isinstance(reducer, str) else name
        metrics_dict: dict[str, Any] = {}
        for m_name, m_val in (getattr(sc, "metrics", {}) or {}).items():
            v = getattr(m_val, "value", m_val)
            if isinstance(v, (int, float)):
                metrics_dict[m_name] = float(v)
        scorer_summary[key] = metrics_dict

    coverage = (cost_seen / trial_count) if trial_count > 0 else 0.0

    return {
        "sample_count": sample_count,
        "epochs": max(epochs, 1),
        "trial_count": trial_count,
        "scorers": scorer_summary,
        "latency_ms": {
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "max": max(latencies) if latencies else None,
        },
        "cost_usd_total": cost_total if cost_seen > 0 else None,
        "cost_usd_coverage": coverage,
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "cache_read": cache_read_tokens,
            "cache_write": cache_write_tokens,
        },
    }


# ---------------------------------------------------------------------------
# Run sidecar
# ---------------------------------------------------------------------------


def _write_run_sidecar(log_dir: Path, record: dict[str, Any]) -> Path:
    """Write ``<run_id>.run.json`` next to the Inspect ``.eval`` log.

    Written twice per run — once at start (status=running) so a crashed
    run still leaves a record of what was attempted, and once at the end
    with metrics. Atomic-ish via temp file + rename so a reader never
    sees a half-written JSON.
    """
    path = log_dir / f"{record['run_id']}.run.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8",
    )
    tmp.rename(path)
    return path


# ---------------------------------------------------------------------------
# Threshold check
# ---------------------------------------------------------------------------


def _check_thresholds(
    metrics: dict[str, Any], thresholds: list[str]
) -> list[str]:
    """Return a list of violation messages (empty = all pass)."""
    failures: list[str] = []
    for raw in thresholds:
        if ">=" not in raw:
            failures.append(f"invalid threshold spec {raw!r}")
            continue
        key, _, value_str = raw.partition(">=")
        key = key.strip()
        try:
            value = float(value_str.strip())
        except ValueError:
            failures.append(f"non-numeric threshold {raw!r}")
            continue
        # Lookup nested keys via dotted path (e.g.
        # ``scorers.final_answer_scorer.accuracy``).
        node: Any = metrics
        for part in key.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            else:
                node = None
                break
        if not isinstance(node, (int, float)):
            failures.append(f"{key}: not present in metrics")
            continue
        if float(node) < value:
            failures.append(f"{key}={node:.4f} < threshold {value:.4f}")
    return failures


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


async def _cmd_run(args: argparse.Namespace) -> int:
    tenant_id = _resolve_tenant(args)

    epochs = int(args.epochs)
    if epochs < 1:
        print("ERROR: --epochs must be >= 1", file=sys.stderr)
        return 1

    dataset = load_dataset(args.dataset)
    print(
        f"Loaded {len(dataset.samples)} samples from {dataset.path} "
        f"(sha256={dataset.sha256[:12]}...)"
    )
    if epochs > 1:
        print(
            f"epochs={epochs}: every sample runs {epochs}x — "
            f"{len(dataset.samples) * epochs} container runs, ~{epochs}x "
            f"API cost. Scores add /mean and /at_least_{epochs} reducers."
        )

    await init_database()

    coworker = await _resolve_coworker(args.coworker, tenant_id)
    if coworker is None:
        print(
            f"ERROR: coworker {args.coworker!r} not found in tenant "
            f"{tenant_id!r}",
            file=sys.stderr,
        )
        return 1

    # Resolve user identity for user-mode MCP authentication. Optional
    # at the CLI level, but **required** if any of the coworker's MCP
    # tools have ``auth_mode in ("user", "both")``: leaving user_id
    # blank in that case makes ``X-RoleMesh-User-Id`` go unset, the
    # credential proxy skips OIDC bearer injection, and the upstream
    # MCP server rejects ``initialize`` — at which point Claude SDK
    # currently hangs forever (Bug 9). Fail-loud upfront beats every
    # sample silently timing out.
    user_id = (args.user or "").strip()
    coworker_mcp_configs = await list_coworker_mcp_configs(
        coworker.id, tenant_id=tenant_id,
    )
    if user_id:
        user = await get_user(user_id, tenant_id=tenant_id)
        if user is None:
            print(
                f"ERROR: user {user_id!r} not found in tenant "
                f"{tenant_id!r}",
                file=sys.stderr,
            )
            return 1
    else:
        offending = _user_mode_mcp_servers(coworker_mcp_configs)
        if offending:
            print(
                f"ERROR: coworker {coworker.folder!r} has user-mode MCP "
                f"servers {offending} but --user was not provided. "
                f"Pass --user <uuid> so the credential proxy can inject "
                f"an OIDC bearer; otherwise every sample will hang on "
                f"the MCP initialize handshake.",
                file=sys.stderr,
            )
            return 1

    frozen = await freeze_coworker_config(coworker.id, tenant_id=tenant_id)

    # Container + NATS setup deferred to import time of inspect_glue,
    # so we only pay the Docker import when actually running a dataset
    # (argparse errors and pre-flight failures stay fast).
    from rolemesh.container.runtime import get_runtime
    from rolemesh.evaluation.inspect_glue import build_eval_task
    from rolemesh.evaluation.runner import EvalRunner
    from rolemesh.ipc.nats_transport import NatsTransport

    runtime = get_runtime()
    await runtime.ensure_available()

    # The egress infrastructure (networks + gateway) is declared by the
    # deployment layer and must already be up; the eval CLI — like the
    # orchestrator — only verifies the invariants and fails closed.
    # Agent DNS pinning is config-driven (EGRESS_GATEWAY_DNS_IP), so no
    # per-process gateway-IP registration is needed anymore.
    await runtime.verify_infrastructure()

    transport = NatsTransport(NATS_URL)
    try:
        await transport.connect()
    except ConnectionError:
        print(
            f"ERROR: failed to connect to NATS at {NATS_URL}. Is the "
            f"NATS server running?",
            file=sys.stderr,
        )
        return 1

    # Cache the resolved coworker so ContainerAgentExecutor doesn't
    # have to re-query the DB on every sample (the executor accepts a
    # callable; we close over the dict-cache here for O(1) lookup).
    coworker_cache: dict[str, Any] = {coworker.id: coworker}
    # MCP configs live in the relation table now (02b dropped the
    # inline JSONB column); cache them alongside the coworker so the
    # executor's ``get_mcp_configs`` callable stays O(1) too.
    mcp_cache: dict[str, list[Any]] = {coworker.id: list(coworker_mcp_configs)}

    def _get_coworker(coworker_id: str) -> Any:
        return coworker_cache.get(coworker_id)

    def _get_mcp_configs(coworker_id: str) -> list[Any]:
        return mcp_cache.get(coworker_id, [])

    log_dir = Path(args.log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    run_id = str(uuid.uuid4())
    started_at = datetime.now(UTC).isoformat()
    record: dict[str, Any] = {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "coworker_id": coworker.id,
        "coworker_folder": coworker.folder,
        "coworker_config_sha256": frozen.sha256,
        "coworker_config": frozen.config,
        "dataset_path": dataset.path,
        "dataset_sha256": dataset.sha256,
        "status": "running",
        "epochs": epochs,
        "started_at": started_at,
        "finished_at": None,
        "metrics": None,
        "eval_log_uri": None,
    }
    try:
        sidecar = _write_run_sidecar(log_dir, record)
        print(f"Run {run_id} (sidecar: {sidecar})")

        runner = EvalRunner(
            runtime=runtime,
            transport=transport,
            get_coworker=_get_coworker,
            get_mcp_configs=_get_mcp_configs,
            run_id=run_id,
            timeout_s=float(args.timeout_s),
            user_id=user_id,
        )

        task = build_eval_task(
            dataset=dataset,
            runner=runner,
            coworker=coworker,
            judge_model=args.judge_model,
            task_name=f"rolemesh-eval-{coworker.folder}",
            epochs=epochs,
        )

        # Late import — Inspect AI is an optional dependency.
        # ``eval_async`` is the in-loop entry point. Using the sync
        # ``eval`` (even via to_thread) spawns a separate anyio loop
        # inside Inspect, and asyncpg / aiohttp resources created on
        # our outer loop blow up when the solver tries to use them
        # — "attached to a different loop" errors at every sample.
        from inspect_ai import eval_async

        # max_samples in inspect-ai controls per-task sample concurrency.
        results_list = await eval_async(
            task,
            log_dir=str(log_dir),
            max_samples=int(args.max_samples_concurrent),
        )
        # inspect_eval returns a list[EvalLog] (one per task).
        result = results_list[0] if results_list else None
        if result is None:
            raise RuntimeError("inspect_ai.eval returned no results")

        metrics = _aggregate_metrics(
            inspect_results=result,
            sample_count=len(dataset.samples),
            epochs=epochs,
        )
        eval_log_uri = getattr(result, "location", None) or str(log_dir)

        record.update(
            status="completed",
            metrics=metrics,
            eval_log_uri=str(eval_log_uri) if eval_log_uri else None,
            finished_at=datetime.now(UTC).isoformat(),
        )
        _write_run_sidecar(log_dir, record)

        if args.json:
            print(json.dumps(
                {"run_id": run_id, "metrics": metrics,
                 "eval_log_uri": str(eval_log_uri)}
                , indent=2))
        else:
            _print_run_summary(run_id, metrics, eval_log_uri)

        violations = _check_thresholds(metrics, args.threshold or [])
        if violations:
            print("\nThreshold violations:", file=sys.stderr)
            for v in violations:
                print(f"  - {v}", file=sys.stderr)
            return 2

        return 0

    except Exception:
        logger.exception("eval run failed")
        try:
            record.update(
                status="failed",
                finished_at=datetime.now(UTC).isoformat(),
            )
            _write_run_sidecar(log_dir, record)
        except Exception:
            logger.exception("failed to write failed-run sidecar")
        return 1
    finally:
        with contextlib.suppress(Exception):
            await transport.close()


def _print_run_summary(
    run_id: str, metrics: dict[str, Any], eval_log_uri: Any,
) -> None:
    print(f"\nrun_id        : {run_id}")
    print(f"eval_log_uri  : {eval_log_uri}")
    print(f"sample_count  : {metrics.get('sample_count')}")
    scorers = metrics.get("scorers") or {}
    for name, vals in scorers.items():
        acc = vals.get("accuracy")
        if isinstance(acc, (int, float)):
            print(f"  {name:30s} accuracy={acc:.4f}")
    lat = metrics.get("latency_ms") or {}
    print(
        f"latency_ms    : p50={lat.get('p50')}, p95={lat.get('p95')}, "
        f"max={lat.get('max')}"
    )
    cost = metrics.get("cost_usd_total")
    cov = metrics.get("cost_usd_coverage", 0.0)
    cost_str = f"${cost:.4f}" if isinstance(cost, (int, float)) else "n/a"
    print(f"cost_usd      : {cost_str} (coverage={cov * 100:.1f}%)")
    print(f"\nView per-sample detail: inspect view {eval_log_uri}")


# ---------------------------------------------------------------------------
# argparse plumbing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rolemesh-eval")
    p.add_argument(
        "--tenant", help="tenant UUID (overrides ROLEMESH_TENANT_ID)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("run", help="run an eval over a dataset")
    pr.add_argument("--coworker", required=True, help="coworker id or folder")
    pr.add_argument("--dataset", required=True, help="path to JSONL dataset")
    pr.add_argument(
        "--max-samples-concurrent", type=int, default=4,
        help="parallel samples (sweet spot 4-8; rate limits dominate)",
    )
    pr.add_argument(
        "--timeout-s", type=int, default=300,
        help="per-sample wall-clock timeout in seconds — hard cap; on "
             "expiry the container is force-stopped and the sample is "
             "marked status=error. Default 300s. Set higher if your "
             "tasks legitimately run long; set lower to cap eval cost "
             "when an upstream hang (e.g. MCP initialize) would "
             "otherwise eat the full IDLE_TIMEOUT.",
    )
    pr.add_argument(
        "--user", default=None,
        help="user UUID to attribute the eval run to. Required when the "
             "coworker has any MCP tool with auth_mode=user/both — eval "
             "fails-loud at start otherwise. Used to set "
             "X-RoleMesh-User-Id so the credential proxy injects the "
             "user's OIDC bearer on outbound MCP calls.",
    )
    pr.add_argument(
        "--epochs", type=int, default=1,
        help="trials per sample (default 1). With N > 1 every sample "
             "runs N times (N x containers and API cost) and scorer "
             "metrics appear once per reducer: .../mean (per-trial "
             "pass rate) and .../at_least_N (all-N-trials-passed rate "
             "- the consistency gate for customer-facing coworkers).",
    )
    pr.add_argument(
        "--threshold", action="append",
        help="threshold spec like 'scorers.final_answer_scorer.accuracy>=0.9' "
             "(with --epochs N, keys gain a reducer suffix, e.g. "
             "'scorers.final_answer_scorer/at_least_5.accuracy>=0.8')",
    )
    pr.add_argument(
        "--judge-model", default=None,
        help="model id for llm_judge mode (default: EVAL_JUDGE_MODEL or "
             "anthropic/claude-sonnet-4-5)",
    )
    pr.add_argument(
        "--log-dir", default="./eval-logs",
        help="directory to write Inspect AI .eval logs and the "
             "<run_id>.run.json sidecar (frozen coworker config, dataset "
             "sha, metrics). Browse runs with `inspect view --log-dir`.",
    )
    pr.add_argument("--json", action="store_true", help="emit JSON summary")
    return p


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    handlers = {"run": _cmd_run}
    handler = handlers.get(args.command)
    if handler is None:
        parser.error(f"unknown command {args.command!r}")
        return 1

    try:
        return asyncio.run(handler(args))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
