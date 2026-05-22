// @vitest-environment happy-dom
// Inline approval component — covers:
//   * decide button click → typed ApiClient.decideApproval invoked
//   * approve/reject buttons hidden when can-decide=false
//   * imperative setStatus(...) reflects in DOM
//
// Anti-mirror: we don't reach into private rendering logic; we
// assert visible DOM + the public spy on the typed client.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const decideSpy = vi.fn();

vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  return {
    ...actual,
    getApiClient: () => ({
      decideApproval: decideSpy,
    }),
  };
});

import { InlineApproval } from './inline-approval.js';

describe('InlineApproval', () => {
  let card: InlineApproval;

  beforeEach(async () => {
    decideSpy.mockReset();
    decideSpy.mockResolvedValue({
      id: 'a1',
      status: 'approved',
    });
    card = new InlineApproval();
    card.approvalId = 'a1';
    card.toolName = 'refund';
    card.mcpServer = 'erp';
    card.args = { amount: 500, currency: 'USD' };
    card.canDecide = true;
    document.body.appendChild(card);
    await card.updateComplete;
  });

  afterEach(() => {
    card.remove();
  });

  it('renders pending state with approve + reject buttons', () => {
    const buttons = card.querySelectorAll('button');
    const labels = Array.from(buttons).map((b) =>
      (b.textContent ?? '').trim(),
    );
    expect(labels).toContain('Approve');
    expect(labels).toContain('Reject');
    // Args should be visible as JSON.
    expect(card.innerHTML).toContain('amount');
    expect(card.innerHTML).toContain('500');
  });

  it('hides buttons when can-decide is false', async () => {
    card.canDecide = false;
    await card.updateComplete;
    const buttons = Array.from(card.querySelectorAll('button')).map((b) =>
      (b.textContent ?? '').trim(),
    );
    expect(buttons).not.toContain('Approve');
    expect(buttons).not.toContain('Reject');
    expect(card.innerHTML).toMatch(/Waiting for an approver/i);
  });

  it('calls ApiClient.decideApproval with approve action and emits event', async () => {
    const emitted: CustomEvent<{ approvalId: string; status: string }>[] = [];
    card.addEventListener('rm-approval-decided', (e) =>
      emitted.push(e as CustomEvent),
    );
    const buttons = Array.from(card.querySelectorAll('button'));
    const approve = buttons.find((b) =>
      (b.textContent ?? '').trim() === 'Approve',
    ) as HTMLButtonElement | undefined;
    expect(approve).toBeTruthy();
    approve!.click();
    // Let the async decide() resolve.
    await Promise.resolve();
    await Promise.resolve();
    await card.updateComplete;
    expect(decideSpy).toHaveBeenCalledWith('a1', { action: 'approve' });
    expect(emitted).toHaveLength(1);
    expect(emitted[0].detail.status).toBe('approved');
    expect(card.status).toBe('approved');
  });

  it('calls ApiClient.decideApproval with reject action', async () => {
    decideSpy.mockResolvedValue({ id: 'a1', status: 'rejected' });
    const reject = Array.from(card.querySelectorAll('button')).find(
      (b) => (b.textContent ?? '').trim() === 'Reject',
    ) as HTMLButtonElement | undefined;
    reject!.click();
    await Promise.resolve();
    await Promise.resolve();
    await card.updateComplete;
    expect(decideSpy).toHaveBeenCalledWith('a1', { action: 'reject' });
    expect(card.status).toBe('denied');
  });

  it('setStatus updates the rendered label', async () => {
    card.setStatus('approved', 'bob');
    await card.updateComplete;
    expect(card.innerHTML).toContain('Approved by bob');
    // No buttons in resolved state.
    const buttons = Array.from(card.querySelectorAll('button'));
    expect(buttons).toHaveLength(0);
  });

  it('renders denied state with rejecter name', async () => {
    card.setStatus('denied', 'bob');
    await card.updateComplete;
    expect(card.innerHTML).toContain('Rejected by bob');
  });
});
