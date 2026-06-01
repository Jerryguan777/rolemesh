import { describe, expect, it } from 'vitest';

import {
  type ApprovalCard,
  applyResolved,
  mergePending,
  upsertRequested,
} from './approval-store.js';
import type {
  ApprovalRequestedEvent,
  ApprovalResolvedEvent,
} from '../ws/v1_client.js';
import type { components } from '../api/generated/types.js';

type PendingRow = components['schemas']['PendingApprovalRequest'];

function requested(
  id: string,
  summary: string | null = 'do thing',
): ApprovalRequestedEvent {
  return {
    type: 'event.approval.requested',
    request_id: id,
    action_summary: summary,
    expires_at: '2026-01-01T00:00:00Z',
  };
}

function resolved(
  id: string,
  outcome: 'approved' | 'rejected' | 'expired',
): ApprovalResolvedEvent {
  return { type: 'event.approval.resolved', request_id: id, outcome };
}

function row(id: string, summary: string | null = 's'): PendingRow {
  return {
    request_id: id,
    conversation_id: null,
    mcp_server_name: 'stripe',
    tool_name: 'charge',
    action_summary: summary,
    requested_at: '2026-01-01T00:00:00Z',
    expires_at: '2026-01-01T00:05:00Z',
  };
}

describe('upsertRequested', () => {
  it('adds a pending card for a new request', () => {
    const out = upsertRequested([], requested('a', 'charge $500'));
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      requestId: 'a',
      actionSummary: 'charge $500',
      status: 'pending',
    });
  });

  it('is idempotent — a redelivered requested event does not duplicate', () => {
    const once = upsertRequested([], requested('a'));
    const twice = upsertRequested(once, requested('a'));
    expect(twice).toHaveLength(1);
  });

  it('does NOT revert a resolved card back to pending on redelivery', () => {
    // The WS can replay event.approval.requested on reconnect. If that
    // re-pended an already-decided card, the user would see live buttons on a
    // request the server has already closed — a real double-decision risk.
    const seeded: ApprovalCard[] = [
      { requestId: 'a', actionSummary: 's', expiresAt: null, status: 'approved' },
    ];
    const out = upsertRequested(seeded, requested('a'));
    expect(out).toHaveLength(1);
    expect(out[0].status).toBe('approved');
  });

  it('coerces a missing action_summary to null', () => {
    const ev = { type: 'event.approval.requested', request_id: 'a' } as
      ApprovalRequestedEvent;
    expect(upsertRequested([], ev)[0].actionSummary).toBeNull();
  });
});

describe('applyResolved', () => {
  it('flips a pending card to its outcome', () => {
    const seeded = upsertRequested([], requested('a'));
    const out = applyResolved(seeded, resolved('a', 'rejected'));
    expect(out[0].status).toBe('rejected');
  });

  it('ignores a resolution for an unknown id (no phantom card)', () => {
    const out = applyResolved([], resolved('ghost', 'approved'));
    expect(out).toEqual([]);
  });

  it('is first-wins — does not re-resolve an already-terminal card', () => {
    const seeded: ApprovalCard[] = [
      { requestId: 'a', actionSummary: 's', expiresAt: null, status: 'approved' },
    ];
    const out = applyResolved(seeded, resolved('a', 'expired'));
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

  it('drops a pending card decided while disconnected (absent from rows)', () => {
    // Card 'a' was pending; it was decided server-side while the socket was
    // down, so it is absent from the reconnect read. Leaving it would show a
    // dead card with live buttons.
    const before = upsertRequested([], requested('a'));
    const out = mergePending(before, []);
    expect(out).toEqual([]);
  });

  it('keeps already-resolved cards (the user record of the decision)', () => {
    const seeded: ApprovalCard[] = [
      { requestId: 'done', actionSummary: 's', expiresAt: null, status: 'rejected' },
    ];
    const out = mergePending(seeded, [row('new')]);
    expect(out.find((c) => c.requestId === 'done')?.status).toBe('rejected');
    expect(out.find((c) => c.requestId === 'new')?.status).toBe('pending');
  });

  it('does not re-pend a row whose id already has a resolved card', () => {
    const seeded: ApprovalCard[] = [
      { requestId: 'a', actionSummary: 's', expiresAt: null, status: 'approved' },
    ];
    const out = mergePending(seeded, [row('a')]);
    expect(out).toHaveLength(1);
    expect(out[0].status).toBe('approved');
  });
});
