"""Inspect-AI scorers for the rolemesh eval framework.

Outcome-only, two axes: answer_check (Inspect's model_graded_qa judging
the final reply against the sample's ``target`` criterion) and — for
state-mutating datasets — state_check (environment-state probes).
Latency / token spend are not scorers; they ride along in sample
metadata and are aggregated post-run. Tool-call traces are recorded in
the .eval log for triage but never graded: outcomes, not paths.
"""

from rolemesh.evaluation.scorers.answer_check import answer_check
from rolemesh.evaluation.scorers.state_check import state_check

__all__ = ["answer_check", "state_check"]
