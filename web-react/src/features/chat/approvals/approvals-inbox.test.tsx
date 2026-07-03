// @vitest-environment happy-dom
//
// Re-expression of web/src/components/approvals-inbox.test.ts for the
// React inbox: badge count/urgency, urgent-first ordering, row
// anatomy, empty state, triage-only, safety shield, and the jump
// callback. The Lit re-fetch-trigger suite maps onto store/effect
// wiring here; the mount/visibility triggers are covered implicitly
// by the fetch stub call counts.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { ApprovalRequest, Coworker } from '../../../api/client';
import { useApprovalStore } from '../../../stores/approval-store';
import { ApprovalsInbox } from './approvals-inbox';

const COWORKERS = [
  { id: 'cw-1', name: 'Rex' },
  { id: 'cw-2', name: 'Mira' },
] as unknown as Coworker[];

function row(id: string, over: Partial<ApprovalRequest> = {}): ApprovalRequest {
  return {
    request_id: id,
    conversation_id: 'conv-1',
    mcp_server_name: 'stripe',
    tool_name: 'charge',
    action_summary: null,
    requested_at: new Date(Date.now() - 60_000).toISOString(),
    expires_at: new Date(Date.now() + 18 * 60_000).toISOString(),
    status: 'pending',
    params: { amount: 6200 },
    coworker_id: 'cw-1',
    ...over,
  } as ApprovalRequest;
}

let serverRows: ApprovalRequest[] = [];

beforeEach(() => {
  serverRows = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      new Response(
        JSON.stringify({ items: serverRows, total: serverRows.length, limit: 100, offset: 0 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    ),
  );
  useApprovalStore.setState({
    conversationId: null,
    cards: [],
    busyIds: {},
    highlightId: null,
    inboxRows: [],
    inboxOpen: false,
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderInbox(over: Partial<Parameters<typeof ApprovalsInbox>[0]> = {}) {
  const onJump = vi.fn();
  const utils = render(
    <ApprovalsInbox
      coworkers={COWORKERS}
      conversations={[]}
      activeChatId={null}
      onJump={onJump}
      {...over}
    />,
  );
  return { onJump, ...utils };
}

describe('ApprovalsInbox badge', () => {
  it('mount refresh seeds the badge from the tenant-wide pending set', async () => {
    serverRows = [row('r1'), row('r2')];
    renderInbox();
    expect((await screen.findByTestId('inbox-badge')).textContent).toBe('2');
    expect(screen.getByTestId('inbox-badge').dataset.urgent).toBe('false');
  });

  it('goes urgent when any item is under 5 minutes', async () => {
    serverRows = [
      row('r1'),
      row('r2', { expires_at: new Date(Date.now() + 60_000).toISOString() }),
    ];
    renderInbox();
    expect((await screen.findByTestId('inbox-badge')).dataset.urgent).toBe('true');
  });

  it('renders no badge when nothing is pending', async () => {
    renderInbox();
    fireEvent.click(screen.getByTestId('inbox-btn'));
    expect(await screen.findByTestId('inbox-empty')).toBeTruthy();
    expect(screen.queryByTestId('inbox-badge')).toBeNull();
  });
});

describe('ApprovalsInbox popover', () => {
  it('sorts rows most-urgent first regardless of input order', async () => {
    serverRows = [
      row('r-late', { tool_name: 'late', expires_at: new Date(Date.now() + 30 * 60_000).toISOString() }),
      row('r-soon', { tool_name: 'soon', expires_at: new Date(Date.now() + 2 * 60_000).toISOString() }),
    ];
    renderInbox();
    await screen.findByTestId('inbox-badge');
    fireEvent.click(screen.getByTestId('inbox-btn'));
    const rows = screen.getAllByTestId('inbox-row');
    expect(rows[0].textContent).toContain('soon');
    expect(rows[0].dataset.urgent).toBe('true');
    expect(rows[1].textContent).toContain('late');
    expect(screen.getByText(/1 expiring soon/)).toBeTruthy();
  });

  it('row anatomy: coworker name, mono tool chip, countdown, params one-liner', async () => {
    serverRows = [row('r1')];
    renderInbox();
    await screen.findByTestId('inbox-badge');
    fireEvent.click(screen.getByTestId('inbox-btn'));
    const r = screen.getByTestId('inbox-row');
    expect(r.textContent).toContain('Rex coworker');
    expect(r.textContent).toContain('stripe.charge');
    expect(r.textContent).toMatch(/\d+m left/);
    expect(r.textContent).toContain('amount: 6200');
  });

  it('omits the params line when params is empty', async () => {
    serverRows = [row('r1', { params: {} })];
    renderInbox();
    await screen.findByTestId('inbox-badge');
    fireEvent.click(screen.getByTestId('inbox-btn'));
    expect(screen.getByTestId('inbox-row').querySelector('.r3')).toBeNull();
  });

  it('is triage-only: no approve/reject controls anywhere', async () => {
    serverRows = [row('r1')];
    renderInbox();
    await screen.findByTestId('inbox-badge');
    fireEvent.click(screen.getByTestId('inbox-btn'));
    const pop = screen.getByTestId('inbox-pop');
    expect(pop.textContent).not.toContain('Approve');
    expect(pop.textContent).not.toContain('Reject');
  });

  it('whole row click jumps and closes the popover', async () => {
    serverRows = [row('r1')];
    const { onJump } = renderInbox();
    await screen.findByTestId('inbox-badge');
    fireEvent.click(screen.getByTestId('inbox-btn'));
    fireEvent.click(screen.getByTestId('inbox-row'));
    expect(onJump).toHaveBeenCalledWith(expect.objectContaining({ request_id: 'r1' }));
    expect(screen.queryByTestId('inbox-pop')).toBeNull();
  });

  it('shows a shield on a safety-triggered row, none for business-policy', async () => {
    serverRows = [
      row('r1', {
        triggered_by: { kind: 'safety_rule', rule_id: 'sr-1', check_id: 'pii.regex' },
      } as Partial<ApprovalRequest>),
      row('r2'),
    ];
    renderInbox();
    await screen.findByTestId('inbox-badge');
    fireEvent.click(screen.getByTestId('inbox-btn'));
    expect(screen.getAllByTestId('inbox-shield').length).toBe(1);
  });
});
