// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import './approval-card.js';
import type { ApprovalCard } from './approval-card.js';
import type { ApprovalDecisionDetail } from './approval-card.js';

async function mount(props: Partial<ApprovalCard> = {}): Promise<ApprovalCard> {
  const el = document.createElement('rm-approval-card') as ApprovalCard;
  Object.assign(el, { requestId: 'req-1', status: 'pending', ...props });
  document.body.appendChild(el);
  await el.updateComplete;
  return el;
}

function $<T extends Element>(el: Element, sel: string): T | null {
  return el.querySelector<T>(sel);
}

describe('<rm-approval-card>', () => {
  let el: ApprovalCard | null = null;
  beforeEach(() => {
    el = null;
  });
  afterEach(() => {
    el?.remove();
  });

  it('renders the action summary and both buttons when pending', async () => {
    el = await mount({ actionSummary: 'charge $500 on stripe' });
    expect($(el, '[data-testid="approval-summary"]')?.textContent).toContain(
      'charge $500 on stripe',
    );
    expect($(el, '[data-testid="approval-approve"]')).not.toBeNull();
    expect($(el, '[data-testid="approval-reject"]')).not.toBeNull();
  });

  it('falls back to a generic summary when none is given', async () => {
    el = await mount({ actionSummary: null });
    expect($(el, '[data-testid="approval-summary"]')?.textContent).toContain(
      'needs your approval',
    );
  });

  it('emits approval-decision with the request id + approve verb', async () => {
    el = await mount({ requestId: 'abc', actionSummary: 's' });
    const onDecision = vi.fn();
    el.addEventListener('approval-decision', (e) =>
      onDecision((e as CustomEvent<ApprovalDecisionDetail>).detail),
    );
    $<HTMLButtonElement>(el, '[data-testid="approval-approve"]')!.click();
    expect(onDecision).toHaveBeenCalledTimes(1);
    expect(onDecision).toHaveBeenCalledWith({
      requestId: 'abc',
      decision: 'approve',
    });
  });

  it('emits the reject verb from the ❌ button', async () => {
    el = await mount({ requestId: 'abc' });
    const onDecision = vi.fn();
    el.addEventListener('approval-decision', (e) =>
      onDecision((e as CustomEvent<ApprovalDecisionDetail>).detail),
    );
    $<HTMLButtonElement>(el, '[data-testid="approval-reject"]')!.click();
    expect(onDecision).toHaveBeenCalledWith({
      requestId: 'abc',
      decision: 'reject',
    });
  });

  it('does not emit while busy (double-tap guard)', async () => {
    el = await mount({ busy: true });
    const onDecision = vi.fn();
    el.addEventListener('approval-decision', onDecision);
    // The button is disabled; calling click() on a disabled button is a no-op,
    // and the handler also early-returns on busy — belt and braces.
    $<HTMLButtonElement>(el, '[data-testid="approval-approve"]')?.click();
    expect(onDecision).not.toHaveBeenCalled();
  });

  it('shows a terminal status pill and hides the buttons once resolved', async () => {
    el = await mount({ status: 'approved' });
    expect($(el, '[data-testid="approval-approve"]')).toBeNull();
    expect($(el, '[data-testid="approval-reject"]')).toBeNull();
    expect($(el, '[data-testid="approval-status"]')?.textContent).toContain(
      'Approved',
    );
  });

  it('does not emit after it has resolved even if emit is forced', async () => {
    el = await mount({ status: 'rejected' });
    const onDecision = vi.fn();
    el.addEventListener('approval-decision', onDecision);
    // No buttons exist; nothing to click. Assert the resolved render really
    // dropped them (a regression that left them would let a late tap fire).
    expect($(el, '[data-testid="approval-approve"]')).toBeNull();
    expect(onDecision).not.toHaveBeenCalled();
  });
});
