# Frontdesk Agent v1.2 — Implementation Plan

This directory holds the implementation plan for the Frontdesk feature
on `feat/frontdesk`. The work is split across **3 phases**, each meant
to be done in **its own fresh Claude Code session**.

## File map

| File | What it is |
|---|---|
| [`handbook.md`](handbook.md) | Source of truth. Full v1.2 design + 9-step plan + verified facts + 35 pitfalls. Every phase doc references it. |
| [`phase-a-foundation.md`](phase-a-foundation.md) | Phase A scope (Step 1-3): DB schema + DB helpers + `_coworker_from_state` fix + ToolContext NATS RPC. Includes the session prompt. |
| [`phase-b-delegation-core.md`](phase-b-delegation-core.md) | Phase B scope (Step 4-6): `list_agents` + `delegate_to_agent` handler + catalog injection. 22-test matrix. Includes the session prompt. |
| [`phase-c-integration.md`](phase-c-integration.md) | Phase C scope (Step 7-9): WebUI admin + channel-level approval fan-out + routing eval + docs update. Includes the session prompt. |

## How to run a phase

1. Open the phase doc (`phase-X-*.md`).
2. Copy the **"Session Prompt"** block at the top.
3. Start a fresh Claude Code session in this repo (`/clear`, or new
   terminal) and paste it as the first message.
4. Let the session work through its commits. Each phase produces a
   small number of commits on `feat/frontdesk`; nothing is pushed
   automatically.
5. Review the diff. If satisfied, move to the next phase.

## Why split into 3 phases

Total scope is ~4,200 LOC (1,400 prod + 2,200 tests + 500 docs +
schema). Detailed reasoning is in the chat thread that produced this
plan, but in brief:

- **Phase A** completes a latent-bug fix (`_coworker_from_state`) and
  the schema groundwork. The repo is healthier after Phase A even if
  Frontdesk never ships. Small enough to fit easily in one session.
- **Phase B** is the heart: the delegation handler plus its
  22-scenario test matrix. Roughly half the total LOC. It deserves a
  dedicated session so the implementer is not splitting attention
  across schema work or UI plumbing.
- **Phase C** glues the feature to the real product surfaces (WebUI
  admin, approval fan-out, eval, docs). Naturally cohesive and
  smaller than Phase B.

Doing all three in one session is technically possible but invites
context compaction in the middle of Phase B's most delicate code (Pi
two-event merge, OUTER_GUARD vs business-timeout split, sticky
concurrency race, role_config NATS interception). The three-phase
split is the conservative call.

## Phase ordering and dependencies

Phase A must complete before Phase B (B needs the schema columns and
the `_coworker_from_state` fix). Phase B must complete before Phase C
(C's UI shows delegations, C's approval fan-out tests need real child
convs).

Each phase ends with the branch in a green-and-mergeable state — a
human can stop after Phase A if they need to pause. Phase B alone
gives a functional CLI-style frontdesk; Phase C is what makes it
production-ready.

## Source of truth disagreements

If a phase doc and `handbook.md` disagree on a detail, **the handbook
wins**. Open a ticket / ask the user; don't silently follow the phase
doc.
