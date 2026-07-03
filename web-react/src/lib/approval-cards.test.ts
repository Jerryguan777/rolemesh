import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  type ApprovalCard,
  applyResolved,
  cardsFromConversation,
  mergePending,
  upsertRequested,
} from './approval-cards';
import type {
  ApprovalRequestedEvent,
  ApprovalResolvedEvent,
} from '../ws/v1_client';
import type { components } from '../api/generated/types';

type PendingRow = components['schemas']['ApprovalRequest'];

function requested(
  id: string,
  extra: Partial<ApprovalRequestedEvent> = {},
): ApprovalRequestedEvent {
  return {
    type: 'event.approval.requested',
    request_id: id,
    action_summary: 'do thing',
    expires_at: '2026-01-01T00:00:00Z',
    ...extra,
  };
}

function resolved(
  id: string,
  outcome: 'approved' | 'rejected' | 'expired' | 'cancelled',
): ApprovalResolvedEvent {
  return { type: 'event.approval.resolved', request_id: id, outcome };
}

function row(id: string, extra: Partial<PendingRow> = {}): PendingRow {
  return {
    request_id: id,
    conversation_id: null,
    mcp_server_name: 'stripe',
    tool_name: 'charge',
    action_summary: 's',
    requested_at: '2026-01-01T00:00:00Z',
    expires_at: '2026-01-01T00:05:00Z',
    status: 'pending',
    decided_at: null,
    note: null,
    ...extra,
  };
}

/** A resolved card seed with all required fields populated. */
function seed(id: string, status: ApprovalCard['status']): ApprovalCard {
  return {
    requestId: id,
    mcpServerName: 'stripe',
    toolName: 'charge',
    params: {},
    coworkerId: null,
    rationale: null,
    requestedAt: null,
    expiresAt: null,
    actionSummary: 's',
    status,
    resolvedAt: null,
    note: null,
    triggeredBy: null,
    orderTs: 0,
  };
}

describe('upsertRequested', () => {
  it('adds a pending card for a new request', () => {
    const out = upsertRequested([], requested('a', { action_summary: 'charge $500' }));
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      requestId: 'a',
      actionSummary: 'charge $500',
      status: 'pending',
    });
  });

  it('maps the new decision-relevant fields off the requested event', () => {
    const out = upsertRequested(
      [],
      requested('a', {
        mcp_server_name: 'amazon-ads-api',
        tool_name: 'campaign.pause',
        params: { campaign_id: 'SP-1', amount: 3210 },
        coworker_id: 'cw-7',
        rationale: 'ROAS below floor',
        requested_at: '2026-05-31T10:00:00Z',
      }),
    );
    expect(out[0]).toMatchObject({
      mcpServerName: 'amazon-ads-api',
      toolName: 'campaign.pause',
      params: { campaign_id: 'SP-1', amount: 3210 },
      coworkerId: 'cw-7',
      rationale: 'ROAS below floor',
      requestedAt: '2026-05-31T10:00:00Z',
      resolvedAt: null,
      note: null,
    });
  });

  it('defaults params to {} when the field is missing', () => {
    const out = upsertRequested([], requested('a'));
    expect(out[0].params).toEqual({});
  });

  it('coerces a non-object params (array) to {} rather than passing it through', () => {
    // The server drops non-dict params upstream; the store must not surface an
    // array to the card, which would render Object.entries garbage.
    const ev = requested('a', {
      params: [1, 2, 3] as unknown as Record<string, unknown>,
    });
    expect(upsertRequested([], ev)[0].params).toEqual({});
  });

  it('is idempotent — a redelivered requested event does not duplicate', () => {
    const once = upsertRequested([], requested('a'));
    const twice = upsertRequested(once, requested('a'));
    expect(twice).toHaveLength(1);
  });

  it('carries triggered_by through (safety provenance §3.10)', () => {
    const tb = {
      kind: 'safety_rule' as const,
      rule_id: 'sr-1',
      check_id: 'presidio.pii',
      stage: 'post_tool_result' as const,
    };
    const out = upsertRequested([], requested('a', { triggered_by: tb }));
    expect(out[0].triggeredBy).toEqual(tb);
  });

  it('defaults triggeredBy to null for a business-policy approval', () => {
    expect(upsertRequested([], requested('a'))[0].triggeredBy).toBeNull();
  });

  it('does NOT revert a resolved card back to pending on redelivery', () => {
    // The WS can replay event.approval.requested on reconnect. If that
    // re-pended an already-decided card, the user would see live buttons on a
    // request the server has already closed — a real double-decision risk.
    const out = upsertRequested([seed('a', 'approved')], requested('a'));
    expect(out).toHaveLength(1);
    expect(out[0].status).toBe('approved');
  });

  it('coerces a missing action_summary to null', () => {
    const ev = { type: 'event.approval.requested', request_id: 'a' } as
      ApprovalRequestedEvent;
    const out = upsertRequested([], ev);
    expect(out[0].actionSummary).toBeNull();
    expect(out[0].rationale).toBeNull();
    expect(out[0].params).toEqual({});
  });
});

describe('applyResolved', () => {
  afterEach(() => vi.useRealTimers());

  it('flips a pending card to its outcome', () => {
    const seeded = upsertRequested([], requested('a'));
    const out = applyResolved(seeded, resolved('a', 'rejected'));
    expect(out[0].status).toBe('rejected');
  });

  it('flips on the cancelled outcome (container withdrew the call)', () => {
    const seeded = upsertRequested([], requested('a'));
    const out = applyResolved(seeded, resolved('a', 'cancelled'));
    expect(out[0].status).toBe('cancelled');
  });

  it('stamps resolvedAt from the wall clock on the flip', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-31T12:00:00Z'));
    const seeded = upsertRequested([], requested('a'));
    const out = applyResolved(seeded, resolved('a', 'approved'));
    expect(out[0].resolvedAt).toBe(Date.parse('2026-05-31T12:00:00Z'));
  });

  it('ignores a resolution for an unknown id (no phantom card)', () => {
    const out = applyResolved([], resolved('ghost', 'approved'));
    expect(out).toEqual([]);
  });

  it('is first-wins — does not re-resolve an already-terminal card', () => {
    const out = applyResolved([seed('a', 'approved')], resolved('a', 'expired'));
    expect(out[0].status).toBe('approved');
  });

  it('only touches the matching card', () => {
    const seeded = upsertRequested(
      upsertRequested([], requested('a')),
      requested('b'),
    );
    const out = applyResolved(seeded, resolved('a', 'approved'));
    expect(out.find((c) => c.requestId === 'a')?.status).toBe('approved');
    expect(out.find((c) => c.requestId === 'b')?.status).toBe('pending');
  });
});

describe('mergePending', () => {
  it('replaces the pending set with the authoritative rows', () => {
    const before = upsertRequested([], requested('stale'));
    const out = mergePending(before, [row('fresh')]);
    expect(out.map((c) => c.requestId)).toEqual(['fresh']);
    expect(out[0].status).toBe('pending');
  });

  it('maps the new fields off the REST pending row', () => {
    const out = mergePending(
      [],
      [
        row('a', {
          params: { order_id: 'ord_1' },
          coworker_id: 'cw-2',
          rationale: 'why',
        }),
      ],
    );
    expect(out[0]).toMatchObject({
      params: { order_id: 'ord_1' },
      coworkerId: 'cw-2',
      rationale: 'why',
      mcpServerName: 'stripe',
      toolName: 'charge',
    });
  });

  it('drops a pending card decided while disconnected (absent from rows)', () => {
    // Card 'a' was pending; it was decided server-side while the socket was
    // down, so it is absent from the reconnect read. Leaving it would show a
    // dead card with live buttons.
    const before = upsertRequested([], requested('a'));
    const out = mergePending(before, []);
    expect(out).toEqual([]);
  });

  it('keeps already-resolved cards (the user record of the decision)', () => {
    const out = mergePending([seed('done', 'rejected')], [row('new')]);
    expect(out.find((c) => c.requestId === 'done')?.status).toBe('rejected');
    expect(out.find((c) => c.requestId === 'new')?.status).toBe('pending');
  });

  it('keeps a cancelled card across reconnect', () => {
    const out = mergePending([seed('c', 'cancelled')], [row('new')]);
    expect(out.find((c) => c.requestId === 'c')?.status).toBe('cancelled');
  });

  it('does not re-pend a row whose id already has a resolved card', () => {
    const out = mergePending([seed('a', 'approved')], [row('a')]);
    expect(out).toHaveLength(1);
    expect(out[0].status).toBe('approved');
  });
});

describe('cardsFromConversation', () => {
  it('preserves a resolved row status verbatim (not forced to pending)', () => {
    // This is the whole point of the full-conversation read vs mergePending:
    // a row that the server records as rejected must re-render as a rejected
    // card on reload, with no live action buttons.
    const out = cardsFromConversation([
      row('a', { status: 'rejected', decided_at: '2026-01-01T00:02:00Z' }),
    ]);
    expect(out[0].status).toBe('rejected');
  });

  it('parses decided_at into resolvedAt for a terminal row', () => {
    const out = cardsFromConversation([
      row('a', { status: 'approved', decided_at: '2026-01-01T00:02:00Z' }),
    ]);
    expect(out[0].resolvedAt).toBe(Date.parse('2026-01-01T00:02:00Z'));
  });

  it('leaves resolvedAt null for a still-pending row even if decided_at leaks in', () => {
    // Defensive: a pending row should never carry a decided_at, but if the API
    // ever sends one we must not paint a pending card as already-decided.
    const out = cardsFromConversation([
      row('a', { status: 'pending', decided_at: '2026-01-01T00:02:00Z' }),
    ]);
    expect(out[0].resolvedAt).toBeNull();
  });

  it('orders the card by requested_at, not decided_at', () => {
    // The card belongs where the request was raised (right after the user's
    // message), not where it was decided — decided_at can be much later.
    const out = cardsFromConversation([
      row('a', {
        status: 'approved',
        requested_at: '2026-01-01T00:00:00Z',
        decided_at: '2026-01-01T09:00:00Z',
      }),
    ]);
    expect(out[0].orderTs).toBe(Date.parse('2026-01-01T00:00:00Z'));
  });

  it('defaults status to pending when the row omits it', () => {
    const r = row('a');
    delete (r as { status?: string }).status;
    const out = cardsFromConversation([r]);
    expect(out[0].status).toBe('pending');
  });
});
