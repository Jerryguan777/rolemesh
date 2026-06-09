// Pure state helpers for the HITL approval cards (docs/12-hitl-approval-architecture.md
// §10 S5; rich-card contract in .hitl-ui/spec.md §3 + Appendix C.5). Kept out of
// `chat-panel.ts` so the event→state transitions are unit-testable in isolation,
// without mounting the whole chat surface.
//
// The card list is the SPA's view of in-flight approvals for the *current*
// conversation. Three inputs feed it:
//   - `event.approval.requested`  → a new pending card (idempotent on id)
//   - `event.approval.resolved`   → an existing card flips to its terminal state
//   - `GET /api/v1/approvals/requests` (reconnect) → the authoritative pending set
//
// Every function is pure and returns a fresh array (Lit `@state` change
// detection is reference-based), never mutating the input. The one exception to
// "pure" is `applyResolved` stamping a client-side `resolvedAt`: the wire's
// resolved event carries no timestamp (§3.6 says fall back to client time), so
// we read the wall clock at flip time. That is a deliberate, documented seam.

import type { components } from '../api/generated/types.js';
import type {
  ApprovalRequestedEvent,
  ApprovalResolvedEvent,
} from '../ws/v1_client.js';

export type ApprovalStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'expired'
  | 'cancelled';

/** Provenance of a safety-triggered approval (§3.10). Present (kind
 *  "safety_rule") when a safety check's require_approval verdict raised the
 *  approval; null for a business-policy approval. Forward-compatible: the SPA
 *  handles `safety_rule` explicitly and degrades to nothing on unknown kinds.
 *  NOTE: no producer sets this yet (the safety→approval bridge is unbuilt), so
 *  in practice it is always null today — the indicators below render nothing. */
export type ApprovalTriggeredBy =
  components['schemas']['ApprovalTriggeredBy'] | null;

export interface ApprovalCard {
  requestId: string;
  /** The gated MCP server (`event.approval.requested.mcp_server_name`). */
  mcpServerName: string | null;
  /** The gated tool (`tool_name`). */
  toolName: string | null;
  /** Raw tool arguments — the decision input (§3.3). Defaults to `{}` when the
   *  wire omits it or sends a non-object (the server drops non-dicts upstream,
   *  but we still normalise defensively). */
  params: Record<string, unknown>;
  /** The coworker whose call is gated; used by the card meta line. */
  coworkerId: string | null;
  /** The agent's free-text "why" (§3.4). Null ⇒ the card omits the block. */
  rationale: string | null;
  /** When the approval became pending (ISO-8601), for the "2m ago" meta. */
  requestedAt: string | null;
  /** When the pending approval auto-expires (ISO-8601), drives the countdown. */
  expiresAt: string | null;
  /** Server's one-line hint; kept for narrow surfaces and as a card subtitle. */
  actionSummary: string | null;
  status: ApprovalStatus;
  /** Client-stamped wall-clock (epoch ms) of the terminal flip; null while
   *  pending. The wire's resolved event has no timestamp, so this is our own
   *  record of "decided at" for the resolved header (§3.6). */
  resolvedAt: number | null;
  /** The approver's own rejection note (§3.5), held locally so the resolved
   *  card can echo it back ("YOUR REASON"). Never comes off the wire — it is
   *  what the user typed — and is lost on reconnect, which is acceptable. */
  note: string | null;
  /** Safety-rule provenance (§3.10); null for a business-policy approval. */
  triggeredBy: ApprovalTriggeredBy;
  /** Ordering key (epoch ms) used to interleave the card with chat messages in
   *  chronological position instead of pinning it to the conversation tail. It
   *  is stamped client-side at the instant the card enters the timeline for a
   *  *live* push (so it follows arrival order regardless of client↔server clock
   *  skew), and parsed from the server `requested_at` on reload (so it matches
   *  the server's ordering of the persisted messages around it). Both sources
   *  are monotonic within their own world, which is all the interleave needs. */
  orderTs: number;
}

type PendingRow = components['schemas']['ApprovalRequest'];

/** Normalise a wire `params` value to a plain object. A non-object (array,
 *  string, null, undefined) collapses to `{}` so the card never has to special-
 *  case it — matching the server, which discards non-dict params upstream. */
function coerceParams(p: unknown): Record<string, unknown> {
  if (p && typeof p === 'object' && !Array.isArray(p)) {
    return p as Record<string, unknown>;
  }
  return {};
}

/** Add a pending card for a freshly-requested approval.
 *
 *  Idempotent on `request_id`: a redelivered `event.approval.requested` (the WS
 *  can replay on reconnect) must NOT reset a card that has already resolved
 *  back to `pending` — so a card we've already seen is left exactly as is. */
export function upsertRequested(
  cards: readonly ApprovalCard[],
  ev: ApprovalRequestedEvent,
): ApprovalCard[] {
  if (cards.some((c) => c.requestId === ev.request_id)) {
    return [...cards];
  }
  return [
    ...cards,
    {
      requestId: ev.request_id,
      mcpServerName: ev.mcp_server_name ?? null,
      toolName: ev.tool_name ?? null,
      params: coerceParams(ev.params),
      coworkerId: ev.coworker_id ?? null,
      rationale: ev.rationale ?? null,
      requestedAt: ev.requested_at ?? null,
      expiresAt: ev.expires_at ?? null,
      actionSummary: ev.action_summary ?? null,
      status: 'pending',
      resolvedAt: null,
      note: null,
      triggeredBy: ev.triggered_by ?? null,
      // Live push: stamp arrival time so the card sorts after the user message
      // that triggered it and before the (later) confirmation, even if the
      // server's requested_at clock differs from the browser's.
      orderTs: Date.now(),
    },
  ];
}

/** Flip a card to its terminal status on `event.approval.resolved`.
 *
 *  A resolution for an id we never showed is ignored (no phantom card with a
 *  status but no context). A card that already resolved is left untouched
 *  (first-wins, mirroring the server-side row transition). The flip stamps
 *  `resolvedAt` from the wall clock since the wire carries no timestamp. */
export function applyResolved(
  cards: readonly ApprovalCard[],
  ev: ApprovalResolvedEvent,
): ApprovalCard[] {
  let changed = false;
  const next = cards.map((c) => {
    if (c.requestId !== ev.request_id || c.status !== 'pending') return c;
    changed = true;
    return { ...c, status: ev.outcome, resolvedAt: Date.now() };
  });
  return changed ? next : [...cards];
}

/** Reconcile the card list against the authoritative pending set from REST
 *  (called on (re)connect).
 *
 *  The DB is the source of truth for what is *still pending*. So: keep every
 *  already-resolved card (its terminal state is the user's record of the
 *  decision), and replace the pending subset wholesale with `rows`. A card
 *  that was pending before the disconnect but is absent from `rows` was decided
 *  while we were away — we drop it rather than leave a stale card with live
 *  buttons that would no-op on click. */
export function mergePending(
  cards: readonly ApprovalCard[],
  rows: readonly PendingRow[],
): ApprovalCard[] {
  const resolved = cards.filter((c) => c.status !== 'pending');
  const fromRows: ApprovalCard[] = rows.map((r) => ({
    requestId: r.request_id,
    mcpServerName: r.mcp_server_name ?? null,
    toolName: r.tool_name ?? null,
    params: coerceParams(r.params),
    coworkerId: r.coworker_id ?? null,
    rationale: r.rationale ?? null,
    requestedAt: r.requested_at ?? null,
    expiresAt: r.expires_at ?? null,
    actionSummary: r.action_summary ?? null,
    status: 'pending',
    resolvedAt: null,
    note: null,
    triggeredBy: r.triggered_by ?? null,
    orderTs: r.requested_at ? Date.parse(r.requested_at) : Date.now(),
  }));
  // De-dup defensively: a row whose id already has a resolved card (decided in
  // the same instant the read raced) stays resolved, not re-pended.
  const resolvedIds = new Set(resolved.map((c) => c.requestId));
  return [...resolved, ...fromRows.filter((c) => !resolvedIds.has(c.requestId))];
}

/** Build the full card list from a conversation's authoritative approval record
 *  (`GET /api/v1/conversations/{id}/approval-requests`).
 *
 *  Unlike {@link mergePending} (pending-only, for the inbox / reconnect), this
 *  read carries *every* status plus `decided_at`, so the chat surface can
 *  re-render resolved ✅/❌ cards inline on reload — not just in-flight ones.
 *  `status` is taken verbatim from the row (the server is the source of truth),
 *  `resolvedAt` is parsed from `decided_at` for terminal rows, and `orderTs` is
 *  parsed from `requested_at` so the card lands in chronological position among
 *  the persisted messages. `note` (the approver's typed reason) is client-only
 *  and not persisted, so it is always null here. */
export function cardsFromConversation(
  rows: readonly PendingRow[],
): ApprovalCard[] {
  return rows.map((r) => {
    const status = (r.status as ApprovalStatus) ?? 'pending';
    const decidedAt = r.decided_at ?? null;
    return {
      requestId: r.request_id,
      mcpServerName: r.mcp_server_name ?? null,
      toolName: r.tool_name ?? null,
      params: coerceParams(r.params),
      coworkerId: r.coworker_id ?? null,
      rationale: r.rationale ?? null,
      requestedAt: r.requested_at ?? null,
      expiresAt: r.expires_at ?? null,
      actionSummary: r.action_summary ?? null,
      status,
      resolvedAt:
        status !== 'pending' && decidedAt ? Date.parse(decidedAt) : null,
      note: r.note ?? null,
      triggeredBy: r.triggered_by ?? null,
      orderTs: r.requested_at ? Date.parse(r.requested_at) : 0,
    };
  });
}
