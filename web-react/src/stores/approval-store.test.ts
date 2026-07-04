// @vitest-environment happy-dom
//
// The Zustand layer's own semantics (the §9 transitions themselves are
// tested in lib/approval-cards.test.ts): decide's busy-guard + local
// note stamp, the resolved echo clearing busy, and inbox resilience.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { upsertRequested } from '../lib/approval-cards';
import { useApprovalStore } from './approval-store';
import type { V1WsClient } from '../ws/v1_client';

function fakeWs() {
  return { sendApprovalDecision: vi.fn() } as unknown as V1WsClient;
}

function seedPending(id: string) {
  useApprovalStore.setState((s) => ({
    cards: upsertRequested(s.cards, {
      type: 'event.approval.requested',
      request_id: id,
      expires_at: new Date(Date.now() + 600_000).toISOString(),
    }),
  }));
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      new Response(JSON.stringify({ items: [], total: 0, limit: 100, offset: 0 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    ),
  );
  useApprovalStore.setState({
    conversationId: 'conv-1',
    cards: [],
    busyIds: {},
    highlightId: null,
    inboxRows: [],
    inboxOpen: false,
  });
});

describe('approval store decide()', () => {
  it('sends the frame, marks busy, and stamps a reject note locally', () => {
    const ws = fakeWs();
    useApprovalStore.getState().openConversation('conv-1', ws);
    seedPending('r1');
    useApprovalStore.getState().decide('r1', 'reject', 'too high');
    expect(ws.sendApprovalDecision).toHaveBeenCalledWith('r1', 'reject', 'too high');
    const s = useApprovalStore.getState();
    expect(s.busyIds['r1']).toBe(true);
    expect(s.cards.find((c) => c.requestId === 'r1')?.note).toBe('too high');
  });

  it('busy-guards a second decide until the resolved echo lands', () => {
    const ws = fakeWs();
    useApprovalStore.getState().openConversation('conv-1', ws);
    seedPending('r1');
    useApprovalStore.getState().decide('r1', 'approve');
    useApprovalStore.getState().decide('r1', 'approve');
    expect(ws.sendApprovalDecision).toHaveBeenCalledTimes(1);
    // The echo flips the card, clears busy, and keeps first-wins.
    useApprovalStore.getState().wsResolved({
      type: 'event.approval.resolved',
      request_id: 'r1',
      outcome: 'approved',
    });
    const s = useApprovalStore.getState();
    expect(s.busyIds['r1']).toBeUndefined();
    expect(s.cards[0].status).toBe('approved');
  });

  it('openConversation drops the previous chat\'s cards and busy set', () => {
    const ws = fakeWs();
    useApprovalStore.getState().openConversation('conv-1', ws);
    seedPending('r1');
    useApprovalStore.getState().decide('r1', 'approve');
    useApprovalStore.getState().openConversation('conv-2', ws);
    const s = useApprovalStore.getState();
    expect(s.cards).toEqual([]);
    expect(s.busyIds).toEqual({});
    expect(s.conversationId).toBe('conv-2');
  });

  it('refreshInbox failure keeps the last good rows (badge never breaks)', async () => {
    useApprovalStore.setState({
      inboxRows: [{ request_id: 'keep' } as never],
    });
    vi.stubGlobal('fetch', vi.fn(async () => new Response('boom', { status: 500 })));
    await useApprovalStore.getState().refreshInbox();
    expect(useApprovalStore.getState().inboxRows.length).toBe(1);
  });
});
