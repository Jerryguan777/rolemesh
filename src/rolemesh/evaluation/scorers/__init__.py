"""Inspect-AI scorers for the rolemesh eval framework.

Two orthogonal axes — final_answer (pass/fail correctness) and
tool_trace (pass/fail tool-call shape). Latency / token spend are not
scorers; they ride along in sample metadata and are aggregated
post-run.
"""

from rolemesh.evaluation.scorers.final_answer import final_answer_scorer
from rolemesh.evaluation.scorers.tool_trace import tool_trace_scorer

__all__ = ["final_answer_scorer", "tool_trace_scorer"]
