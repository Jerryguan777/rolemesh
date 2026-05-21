"""Inspect-AI scorers for the rolemesh eval framework.

Four orthogonal axes — final_answer (pass/fail correctness),
tool_trace (pass/fail tool-call shape), routing_accuracy (pass/fail
on frontdesk's delegate_to_agent target choice — frontdesk v1.2),
and cost (transparent passthrough, aggregated post-run).
"""

from rolemesh.evaluation.scorers.final_answer import final_answer_scorer
from rolemesh.evaluation.scorers.routing_accuracy import routing_accuracy_scorer
from rolemesh.evaluation.scorers.tool_trace import tool_trace_scorer

__all__ = ["final_answer_scorer", "routing_accuracy_scorer", "tool_trace_scorer"]
