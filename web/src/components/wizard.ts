// <rm-wizard> — multi-step wizard shell primitive.
//
// Like <rm-dialog>, this is intentionally a *shell*. It owns:
//   - step rail rendering (numbered circles + labels)
//   - Back / Next / Submit button row
//   - keyboard / aria scaffolding
// It does NOT own:
//   - draft state (parent component holds the form values)
//   - validation (parent flips `canAdvance` based on its own rules)
//   - submit behaviour (parent listens for the `submit` event)
//
// This mirrors v2-A's split with <rm-dialog> and the v1.1
// <rm-inline-approval>: small primitive + parent-controlled state.
//
// The default slot renders the body of the current step. The parent
// is expected to swap content in/out based on `currentStep`, e.g.
// by branching with `when()` or by giving each step pane a `slot`
// name matching the step id and showing them conditionally with CSS.
// We keep the default slot simple (the parent picks the strategy)
// rather than introducing a step-id contract.

import { LitElement, css, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';

export interface WizardStep {
  /** Unique id for this step. Surfaces in `step-change` event detail
   *  so parents can switch rendering on it. */
  id: string;
  /** Visible label in the rail. */
  label: string;
  /** Optional helper text under the step label. */
  hint?: string;
}

@customElement('rm-wizard')
export class RmWizard extends LitElement {
  @property() title = '';
  @property({ attribute: false }) steps: WizardStep[] = [];
  /** Zero-based index of the currently visible step. Parent-owned. */
  @property({ type: Number, attribute: 'current-step' }) currentStep = 0;
  /** When false, the Next / Submit button is disabled. */
  @property({ type: Boolean, attribute: 'can-advance' }) canAdvance = true;
  /** Label for the terminal-step button. */
  @property({ attribute: 'submit-label' }) submitLabel = 'Submit';
  /** Whether the wizard is busy (e.g. submit in flight). Disables
   *  Back/Next and Submit while true. */
  @property({ type: Boolean }) busy = false;

  static styles = css`
    :host {
      display: flex;
      flex-direction: column;
      min-height: 0;
      height: 100%;
      font-family: var(--rm-font-body);
      color: var(--rm-ink);
      background: var(--rm-surface);
    }
    .hd {
      display: flex;
      align-items: center;
      padding: 15px 20px;
      border-bottom: 1px solid var(--rm-border);
    }
    .hd h2 {
      font-size: 17px;
      font-weight: 600;
      margin: 0;
    }
    .hd .x {
      margin-left: auto;
      width: 28px;
      height: 28px;
      border-radius: 7px;
      display: grid;
      place-items: center;
      color: var(--rm-ink-3);
      background: none;
      border: none;
      cursor: pointer;
      font-family: inherit;
    }
    .hd .x:hover {
      background: var(--rm-surface-3);
      color: var(--rm-ink);
    }
    .main {
      flex: 1;
      display: grid;
      grid-template-columns: 188px 1fr;
      min-height: 0;
    }
    .rail {
      border-right: 1px solid var(--rm-border);
      padding: 16px 12px;
      background: var(--rm-surface-2);
      overflow-y: auto;
      list-style: none;
      margin: 0;
    }
    .step {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 7px 9px;
      border-radius: var(--rm-radius-sm);
      font-size: var(--rm-text-sm);
      color: var(--rm-ink-3);
      margin-bottom: 2px;
    }
    .step .num {
      width: 21px;
      height: 21px;
      border-radius: 50%;
      border: 1.5px solid var(--rm-border-2);
      display: grid;
      place-items: center;
      font-size: var(--rm-text-xs);
      flex-shrink: 0;
    }
    .step.active {
      color: var(--rm-ink);
      font-weight: 500;
    }
    .step.active .num {
      border-color: var(--rm-accent);
      background: var(--rm-accent);
      color: var(--rm-accent-ink);
    }
    .step.done { color: var(--rm-ink-2); }
    .step.done .num {
      border-color: var(--rm-good);
      background: var(--rm-good);
      color: #fff;
    }
    .body {
      padding: 22px 26px;
      overflow-y: auto;
    }
    .foot {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 13px 20px;
      border-top: 1px solid var(--rm-border);
    }
    .sp { flex: 1; }
    .btn {
      padding: 8px 16px;
      border-radius: var(--rm-radius-sm);
      font-size: 13.5px;
      font-weight: 500;
      border: 1px solid var(--rm-border-2);
      color: var(--rm-ink-2);
      background: none;
      cursor: pointer;
      font-family: inherit;
      transition: var(--rm-transition);
    }
    .btn:hover:not(:disabled) { background: var(--rm-surface-2); }
    .btn.primary {
      background: var(--rm-accent);
      color: var(--rm-accent-ink);
      border-color: var(--rm-accent);
    }
    .btn.primary:hover:not(:disabled) { background: var(--rm-accent-2); }
    .btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
  `;

  private fireStepChange(next: number) {
    if (next < 0 || next >= this.steps.length) return;
    if (next === this.currentStep) return;
    this.currentStep = next;
    this.dispatchEvent(
      new CustomEvent<{ step: number; id: string }>('step-change', {
        detail: { step: next, id: this.steps[next]?.id ?? '' },
        bubbles: true,
        composed: true,
      }),
    );
  }

  private onBack = () => this.fireStepChange(this.currentStep - 1);
  private onNext = () => this.fireStepChange(this.currentStep + 1);

  private onSubmit = () => {
    this.dispatchEvent(
      new CustomEvent('submit', {
        bubbles: true,
        composed: true,
      }),
    );
  };

  private onClose = () => {
    this.dispatchEvent(
      new CustomEvent('close', { bubbles: true, composed: true }),
    );
  };

  override render() {
    const isLast = this.currentStep === this.steps.length - 1;
    const hasPrev = this.currentStep > 0;
    return html`
      <div class="hd">
        <h2>${this.title}</h2>
        <button
          type="button"
          class="x"
          aria-label="Close"
          @click=${this.onClose}
        >×</button>
      </div>
      <div class="main">
        <ol class="rail" aria-label="Wizard steps">
          ${this.steps.map((s, i) => {
            const cls = i === this.currentStep
              ? 'step active'
              : i < this.currentStep
                ? 'step done'
                : 'step';
            return html`
              <li
                class=${cls}
                aria-current=${i === this.currentStep ? 'step' : 'false'}
              >
                <span class="num">${i + 1}</span>
                <span>${s.label}</span>
              </li>
            `;
          })}
        </ol>
        <div class="body">
          <slot></slot>
        </div>
      </div>
      <div class="foot">
        ${hasPrev
          ? html`
              <button
                type="button"
                class="btn"
                ?disabled=${this.busy}
                @click=${this.onBack}
              >Back</button>
            `
          : nothing}
        <span class="sp"></span>
        ${isLast
          ? html`
              <button
                type="button"
                class="btn primary"
                ?disabled=${!this.canAdvance || this.busy}
                @click=${this.onSubmit}
              >${this.submitLabel}</button>
            `
          : html`
              <button
                type="button"
                class="btn primary"
                ?disabled=${!this.canAdvance || this.busy}
                @click=${this.onNext}
              >Next</button>
            `}
      </div>
    `;
  }
}
