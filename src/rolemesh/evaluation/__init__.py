"""Rolemesh evaluation framework.

Inspect-AI based eval runner that ships configurations of an existing
Coworker through the production container path. Manual / nightly only:
not invoked from PR CI, not loaded by the main runtime.

Run records live on the filesystem: Inspect AI's ``.eval`` log per run
plus a ``<run_id>.run.json`` sidecar (frozen coworker config, dataset
sha, metrics) written by the CLI — no business-database tables.

Public surface:
  - rolemesh.evaluation.dataset: JSONL loader
  - rolemesh.evaluation.freeze: coworker_config snapshot
  - rolemesh.evaluation.runner: per-sample container execution
  - rolemesh.evaluation.scorers: final_answer (outcome-only)
  - rolemesh.evaluation.cli: argparse entry (rolemesh-eval)
"""
