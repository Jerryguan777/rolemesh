// Approval store — D-9 lands here: Zustand's first consumer, because
// this state is genuinely cross-cutting (chat cards + top-bar badge +
// inbox popover read it; the chat stream hook and REST hydration write
// it). The §9 state machine itself stays framework-free in
// lib/approval-cards.ts — this store owns WHEN those transitions run.
//
// TWO state blocks, deliberately (corrected from spec O.6's "one map"
// against the shipped Lit architecture): the WS is per-conversation,
// so `cards` (WS + conversation-record driven) can only ever cover the
// ACTIVE conversation, while the tenant-wide `inboxRows` MUST be
// REST-fed (the Lit inbox's explicit-trigger design — its header
// comment says the store is "deliberately NOT lifted to the shell").
// The Lit `approval-activity` bubble becomes a direct refreshInbox()
// call here.
//
// Shipped Lit semantics carried over verbatim (chat-panel.ts):
//   - open + (re)connect → refresh from the conversation's FULL record
//     (cardsFromConversation), preserving local rejection notes — the
//     note never returns on the wire.
//   - decide(): busy until the `event.approval.resolved` echo lands
//     (double-tap guard); the frame carries only id + verb + note (the
//     approver identity is stamped server-side from the WS ticket).
//   - The SPA never self-decides an outcome — countdown expiry changes
//     nothing until the server's `expired` push.

import { create } from 'zustand';
import {
  applyResolved,
  cardsFromConversation,
  upsertRequested,
  type ApprovalCard,
} from '../lib/approval-cards';
import { getApiClient, type ApprovalRequest } from '../api/client';
import type {
  ApprovalRequestedEvent,
  ApprovalResolvedEvent,
  V1WsClient,
} from '../ws/v1_client';

/** The active conversation's WS client (owned by the stream hook, not
 *  the store — held module-level so setting it never re-renders). */
let ws: V1WsClient | null = null;

interface ApprovalStoreState {
  /** Conversation the `cards` block belongs to. */
  conversationId: string | null;
  /** The active conversation's cards — pending AND resolved (§3.8). */
  cards: ApprovalCard[];
  /** Decision frames awaiting their resolved echo (double-tap guard). */
  busyIds: Record<string, true>;
  /** Card to pulse + auto-expand (inbox jump / see-decision link). */
  highlightId: string | null;
  /** Tenant-wide pending rows — the badge + popover data (REST-fed). */
  inboxRows: ApprovalRequest[];
  /** Popover visibility — store-held so the resolved card's
   *  "← Back to inbox" link can open it from inside the stream. */
  inboxOpen: boolean;

  /** Conversation switch: bind the block, drop the old chat's cards,
   *  adopt the new WS client, and hydrate from the full record. */
  openConversation(conversationId: string | null, client: V1WsClient | null): void;
  /** Re-render the card list from the conversation's authoritative
   *  record (open + reconnect); local rejection notes survive. */
  refreshCards(): Promise<void>;
  wsRequested(ev: ApprovalRequestedEvent): void;
  wsResolved(ev: ApprovalResolvedEvent): void;
  /** Relay a card decision to the orchestrator (busy-guarded). */
  decide(requestId: string, decision: 'approve' | 'reject', note?: string): void;
  /** Re-pull the tenant-wide pending set (the 5 Lit inbox triggers). */
  refreshInbox(): Promise<void>;
  setHighlight(id: string | null): void;
  setInboxOpen(open: boolean): void;
}

export const useApprovalStore = create<ApprovalStoreState>((set, get) => ({
  conversationId: null,
  cards: [],
  busyIds: {},
  highlightId: null,
  inboxRows: [],
  inboxOpen: false,

  openConversation(conversationId, client) {
    ws = client;
    set({ conversationId, cards: [], busyIds: {}, highlightId: null });
    if (conversationId) void get().refreshCards();
  },

  async refreshCards() {
    const conversationId = get().conversationId;
    if (!conversationId) return;
    try {
      const rows = await getApiClient().listConversationApprovalRequests(
        conversationId,
      );
      // Guard against a conversation switch mid-flight.
      if (get().conversationId !== conversationId) return;
      const localNotes = new Map(
        get()
          .cards.filter((c) => c.note)
          .map((c) => [c.requestId, c.note] as const),
      );
      set({
        cards: cardsFromConversation(rows).map((c) =>
          localNotes.has(c.requestId)
            ? { ...c, note: localNotes.get(c.requestId) ?? null }
            : c,
        ),
      });
    } catch (err) {
      console.warn('listConversationApprovalRequests failed', err);
    }
  },

  wsRequested(ev) {
    set((s) => ({ cards: upsertRequested(s.cards, ev) }));
    // §4.8 trigger C — the Lit approval-activity bubble.
    void get().refreshInbox();
  },

  wsResolved(ev) {
    set((s) => {
      const busyIds = { ...s.busyIds };
      delete busyIds[ev.request_id];
      return { cards: applyResolved(s.cards, ev), busyIds };
    });
    void get().refreshInbox();
  },

  decide(requestId, decision, note) {
    if (!ws || get().busyIds[requestId]) return;
    set((s) => {
      const next: Partial<ApprovalStoreState> = {
        busyIds: { ...s.busyIds, [requestId]: true as const },
      };
      // Stash the rejection note locally so the resolved card can echo
      // it back ("YOUR REASON") — it never returns on the wire.
      if (decision === 'reject' && note) {
        next.cards = s.cards.map((c) =>
          c.requestId === requestId ? { ...c, note } : c,
        );
      }
      return next;
    });
    ws.sendApprovalDecision(requestId, decision, note);
  },

  async refreshInbox() {
    try {
      const rows = await getApiClient().listPendingApprovalRequests();
      set({ inboxRows: rows });
    } catch (err) {
      // Resilient — the badge keeps its last good value; the next
      // trigger retries.
      console.warn('listPendingApprovalRequests failed', err);
    }
  },

  setHighlight(highlightId) {
    set({ highlightId });
  },

  setInboxOpen(inboxOpen) {
    set({ inboxOpen });
    if (inboxOpen) void get().refreshInbox();
  },
}));
