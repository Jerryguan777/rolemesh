// @vitest-environment happy-dom
// Chat panel + inline approval bridge — pins the contract that:
//   1. event.approval.required → spawns a card keyed by approval_id
//   2. event.approval.resolved → mutates that card's status
//   3. Cards survive a token frame interleave without being clobbered
//
// We bypass connectedCallback() side-effects by driving handleV1Event
// directly; the panel constructor is enough to wire @state defaults.

import { describe, expect, it } from 'vitest';
import { ChatPanel } from './chat-panel.js';

interface Internals {
  approvals: Map<string, { status: string; toolName: string }>;
  me: { user_id: string } | null;
  handleV1Event(e: Record<string, unknown>): void;
}

function makePanel(): { panel: ChatPanel; i: Internals } {
  const panel = new ChatPanel();
  const i = panel as unknown as Internals;
  i.me = { user_id: 'bob-uuid' };
  return { panel, i };
}

describe('ChatPanel approval bridge', () => {
  it('spawns an inline card when event.approval.required arrives', () => {
    const { i } = makePanel();
    i.handleV1Event({
      type: 'event.approval.required',
      approval_id: 'a1',
      run_id: 'r1',
      summary: {
        tool_name: 'refund',
        mcp_server_name: 'erp',
        args: { amount: 500 },
      },
    });
    expect(i.approvals.size).toBe(1);
    const row = i.approvals.get('a1')!;
    expect(row.status).toBe('pending');
    expect(row.toolName).toBe('refund');
  });

  it('updates status when event.approval.resolved fires (approve→approved)', () => {
    const { i } = makePanel();
    i.handleV1Event({
      type: 'event.approval.required',
      approval_id: 'a1',
      run_id: 'r1',
      summary: { tool_name: 'refund', mcp_server_name: 'erp', args: {} },
    });
    i.handleV1Event({
      type: 'event.approval.resolved',
      approval_id: 'a1',
      decision: 'approve',
      actor_user_id: 'bob-uuid',
    });
    expect(i.approvals.get('a1')!.status).toBe('approved');
  });

  it('updates status when event.approval.resolved fires (deny→denied)', () => {
    const { i } = makePanel();
    i.handleV1Event({
      type: 'event.approval.required',
      approval_id: 'a1',
      summary: { tool_name: 'refund', mcp_server_name: 'erp', args: {} },
    });
    i.handleV1Event({
      type: 'event.approval.resolved',
      approval_id: 'a1',
      decision: 'deny',
    });
    expect(i.approvals.get('a1')!.status).toBe('denied');
  });

  it('ignores resolved for an unseen approval (silent no-op)', () => {
    const { i } = makePanel();
    i.handleV1Event({
      type: 'event.approval.resolved',
      approval_id: 'unknown',
      decision: 'approve',
    });
    expect(i.approvals.size).toBe(0);
  });

  it('does not mutate approvals on unrelated event.run.token', () => {
    const { i } = makePanel();
    i.handleV1Event({
      type: 'event.approval.required',
      approval_id: 'a1',
      summary: { tool_name: 'refund', mcp_server_name: 'erp', args: {} },
    });
    const before = i.approvals.get('a1');
    i.handleV1Event({
      type: 'event.run.token',
      run_id: 'r1',
      delta: 'hello',
    });
    expect(i.approvals.get('a1')).toBe(before);
  });

  it('maps expired and cancelled wire decisions', () => {
    const { i } = makePanel();
    i.handleV1Event({
      type: 'event.approval.required',
      approval_id: 'a1',
      summary: { tool_name: 't', mcp_server_name: 's', args: {} },
    });
    i.handleV1Event({
      type: 'event.approval.required',
      approval_id: 'a2',
      summary: { tool_name: 't', mcp_server_name: 's', args: {} },
    });
    i.handleV1Event({
      type: 'event.approval.resolved',
      approval_id: 'a1',
      decision: 'expired',
    });
    i.handleV1Event({
      type: 'event.approval.resolved',
      approval_id: 'a2',
      decision: 'cancelled',
    });
    expect(i.approvals.get('a1')!.status).toBe('expired');
    expect(i.approvals.get('a2')!.status).toBe('cancelled');
  });
});
