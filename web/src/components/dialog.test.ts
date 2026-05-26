// @vitest-environment happy-dom
// <rm-dialog> behaviour — we test the contract a parent depends on,
// not the internal markup. happy-dom implements HTMLDialogElement
// with show/close + dispatches a `close` event, but does NOT route
// ESC keyboard events to the dialog's `cancel` listener (that is a
// browser-level UA behaviour). So:
//   - X-button + backdrop click + programmatic close use real DOM
//     interactions.
//   - ESC is simulated by dispatching the cancel event the spec
//     mandates the UA fires; the component's behaviour given that
//     event is what we are pinning.
//
// Anti-mirror: we never reach into private state. The contract is
// that `open` reflects to the attribute, the native `<dialog>` is
// shown / closed, and `@close` fires with the right `reason`.

import { describe, expect, it, beforeEach, afterEach } from 'vitest';
import './dialog.js';
import type { RmDialog, DialogCloseReason } from './dialog.js';

function mount(): RmDialog {
  const el = document.createElement('rm-dialog') as RmDialog;
  el.title = 'Test dialog';
  document.body.appendChild(el);
  return el;
}

async function waitFrame(el: RmDialog) {
  // LitElement updates are async; wait for the next microtask plus
  // the element's updateComplete promise so dom queries are stable.
  await el.updateComplete;
}

describe('<rm-dialog>', () => {
  let el: RmDialog;

  beforeEach(() => {
    el = mount();
  });
  afterEach(() => {
    el.remove();
  });

  it('opens the native <dialog> when `open` flips to true', async () => {
    el.open = true;
    await waitFrame(el);
    const native = el.shadowRoot!.querySelector('dialog')!;
    expect(native.hasAttribute('open')).toBe(true);
  });

  it('closes the native <dialog> when `open` flips back to false', async () => {
    el.open = true;
    await waitFrame(el);
    el.open = false;
    await waitFrame(el);
    const native = el.shadowRoot!.querySelector('dialog')!;
    expect(native.hasAttribute('open')).toBe(false);
  });

  it('fires close with reason="x" when the X button is clicked', async () => {
    el.open = true;
    await waitFrame(el);
    const reasons: DialogCloseReason[] = [];
    el.addEventListener('close', (e) => {
      reasons.push(
        (e as CustomEvent<{ reason: DialogCloseReason }>).detail.reason,
      );
    });
    const btn = el.shadowRoot!.querySelector<HTMLButtonElement>('.hd .x')!;
    btn.click();
    expect(reasons).toEqual(['x']);
    expect(el.open).toBe(false);
  });

  it('fires close with reason="backdrop" when the dialog backdrop is clicked', async () => {
    el.open = true;
    await waitFrame(el);
    const reasons: DialogCloseReason[] = [];
    el.addEventListener('close', (e) => {
      reasons.push(
        (e as CustomEvent<{ reason: DialogCloseReason }>).detail.reason,
      );
    });
    // Native <dialog> reports backdrop hits as clicks where
    // event.target === the <dialog> element itself.
    const native = el.shadowRoot!.querySelector('dialog')!;
    native.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(reasons).toEqual(['backdrop']);
  });

  it('does NOT close on backdrop click when close-on-backdrop=false', async () => {
    el.closeOnBackdrop = false;
    el.open = true;
    await waitFrame(el);
    let fired = false;
    el.addEventListener('close', () => { fired = true; });
    const native = el.shadowRoot!.querySelector('dialog')!;
    native.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(fired).toBe(false);
    expect(el.open).toBe(true);
  });

  it('treats the ESC cancel event as a close with reason="esc"', async () => {
    el.open = true;
    await waitFrame(el);
    const reasons: DialogCloseReason[] = [];
    el.addEventListener('close', (e) => {
      reasons.push(
        (e as CustomEvent<{ reason: DialogCloseReason }>).detail.reason,
      );
    });
    const native = el.shadowRoot!.querySelector('dialog')!;
    // The UA fires `cancel` (cancellable) when the user presses ESC.
    const cancel = new Event('cancel', { cancelable: true });
    native.dispatchEvent(cancel);
    expect(reasons).toEqual(['esc']);
    expect(el.open).toBe(false);
  });

  it('suppresses ESC when close-on-esc=false', async () => {
    el.closeOnEsc = false;
    el.open = true;
    await waitFrame(el);
    let fired = false;
    el.addEventListener('close', () => { fired = true; });
    const native = el.shadowRoot!.querySelector('dialog')!;
    const cancel = new Event('cancel', { cancelable: true });
    native.dispatchEvent(cancel);
    expect(fired).toBe(false);
    // The cancel event was preventDefaulted, so the UA would keep
    // the dialog open — the `open` property must therefore stay true.
    expect(el.open).toBe(true);
  });

  it('hides the header bar entirely when no title is supplied', async () => {
    el.title = '';
    el.open = true;
    await waitFrame(el);
    expect(el.shadowRoot!.querySelector('.hd')).toBeNull();
  });
});
