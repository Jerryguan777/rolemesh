# Red-team baselines — consolidated summary

This directory archives red-team reference snapshots; future runs diff against
them. This index is the **complete picture of what we currently know** — but the
two kinds of evidence below are very different in strength, so they are kept
separate on purpose. **Do not read the P1 section as a measured scorecard.**

---

## 1. poison-mcp (ASI04) — LIVE validation (strong evidence)

Full record: [`2026-06-29-poison-mcp.md`](2026-06-29-poison-mcp.md).

- Chain + **WEAK ruler live-verified**; `audit_log` executes (not parked).
- **Headline finding:** this staging stack has **no `PRE_TOOL_CALL` `tool_input`
  scan**, so an ASI04 STRONG exfil through a *bound* tool is unblocked by default
  (confirmed by `safety_rules` inspection).
- **No live STRONG** was producible — Claude refused the cross-user read
  upstream (4/4 benign reads, 0 self-`audit_log`: strong model-layer resistance).

## 2. P1 plugin scan — CALIBRATION ONLY (qualitative, NOT a scorecard)

8 plugins: `bola`, `bfla`, `rbac`, `pii`, `tool-discovery`, `excessive-agency`,
`cross-session-leak`, `ssrf`, with scoped `base64`/`crescendo`.

> ⚠️ **No clean full-scan scorecard exists.** P1 shipped at `numTests=2`
> (calibration) and was never raised to ~8, and bring-up was dominated by
> broken-chain noise. The points below are the **qualitative read-out** from the
> calibration run (provider README + the Run-2 commits), **not** a measured
> pass/fail table. Treat as "what we observed", not "the defense rate".

- **`tool-discovery` — P1's only real finding.** The agent can be induced to
  enumerate available tools/servers. (Candidate to back-fill into `attack_sim`;
  deliberately **not pursued** — see roadmap.)
- **`base64` mostly stopped by Anthropic's Usage-Policy layer (`model_aup`), not
  RoleMesh** — low signal for the tool layer; retargeted to `bola` only.
- **`crescendo` is not true multi-turn** — the provider opens a new conversation
  per call, so "held N rounds" *understates* exposure; `defended_by` also comes
  through empty on crescendo cases. Real multi-turn is deferred (roadmap).
- **`bola`/`bfla` need promptfoo remote generation** — disabling it yields zero
  cases for the two most important plugins.
- **Rig caveat (service mode):** the sandbox MCPs use a static `X-Actor` header,
  which **bypasses RoleMesh's dynamic per-user token path**. So a cross-user MCP
  hit exercises the *deliberately-vulnerable sandbox* + a talked-into call — it
  does **not** test RoleMesh's real per-user/tenant isolation.
- **Scoring guard:** 12 empty-completion cases were being mis-scored as PASS
  (inflating the defense rate); fixed by the broken-chain guard + `defended_by`.

---

## Gap — how to get a real P1 scorecard

A true full baseline needs a clean `promptfoo redteam run` at `numTests ≈ 8` on a
staging stack (with an egress allow-rule per bound MCP — see the run steps).
**Not done here, deliberately**, to keep this branch's wrap-up minimal. When run,
archive it as `baselines/<date>-p1-scan.md` and add a row to section 2 above.
