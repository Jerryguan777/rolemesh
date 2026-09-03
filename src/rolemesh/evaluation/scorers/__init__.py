"""Inspect-AI scorers for the rolemesh eval framework.

One axis — final_answer (pass/fail correctness of the outcome).
Latency / token spend are not scorers; they ride along in sample
metadata and are aggregated post-run. Tool-call traces are recorded
in the .eval log for triage (``inspect view``) but not graded: the
framework scores outcomes, not paths.
"""

from rolemesh.evaluation.scorers.final_answer import final_answer_scorer

__all__ = ["final_answer_scorer"]
