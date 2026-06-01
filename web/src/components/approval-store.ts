// Pure state helpers for the HITL approval cards (docs/21-hitl-approval-plan.md
// §10 S5). Kept out of `chat-panel.ts` so the event→state transitions are unit-
// testable in isolation, without mounting the whole chat surface.
//
// The card list is the SPA's view of in-flight approvals for the *current*
// conversation. Three inputs feed it:
//   - `event.approval.requested`  → a new pending card (idempotent on id)
//   - `event.approval.resolved`   → an existing card flips to its terminal state
//   - `GET /api/v1/approval-requests` (reconnect) → the authoritative pending set
//
// Every function is pure and returns a fresh array (Lit `@state` change
// detection is reference-based), never mutating the input.

import type { components } from '../api/generated/types.js';
import type {
  ApprovalRequestedEvent,
  ApprovalResolvedEvent,
} from '../ws/v1_client.js';

export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'expired';

export interface ApprovalCard {
  requestId: string;
  actionSummary: string | null;
  expiresAt: string | null;
  status: ApprovalStatus;
}

type PendingRow = components['schemas']['PendingApprovalRequest'];

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
      actionSummary: ev.action_summary ?? null,
      expiresAt: ev.expires_at ?? null,
      status: 'pending',
    },
  ];
}

/** Flip a card to its terminal status on `event.approval.resolved`.
 *
 *  A resolution for an id we never showed is ignored (no phantom card with a
 *  status but no context). A card that already resolved is left untouched
 *  (first-wins, mirroring the server-side row transition). */
export function applyResolved(
  cards: readonly ApprovalCard[],
  ev: ApprovalResolvedEvent,
): ApprovalCard[] {
  let changed = false;
  const next = cards.map((c) => {
    if (c.requestId !== ev.request_id || c.status !== 'pending') return c;
    changed = true;
    return { ...c, status: ev.outcome };
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
    actionSummary: r.action_summary ?? null,
    expiresAt: r.expires_at,
    status: 'pending',
  }));
  // De-dup defensively: a row whose id already has a resolved card (decided in
  // the same instant the read raced) stays resolved, not re-pended.
  const resolvedIds = new Set(resolved.map((c) => c.requestId));
  return [...resolved, ...fromRows.filter((c) => !resolvedIds.has(c.requestId))];
}
