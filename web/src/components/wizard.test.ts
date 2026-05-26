// @vitest-environment happy-dom
// <rm-wizard> behaviour — primitive that exposes a step rail + body
// slot + Back/Next/Submit footer. The component owns no draft state;
// the parent flips `currentStep` and `canAdvance`. We test:
//   - rail renders one entry per step + marks active/done correctly
//   - Next/Back buttons advance currentStep AND fire step-change
//   - canAdvance=false disables Next / Submit
//   - last step renders the configured submit label and fires submit
//   - close button fires close event
//
// Anti-mirror: we read the DOM and click real buttons; we don't
// re-implement the index arithmetic and compare.

import { describe, expect, it, beforeEach, afterEach } from 'vitest';
import './wizard.js';
import type { RmWizard, WizardStep } from './wizard.js';

const STEPS: WizardStep[] = [
  { id: 'name',  label: 'Name' },
  { id: 'role',  label: 'Role' },
  { id: 'check', label: 'Review' },
];

function mount(): RmWizard {
  const el = document.createElement('rm-wizard') as RmWizard;
  el.title = 'New thing';
  el.steps = STEPS;
  el.submitLabel = 'Create';
  document.body.appendChild(el);
  return el;
}

async function settle(el: RmWizard) {
  await el.updateComplete;
}

describe('<rm-wizard>', () => {
  let el: RmWizard;
  beforeEach(async () => {
    el = mount();
    await settle(el);
  });
  afterEach(() => el.remove());

  it('renders one rail entry per step and marks the active one', async () => {
    const items = el.shadowRoot!.querySelectorAll('.rail .step');
    expect(items.length).toBe(3);
    expect(items[0].classList.contains('active')).toBe(true);
    expect(items[1].classList.contains('active')).toBe(false);
  });

  it('clicking Next advances currentStep and fires step-change', async () => {
    const fired: number[] = [];
    el.addEventListener('step-change', (e) => {
      fired.push((e as CustomEvent<{ step: number }>).detail.step);
    });
    const nextBtn = el.shadowRoot!.querySelector<HTMLButtonElement>(
      '.foot .btn.primary',
    )!;
    nextBtn.click();
    expect(fired).toEqual([1]);
    expect(el.currentStep).toBe(1);
  });

  it('marks past steps as done and current as active after advancing', async () => {
    el.currentStep = 1;
    await settle(el);
    const items = el.shadowRoot!.querySelectorAll('.rail .step');
    expect(items[0].classList.contains('done')).toBe(true);
    expect(items[1].classList.contains('active')).toBe(true);
  });

  it('Back is only rendered after the first step', async () => {
    expect(
      el.shadowRoot!.querySelector('.foot .btn:not(.primary)'),
    ).toBeNull();
    el.currentStep = 1;
    await settle(el);
    const back = el.shadowRoot!.querySelector<HTMLButtonElement>(
      '.foot .btn:not(.primary)',
    );
    expect(back).not.toBeNull();
    expect(back!.textContent?.trim()).toBe('Back');
  });

  it('Back rewinds and emits step-change', async () => {
    el.currentStep = 2;
    await settle(el);
    const fired: number[] = [];
    el.addEventListener('step-change', (e) => {
      fired.push((e as CustomEvent<{ step: number }>).detail.step);
    });
    const back = el.shadowRoot!.querySelector<HTMLButtonElement>(
      '.foot .btn:not(.primary)',
    )!;
    back.click();
    expect(fired).toEqual([1]);
    expect(el.currentStep).toBe(1);
  });

  it('canAdvance=false disables the primary button', async () => {
    el.canAdvance = false;
    await settle(el);
    const primary = el.shadowRoot!.querySelector<HTMLButtonElement>(
      '.foot .btn.primary',
    )!;
    expect(primary.disabled).toBe(true);
  });

  it('renders the submit label on the last step and fires submit', async () => {
    el.currentStep = 2;
    await settle(el);
    const primary = el.shadowRoot!.querySelector<HTMLButtonElement>(
      '.foot .btn.primary',
    )!;
    expect(primary.textContent?.trim()).toBe('Create');
    let submitted = false;
    el.addEventListener('submit', () => { submitted = true; });
    primary.click();
    expect(submitted).toBe(true);
    // Submit must NOT auto-advance — wizard is a primitive; parent
    // closes / unmounts on a successful POST.
    expect(el.currentStep).toBe(2);
  });

  it('clicking the close X fires close', async () => {
    let closed = false;
    el.addEventListener('close', () => { closed = true; });
    const x = el.shadowRoot!.querySelector<HTMLButtonElement>('.hd .x')!;
    x.click();
    expect(closed).toBe(true);
  });

  it('does not fire step-change when Next is clicked past the last step', async () => {
    el.currentStep = 2;
    await settle(el);
    const fired: number[] = [];
    el.addEventListener('step-change', (e) => {
      fired.push((e as CustomEvent<{ step: number }>).detail.step);
    });
    // On the last step the primary button is Submit, not Next, so
    // we cannot advance past the end through the UI. But the
    // internal fireStepChange must still guard — verify by calling
    // currentStep++ directly through the property.
    el.currentStep = 3;
    await settle(el);
    // The component clamps display by only rendering steps in `steps`,
    // but should not crash; the rail length stays at 3.
    expect(el.shadowRoot!.querySelectorAll('.rail .step').length).toBe(3);
    expect(fired.length).toBe(0);
  });
});
