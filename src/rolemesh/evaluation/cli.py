"""rolemesh-eval CLI — run / list / show.

Manual / nightly tool. Assumes external infrastructure is already up:
PostgreSQL reachable from ``DATABASE_URL``, NATS reachable from
``NATS_URL``, Docker daemon available (for ``run``). Eval does not
launch the gateway / orchestrator on its own — operators run those
separately if MCP tools or egress filtering matters for the dataset.

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
from datetime import UTC
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
)
from rolemesh.evaluation.dataset import load_dataset
from rolemesh.evaluation.freeze import freeze_coworker_config
from rolemesh.evaluation.store import (
    finalize_eval_run,
    get_eval_run,
    list_eval_runs,
)

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
    # Fall back to folder name lookup. Matches what users see in
    # ``rolemesh-eval list`` and on disk under data/tenants/<t>/coworkers/.
    return await get_coworker_by_folder(tenant_id, coworker_arg)


def _user_mode_mcp_servers(coworker: Any) -> list[str]:
    """Names of MCP servers on this coworker that need a user identity.

    ``user`` and ``both`` modes both call out to the credential proxy
    expecting an ``X-RoleMesh-User-Id`` header so an OIDC bearer can
    be looked up; ``service`` mode uses static per-server headers and
    is safe under ``user_id=""``.
    """
    return [
        t.name for t in (coworker.tools or [])
        if t.auth_mode in ("user", "both")
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
) -> dict[str, Any]:
    """Walk per-sample EvalSample objects and produce summary metrics.

    Inspect AI's EvalLog has both top-level ``results.scores`` (one per
    scorer) and per-sample ``samples[i].scores`` + ``metadata``. We
    pull latency / cost out of metadata since they're not Inspect
    scorers, and accuracy from the scorer summary.
    """
    samples = getattr(inspect_results, "samples", None) or []
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
    # of EvalScore objects. Each carries ``name`` and ``metrics`` dict.
    scorer_summary: dict[str, dict[str, Any]] = {}
    results = getattr(inspect_results, "results", None)
    scores_list = getattr(results, "scores", None) if results else None
    for sc in scores_list or []:
        name = getattr(sc, "name", None)
        if not isinstance(name, str):
            continue
        metrics_dict: dict[str, Any] = {}
        for m_name, m_val in (getattr(sc, "metrics", {}) or {}).items():
            v = getattr(m_val, "value", m_val)
            if isinstance(v, (int, float)):
                metrics_dict[m_name] = float(v)
        scorer_summary[name] = metrics_dict

    coverage = (cost_seen / sample_count) if sample_count > 0 else 0.0

    return {
        "sample_count": sample_count,
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

    dataset = load_dataset(args.dataset)
    print(
        f"Loaded {len(dataset.samples)} samples from {dataset.path} "
        f"(sha256={dataset.sha256[:12]}...)"
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
        offending = _user_mode_mcp_servers(coworker)
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
    # so we only pay the Docker import when actually running. The CLI's
    # other subcommands (list / show) don't need this.
    from rolemesh.container.runtime import get_runtime
    from rolemesh.egress.bootstrap import ensure_gateway_running_and_register_dns
    from rolemesh.evaluation.inspect_glue import build_eval_task
    from rolemesh.evaluation.runner import EvalRunner
    from rolemesh.ipc.nats_transport import NatsTransport

    runtime = get_runtime()
    await runtime.ensure_available()

    # In EC-2 mode the agent containers want their DNS pointed at the
    # egress gateway so DNS-exfil filtering applies. The orchestrator
    # daemon registers the gateway IP at startup; when eval runs as a
    # standalone CLI process it has its own runner module and would
    # otherwise spawn containers with Docker's default resolver,
    # silently losing DNS-level egress protection. The helper is
    # idempotent — reuses a running gateway, only relaunches if none
    # exists — so calling it alongside a live orchestrator is safe.
    # Returns None silently when EC-2 isn't active (rollback mode).
    await ensure_gateway_running_and_register_dns(runtime)

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

    def _get_coworker(coworker_id: str) -> Any:
        return coworker_cache.get(coworker_id)

    log_dir = Path(args.log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    run_row = None
    try:
        run_row = await _create_run_row(
            tenant_id=tenant_id,
            coworker_id=coworker.id,
            frozen_config=frozen.config,
            frozen_sha=frozen.sha256,
            dataset_path=dataset.path,
            dataset_sha=dataset.sha256,
        )
        print(f"Created eval_runs row {run_row.id} (status=running)")

        runner = EvalRunner(
            runtime=runtime,
            transport=transport,
            get_coworker=_get_coworker,
            run_id=run_row.id,
            timeout_s=float(args.timeout_s),
            user_id=user_id,
        )

        task = build_eval_task(
            dataset=dataset,
            runner=runner,
            coworker=coworker,
            judge_model=args.judge_model,
            task_name=f"rolemesh-eval-{coworker.folder}",
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
            inspect_results=result, sample_count=len(dataset.samples),
        )
        eval_log_uri = getattr(result, "location", None) or str(log_dir)

        await finalize_eval_run(
            run_row.id,
            tenant_id=tenant_id,
            status="completed",
            metrics=metrics,
            eval_log_uri=str(eval_log_uri) if eval_log_uri else None,
        )

        if args.json:
            print(json.dumps(
                {"run_id": run_row.id, "metrics": metrics,
                 "eval_log_uri": str(eval_log_uri)}
                , indent=2))
        else:
            _print_run_summary(run_row.id, metrics, eval_log_uri)

        violations = _check_thresholds(metrics, args.threshold or [])
        if violations:
            print("\nThreshold violations:", file=sys.stderr)
            for v in violations:
                print(f"  - {v}", file=sys.stderr)
            return 2

        return 0

    except Exception:
        logger.exception("eval run failed")
        if run_row is not None:
            try:
                await finalize_eval_run(
                    run_row.id,
                    tenant_id=tenant_id,
                    status="failed",
                    metrics=None,
                    eval_log_uri=None,
                )
            except Exception:
                logger.exception("failed to mark eval_run failed")
        return 1
    finally:
        with contextlib.suppress(Exception):
            await transport.close()


async def _create_run_row(
    *,
    tenant_id: str,
    coworker_id: str,
    frozen_config: dict[str, Any],
    frozen_sha: str,
    dataset_path: str,
    dataset_sha: str,
) -> Any:
    from rolemesh.evaluation.store import create_eval_run

    return await create_eval_run(
        tenant_id=tenant_id,
        coworker_id=coworker_id,
        coworker_config=frozen_config,
        coworker_config_sha256=frozen_sha,
        dataset_path=dataset_path,
        dataset_sha256=dataset_sha,
    )


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


async def _cmd_list(args: argparse.Namespace) -> int:
    tenant_id = _resolve_tenant(args)
    await init_database()

    coworker_id: str | None = None
    if args.coworker:
        cw = await _resolve_coworker(args.coworker, tenant_id)
        if cw is None:
            print(
                f"ERROR: coworker {args.coworker!r} not found",
                file=sys.stderr,
            )
            return 1
        coworker_id = cw.id

    runs = await list_eval_runs(
        tenant_id=tenant_id, coworker_id=coworker_id, limit=args.limit,
    )
    if args.json:
        out = [
            {
                "id": r.id,
                "coworker_id": r.coworker_id,
                "status": r.status,
                "started_at": r.started_at.isoformat(),
                "finished_at": (
                    r.finished_at.isoformat() if r.finished_at else None
                ),
                "metrics": r.metrics,
                "dataset_path": r.dataset_path,
                "config_sha256": r.coworker_config_sha256,
            }
            for r in runs
        ]
        print(json.dumps(out, indent=2))
        return 0

    if not runs:
        print("(no eval runs)")
        return 0

    print(
        f"{'run_id':36s}  {'status':10s}  {'started':20s}  "
        f"{'accuracy':>9s}  {'cost_usd':>10s}"
    )
    for r in runs:
        ts = r.started_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        acc_val = "-"
        cost_val = "-"
        if isinstance(r.metrics, dict):
            scorers = r.metrics.get("scorers") or {}
            fa = scorers.get("final_answer_scorer") or {}
            if isinstance(fa.get("accuracy"), (int, float)):
                acc_val = f"{fa['accuracy']:.4f}"
            cost = r.metrics.get("cost_usd_total")
            if isinstance(cost, (int, float)):
                cost_val = f"${cost:.4f}"
        print(
            f"{r.id:36s}  {r.status:10s}  {ts:20s}  "
            f"{acc_val:>9s}  {cost_val:>10s}"
        )
    return 0


async def _cmd_show(args: argparse.Namespace) -> int:
    tenant_id = _resolve_tenant(args)
    await init_database()
    run = await get_eval_run(args.run_id, tenant_id=tenant_id)
    if run is None:
        print(f"ERROR: run {args.run_id} not found", file=sys.stderr)
        return 1

    if args.format == "json":
        out = {
            "id": run.id,
            "tenant_id": run.tenant_id,
            "coworker_id": run.coworker_id,
            "status": run.status,
            "dataset_path": run.dataset_path,
            "dataset_sha256": run.dataset_sha256,
            "coworker_config_sha256": run.coworker_config_sha256,
            "coworker_config": run.coworker_config,
            "metrics": run.metrics,
            "eval_log_uri": run.eval_log_uri,
            "started_at": run.started_at.isoformat(),
            "finished_at": (
                run.finished_at.isoformat() if run.finished_at else None
            ),
        }
        print(json.dumps(out, indent=2))
        return 0

    print(f"run_id          : {run.id}")
    print(f"status          : {run.status}")
    print(f"started         : {run.started_at.isoformat()}")
    if run.finished_at:
        print(f"finished        : {run.finished_at.isoformat()}")
    print(f"coworker_id     : {run.coworker_id}")
    print(f"config_sha256   : {run.coworker_config_sha256}")
    print(f"dataset_path    : {run.dataset_path}")
    print(f"dataset_sha256  : {run.dataset_sha256}")
    print(f"eval_log_uri    : {run.eval_log_uri}")
    if isinstance(run.metrics, dict):
        print("\nmetrics:")
        print(json.dumps(run.metrics, indent=2))
    return 0


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
        "--threshold", action="append",
        help="threshold spec like 'scorers.final_answer_scorer.accuracy>=0.9'",
    )
    pr.add_argument(
        "--judge-model", default=None,
        help="model id for llm_judge mode (default: EVAL_JUDGE_MODEL or "
             "anthropic/claude-sonnet-4-5)",
    )
    pr.add_argument(
        "--log-dir", default="./eval-logs",
        help="directory to write Inspect AI .eval logs",
    )
    pr.add_argument("--json", action="store_true", help="emit JSON summary")

    pl = sub.add_parser("list", help="list past eval runs")
    pl.add_argument(
        "--coworker", default=None, help="filter by coworker id or folder",
    )
    pl.add_argument("--limit", type=int, default=20)
    pl.add_argument("--json", action="store_true")

    ps = sub.add_parser("show", help="show details of a single run")
    ps.add_argument("run_id", help="eval_run UUID")
    ps.add_argument(
        "--format", choices=("text", "json"), default="text",
    )
    return p


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    handlers = {"run": _cmd_run, "list": _cmd_list, "show": _cmd_show}
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
