// @vitest-environment happy-dom
// Approvals queue page — covers:
//   * GET /api/v1/approvals?scope=mine&status=pending on mount
//   * empty state when list is empty
//   * row renders inline approval with the right wire fields
//   * scope=all button toggles the query

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const listSpy = vi.fn();
const getMeSpy = vi.fn();
const decideSpy = vi.fn();

vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  return {
    ...actual,
    getApiClient: () => ({
      listApprovals: listSpy,
      getMe: getMeSpy,
      decideApproval: decideSpy,
    }),
  };
});

import { ApprovalsPage } from './approvals-page.js';

async function waitUntilLoaded(page: ApprovalsPage): Promise<void> {
  for (let i = 0; i < 30; i++) {
    await Promise.resolve();
    await page.updateComplete;
    // @ts-expect-error — touching private state for assertion
    if (page.loading === false) return;
  }
  throw new Error('ApprovalsPage did not finish loading');
}

function makeRow(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: 'a-' + Math.random().toString(36).slice(2, 8),
    tenant_id: 't1',
    coworker_id: 'cw1',
    conversation_id: 'conv1',
    policy_id: 'p1',
    user_id: 'alice-uuid',
    job_id: 'job-1',
    mcp_server_name: 'erp',
    actions: [
      {
        mcp_server: 'erp',
        tool_name: 'refund',
        params: { amount: 500 },
      },
    ],
    action_hashes: ['h1'],
    rationale: 'refund',
    source: 'proposal',
    status: 'pending',
    post_exec_mode: 'report',
    resolved_approvers: ['bob-uuid'],
    requested_at: '2026-05-21T12:00:00Z',
    expires_at: '2026-05-21T13:00:00Z',
    created_at: '2026-05-21T12:00:00Z',
    updated_at: '2026-05-21T12:00:00Z',
    ...overrides,
  };
}

describe('ApprovalsPage', () => {
  let page: ApprovalsPage;

  beforeEach(async () => {
    listSpy.mockReset();
    getMeSpy.mockReset();
    decideSpy.mockReset();
    getMeSpy.mockResolvedValue({
      user_id: 'bob-uuid',
      tenant_id: 't1',
      role: 'owner',
    });
    listSpy.mockResolvedValue([]);
    page = new ApprovalsPage();
    document.body.appendChild(page);
    await waitUntilLoaded(page);
  });

  afterEach(() => {
    page.remove();
  });

  it('lists pending approvals with scope=mine by default', () => {
    expect(listSpy).toHaveBeenCalledWith({
      scope: 'mine',
      status: 'pending',
    });
    // Empty state shown when there are no rows.
    expect(page.innerHTML).toMatch(/No pending approvals/i);
  });

  it('renders one inline-approval per row', async () => {
    listSpy.mockResolvedValueOnce([
      makeRow({ id: 'a1' }),
      makeRow({ id: 'a2', resolved_approvers: ['someone-else'] }),
    ]);
    await (page as unknown as { refresh: () => Promise<void> }).refresh();
    await page.updateComplete;
    const cards = page.querySelectorAll('rm-inline-approval');
    expect(cards.length).toBe(2);
    // The first row (bob is approver) should mount with can-decide;
    // the second row (someone else) should NOT.
    const ids = Array.from(cards).map((c) =>
      c.getAttribute('approval-id'),
    );
    expect(ids).toEqual(['a1', 'a2']);
  });

  it('toggles scope to all and re-fetches', async () => {
    listSpy.mockResolvedValueOnce([]);
    const allBtn = Array.from(page.querySelectorAll('button')).find(
      (b) => (b.textContent ?? '').trim() === 'All',
    ) as HTMLButtonElement | undefined;
    expect(allBtn).toBeTruthy();
    allBtn!.click();
    await Promise.resolve();
    await Promise.resolve();
    await page.updateComplete;
    expect(listSpy).toHaveBeenLastCalledWith({
      scope: 'all',
      status: 'pending',
    });
  });

  it('refreshes the list after rm-approval-decided bubbles', async () => {
    listSpy.mockResolvedValueOnce([makeRow({ id: 'a1' })]);
    await (page as unknown as { refresh: () => Promise<void> }).refresh();
    await page.updateComplete;
    listSpy.mockReset();
    listSpy.mockResolvedValue([]);
    // Bubble the event from inside the card.
    const card = page.querySelector('rm-inline-approval')!;
    card.dispatchEvent(
      new CustomEvent('rm-approval-decided', {
        detail: { approvalId: 'a1', status: 'approved' },
        bubbles: true,
        composed: true,
      }),
    );
    await Promise.resolve();
    await Promise.resolve();
    await page.updateComplete;
    expect(listSpy).toHaveBeenCalled();
  });
});
