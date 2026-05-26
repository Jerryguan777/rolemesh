// @vitest-environment happy-dom
// <rm-confirm-dialog> — pin the contract the page components rely on:
//   * Confirm + Cancel buttons emit `confirm` / `cancel` CustomEvents
//   * busy=true disables both buttons + swaps Confirm label
//   * danger tone applies the danger class on Confirm
//   * slotted body children actually render (regression for the
//     light-DOM-slot bug that left the modal body empty)

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import './confirm-dialog.js';
import type { RmConfirmDialog } from './confirm-dialog.js';

async function mount(
  props: Partial<RmConfirmDialog> = {},
  bodyHtml = '<p data-testid="body-marker">Body text here.</p>',
): Promise<RmConfirmDialog> {
  const el = document.createElement('rm-confirm-dialog') as RmConfirmDialog;
  Object.assign(el, { open: true, title: 'Are you sure?', ...props });
  el.innerHTML = bodyHtml;
  document.body.appendChild(el);
  await el.updateComplete;
  // Wait for the nested rm-dialog to render its open <dialog>.
  await new Promise<void>((r) => setTimeout(r, 0));
  await el.updateComplete;
  return el;
}

/** Reach into the shadow root since rm-confirm-dialog is shadow-scoped. */
function $shadow<T extends Element>(el: Element, sel: string): T {
  const root = (el as { shadowRoot?: ShadowRoot }).shadowRoot;
  if (!root) throw new Error('expected shadowRoot on rm-confirm-dialog');
  const found = root.querySelector<T>(sel);
  if (!found) throw new Error(`shadow selector not found: ${sel}`);
  return found;
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
    $shadow<HTMLButtonElement>(el, '[data-testid="confirm-confirm"]').click();
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('emits cancel when the Cancel button is clicked', async () => {
    el = await mount();
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    el.addEventListener('confirm', onConfirm);
    el.addEventListener('cancel', onCancel);
    $shadow<HTMLButtonElement>(el, '[data-testid="confirm-cancel"]').click();
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('renders the danger button class when tone="danger"', async () => {
    el = await mount({ tone: 'danger', confirmLabel: 'Delete' });
    const btn = $shadow<HTMLButtonElement>(el, '[data-testid="confirm-confirm"]');
    expect(btn.classList.contains('btn--danger')).toBe(true);
    expect(btn.classList.contains('btn--primary')).toBe(false);
  });

  it('renders the primary button class when tone="primary"', async () => {
    el = await mount({ tone: 'primary', confirmLabel: 'Save' });
    const btn = $shadow<HTMLButtonElement>(el, '[data-testid="confirm-confirm"]');
    expect(btn.classList.contains('btn--primary')).toBe(true);
    expect(btn.classList.contains('btn--danger')).toBe(false);
  });

  it('disables both buttons + shows the busy label when busy=true', async () => {
    el = await mount({ busy: true, busyLabel: 'Deleting…', confirmLabel: 'Delete' });
    const confirm = $shadow<HTMLButtonElement>(el, '[data-testid="confirm-confirm"]');
    const cancel = $shadow<HTMLButtonElement>(el, '[data-testid="confirm-cancel"]');
    expect(confirm.disabled).toBe(true);
    expect(cancel.disabled).toBe(true);
    expect(confirm.textContent?.trim()).toBe('Deleting…');
  });

  it('disables Confirm but not Cancel when disable-confirm is set, and keeps the regular label', async () => {
    // Policy block path: the parent has determined the action is
    // not allowed (e.g. resource still bound) and wants the user to
    // see the explanation in the body then dismiss with Cancel.
    el = await mount({ disableConfirm: true, confirmLabel: 'Delete' });
    const confirm = $shadow<HTMLButtonElement>(el, '[data-testid="confirm-confirm"]');
    const cancel = $shadow<HTMLButtonElement>(el, '[data-testid="confirm-cancel"]');
    expect(confirm.disabled).toBe(true);
    expect(cancel.disabled).toBe(false);
    // Label is the regular one — NOT swapped to busy-label, because
    // this isn't an in-flight state.
    expect(confirm.textContent?.trim()).toBe('Delete');
  });

  it('renders slotted body content (regression: light DOM slot was a no-op)', async () => {
    // Pin the bug where switching to light DOM made <slot> silently
    // empty: shadowed <slot> projects the host's light DOM children
    // into the body div. If this assertion ever fails, the modal will
    // ship with a blank body again.
    el = await mount(
      {},
      '<p data-testid="body-marker"><strong>Delete coworker</strong> Q3 ops?</p>',
    );
    const marker = el.querySelector('[data-testid="body-marker"]');
    expect(marker, 'slotted body marker must remain in the light DOM').not.toBeNull();
    // assignedNodes() on the body slot must surface the marker.
    const root = (el as unknown as { shadowRoot: ShadowRoot }).shadowRoot;
    const slot = root.querySelector('slot');
    expect(slot, 'rm-confirm-dialog shadow root must contain a <slot>').not.toBeNull();
    const assigned = slot!.assignedElements();
    expect(assigned.map((n) => n.tagName)).toContain('P');
  });
});
