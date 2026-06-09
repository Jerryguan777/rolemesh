"""HITL tool-approval — pure policy matching (docs/12-hitl-approval-architecture.md).

This package holds the dependency-free policy primitives shared by the
container hook and the orchestrator. Keep it free of DB / NATS / I/O
imports so both sides can use it without dragging in the other's stack.
"""
