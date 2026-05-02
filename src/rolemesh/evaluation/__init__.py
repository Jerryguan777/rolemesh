"""Rolemesh evaluation framework.

Inspect-AI based eval runner that ships configurations of an existing
Coworker through the production container path. Manual / nightly only:
not invoked from PR CI, not loaded by the main runtime.

Public surface:
  - rolemesh.evaluation.store: eval_runs DB ops
  - rolemesh.evaluation.dataset: JSONL loader
  - rolemesh.evaluation.freeze: coworker_config snapshot
  - rolemesh.evaluation.runner: per-sample container execution
  - rolemesh.evaluation.scorers: final_answer / tool_trace / cost
  - rolemesh.evaluation.cli: argparse entry (rolemesh-eval)
"""
