// @vitest-environment happy-dom
// <rm-confirm-dialog> — pin the contract the page components rely on:
//   * Confirm + Cancel buttons emit `confirm` / `cancel` CustomEvents
//   * busy=true disables both buttons + swaps Confirm label
//   * danger tone applies the .rm-btn--danger class on Confirm
//   * cancel fires when busy=false (and is suppressed when busy=true)

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import './confirm-dialog.js';
import type { RmConfirmDialog } from './confirm-dialog.js';

async function mount(props: Partial<RmConfirmDialog> = {}): Promise<RmConfirmDialog> {
  const el = document.createElement('rm-confirm-dialog') as RmConfirmDialog;
  Object.assign(el, { open: true, title: 'Are you sure?', ...props });
  document.body.appendChild(el);
  await el.updateComplete;
  // Wait for the nested rm-dialog to render its open <dialog>.
  await new Promise<void>((r) => setTimeout(r, 0));
  await el.updateComplete;
  return el;
}

describe('<rm-confirm-dialog>', () => {
  let el: RmConfirmDialog | null = null;

  beforeEach(() => {
    el = null;
  });

  afterEach(() => {
    el?.remove();
  });

  it('emits confirm when the Confirm button is clicked', async () => {
    el = await mount({ confirmLabel: 'Delete', tone: 'danger' });
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    el.addEventListener('confirm', onConfirm);
    el.addEventListener('cancel', onCancel);
    el.querySelector<HTMLButtonElement>('[data-testid="confirm-confirm"]')!.click();
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('emits cancel when the Cancel button is clicked', async () => {
    el = await mount();
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    el.addEventListener('confirm', onConfirm);
    el.addEventListener('cancel', onCancel);
    el.querySelector<HTMLButtonElement>('[data-testid="confirm-cancel"]')!.click();
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('uses .rm-btn--danger on Confirm when tone="danger"', async () => {
    el = await mount({ tone: 'danger', confirmLabel: 'Delete' });
    const btn = el.querySelector<HTMLButtonElement>('[data-testid="confirm-confirm"]')!;
    expect(btn.classList.contains('rm-btn--danger')).toBe(true);
    expect(btn.classList.contains('rm-btn--primary')).toBe(false);
  });

  it('uses .rm-btn--primary on Confirm when tone="primary"', async () => {
    el = await mount({ tone: 'primary', confirmLabel: 'Save' });
    const btn = el.querySelector<HTMLButtonElement>('[data-testid="confirm-confirm"]')!;
    expect(btn.classList.contains('rm-btn--primary')).toBe(true);
    expect(btn.classList.contains('rm-btn--danger')).toBe(false);
  });

  it('disables both buttons + shows the busy label when busy=true', async () => {
    el = await mount({ busy: true, busyLabel: 'Deleting…', confirmLabel: 'Delete' });
    const confirm = el.querySelector<HTMLButtonElement>('[data-testid="confirm-confirm"]')!;
    const cancel = el.querySelector<HTMLButtonElement>('[data-testid="confirm-cancel"]')!;
    expect(confirm.disabled).toBe(true);
    expect(cancel.disabled).toBe(true);
    expect(confirm.textContent?.trim()).toBe('Deleting…');
  });

  it('suppresses the cancel event while busy', async () => {
    el = await mount({ busy: true });
    const onCancel = vi.fn();
    el.addEventListener('cancel', onCancel);
    // Even though disabled, double-check the handler self-guards in case
    // a programmatic click slips through (e.g. screen-reader bypass).
    (el as unknown as { onCancel: () => void }).onCancel?.();
    expect(onCancel).not.toHaveBeenCalled();
  });
});
