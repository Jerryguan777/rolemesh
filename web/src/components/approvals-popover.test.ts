// @vitest-environment happy-dom
// <rm-approvals-popover> — pure view. Pin observable behaviour:
//   * empty state when rows=[]
//   * one inline-approval per row, capped at POPOVER_MAX_ROWS
//   * overflow indicator surfaces when rows exceed the cap
//   * connectionStatus≠'open' triggers the stale hint
//   * "View all" link dispatches rm-popover-navigate so the parent
//     can close the popover before the route change

import { afterEach, describe, expect, it } from 'vitest';

import './approvals-popover.js';
import {
  POPOVER_MAX_ROWS,
  type RmApprovalsPopover,
} from './approvals-popover.js';
import type { ApprovalRequest, Me } from '../api/client.js';

const ME: Me = {
  user_id: 'u-1',
  tenant_id: 't1',
  name: 'Jerry',
  email: 'j@example.com',
  role: 'owner',
};

function makeApproval(
  id: string,
  approvers: string[] = ['u-1'],
): ApprovalRequest {
  return {
    id,
    tenant_id: 't1',
    job_id: `job-${id}`,
    mcp_server_name: 'fs',
    coworker_id: 'cw-a',
    conversation_id: 'conv-1',
    user_id: 'u-2',
    source: 'proposal',
    post_exec_mode: 'report',
    status: 'pending',
    requested_at: '2026-05-23T00:00:00Z',
    expires_at: '2026-05-24T00:00:00Z',
    created_at: '2026-05-23T00:00:00Z',
    updated_at: '2026-05-23T00:00:00Z',
    actions: [{ tool_name: 'echo', params: {} }],
    resolved_approvers: approvers,
  } as unknown as ApprovalRequest;
}

async function settle(el: RmApprovalsPopover): Promise<void> {
  for (let i = 0; i < 10; i += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

async function mount(props: Partial<RmApprovalsPopover>): Promise<RmApprovalsPopover> {
  const el = document.createElement('rm-approvals-popover') as RmApprovalsPopover;
  Object.assign(el, props);
  document.body.appendChild(el);
  await settle(el);
  return el;
}

describe('<rm-approvals-popover>', () => {
  afterEach(() => {
    document
      .querySelectorAll('rm-approvals-popover')
      .forEach((el) => el.remove());
  });

  it('renders the empty state when there are no rows', async () => {
    const el = await mount({ rows: [], me: ME, loading: false });
    expect(el.querySelector('[data-testid="approval-empty"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="approval-row"]')).toBeNull();
  });

  it('renders a loading hint while the initial fetch is in flight', async () => {
    const el = await mount({ rows: [], me: ME, loading: true });
    expect(el.querySelector('[data-testid="approval-loading"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="approval-empty"]')).toBeNull();
  });

  it('renders one <rm-inline-approval> per row', async () => {
    const rows = [makeApproval('a'), makeApproval('b'), makeApproval('c')];
    const el = await mount({ rows, me: ME });
    const cards = el.querySelectorAll('rm-inline-approval');
    expect(cards.length).toBe(3);
  });

  it('caps the list at POPOVER_MAX_ROWS and surfaces overflow', async () => {
    // Ten rows; cap is 5 — overflow indicator must read "+5".
    const rows = Array.from({ length: POPOVER_MAX_ROWS + 5 }, (_, i) =>
      makeApproval(`a-${i}`),
    );
    const el = await mount({ rows, me: ME });
    expect(el.querySelectorAll('rm-inline-approval').length).toBe(
      POPOVER_MAX_ROWS,
    );
    const overflow = el.querySelector('[data-testid="approval-overflow"]');
    expect(overflow).not.toBeNull();
    expect(overflow?.textContent).toContain(String(rows.length - POPOVER_MAX_ROWS));
  });

  it('shows the stale-hint when the WS is not open', async () => {
    const el = await mount({
      rows: [makeApproval('a')],
      me: ME,
      connectionStatus: 'reconnecting',
    });
    expect(el.querySelector('[data-testid="approvals-stale"]')).not.toBeNull();
  });

  it('hides the stale-hint while the WS is open', async () => {
    const el = await mount({
      rows: [makeApproval('a')],
      me: ME,
      connectionStatus: 'open',
    });
    expect(el.querySelector('[data-testid="approvals-stale"]')).toBeNull();
  });

  it('the "View all" link dispatches rm-popover-navigate so the parent can close', async () => {
    const el = await mount({ rows: [makeApproval('a')], me: ME });
    const link = el.querySelector<HTMLAnchorElement>(
      '[data-testid="approvals-view-all"]',
    )!;
    const events: CustomEvent[] = [];
    el.addEventListener('rm-popover-navigate', (e) =>
      events.push(e as CustomEvent),
    );
    link.click();
    expect(events.length).toBe(1);
    expect(events[0].detail.hash).toBe('#/activity/approvals');
  });

  it('hides decide buttons when the signed-in user is not an approver', async () => {
    // Row's approvers list contains someone else — popover should
    // surface the row read-only. The inline-approval component owns
    // the actual hide; we pin that the parent (popover) hands canDecide
    // correctly via the row's `resolved_approvers` check.
    const row = makeApproval('a', ['someone-else']);
    const el = await mount({ rows: [row], me: ME });
    const card = el.querySelector('rm-inline-approval');
    // `.canDecide` is a property binding; check the attribute reflects.
    // inline-approval reflects canDecide=true → can-decide attribute;
    // false → attribute absent.
    expect(card?.hasAttribute('can-decide')).toBe(false);
  });
});
