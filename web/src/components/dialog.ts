// <rm-dialog> — thin wrapper around the native HTML5 <dialog> element.
//
// Why native <dialog> rather than a hand-rolled modal:
//   - browser handles z-index stacking, ::backdrop, top-layer rendering
//   - keyboard focus trap + ESC-to-close come for free
//   - `inert` for the background is automatic
//
// The wrapper is a primitive: it has no business logic, no draft
// state, no API calls. Parents drive `open` and listen for `@close`.
// This mirrors the v1.1 `<rm-inline-approval>` model — small, slot-
// driven, parent-controlled.
//
// Slots:
//   default — body content
//   footer  — optional action row (Cancel / Confirm buttons)
//
// Events:
//   close — fired when the dialog closes via any path (X / backdrop
//           / ESC / programmatic close). detail.reason is one of
//           'x' | 'backdrop' | 'esc' | 'programmatic'.

import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, query } from 'lit/decorators.js';

export type DialogCloseReason = 'x' | 'backdrop' | 'esc' | 'programmatic';

@customElement('rm-dialog')
export class RmDialog extends LitElement {
  /** Header title. Renders empty if blank. */
  @property() title = '';
  /** Open state. Toggling true → opens via showModal(); false → closes. */
  @property({ type: Boolean, reflect: true }) open = false;
  /** Close when the user clicks outside the dialog box. */
  @property({ type: Boolean, attribute: 'close-on-backdrop' })
  closeOnBackdrop = true;
  /** Close when the user presses ESC. Maps to <dialog>'s default
   *  cancel event; setting this false preventDefaults the cancel. */
  @property({ type: Boolean, attribute: 'close-on-esc' })
  closeOnEsc = true;
  /** Optional max-width override. Defaults to 440px (the prototype
   *  `.dlg` width). */
  @property() width = '440px';

  @query('dialog') private dialogEl!: HTMLDialogElement;

  static styles = css`
    :host {
      display: contents;
    }
    /* Closed-state styling: leave display alone. The UA stylesheet's
     * 'dialog:not([open]) { display: none }' is what hides the
     * element — and crucially, author-origin CSS overrides UA-origin
     * regardless of specificity (cascade origins beat specificity
     * for the same property). A bare 'dialog { display: flex }' here
     * would override the UA's display:none and make EVERY dialog
     * visible on page load, even before the parent sets open=true.
     * (Bug found in PR25 smoke test — all four management pages
     * popped their create dialog the moment they mounted.)
     *
     * The non-display styling (width, border, etc.) is safe at this
     * level because it doesn't affect visibility. */
    dialog {
      width: 100%;
      max-width: var(--rm-dialog-width, 440px);
      padding: 0;
      border: 1px solid var(--rm-border-2);
      border-radius: var(--rm-r-lg, 16px);
      background: var(--rm-surface);
      color: var(--rm-ink);
      box-shadow: var(--rm-shadow-lg);
      font-family: var(--rm-font-body);
      /* Keep overflow hidden so the rounded corners aren't escaped
       * by the inner body's scroll content. */
      overflow: hidden;
    }
    dialog::backdrop {
      background: var(--rm-backdrop);
      backdrop-filter: blur(2px);
    }
    /* Open-state layout: max-height + flex column live HERE, scoped
     * to [open] so they only apply when the dialog is actually
     * visible. This is what keeps the footer pinned + body scrolling
     * (PR25 layout fix). */
    dialog[open] {
      max-height: 85vh;
      display: flex;
      flex-direction: column;
      animation: rm-rise 0.2s ease both;
    }
    .hd {
      display: flex;
      align-items: center;
      padding: 16px 22px;
      border-bottom: 1px solid var(--rm-border);
      /* Pinned to top — flex-column would otherwise compress the
       * header when the body fills the available height. */
      flex-shrink: 0;
    }
    .hd h3 {
      font-size: 16.5px;
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
      transition: var(--rm-transition);
    }
    .hd .x:hover {
      background: var(--rm-surface-3);
      color: var(--rm-ink);
    }
    .body {
      padding: 20px 22px 8px;
      /* Grow to fill remaining height; scroll when content exceeds
       * it. 'min-height: 0' is the standard flex-child fix — without
       * it the body refuses to shrink below its content's intrinsic
       * height and the overflow rule never engages. Backticks would
       * terminate the outer css template literal — quote with
       * single quotes inside this comment. */
      flex: 1 1 auto;
      min-height: 0;
      overflow-y: auto;
    }
    .foot {
      display: flex;
      gap: 9px;
      justify-content: flex-end;
      padding: 14px 22px;
      border-top: 1px solid var(--rm-border);
      /* Pinned to bottom — same rationale as .hd. The dialog's
       * fixed max-height + this flex-shrink:0 together guarantee
       * the Cancel / Confirm buttons stay reachable regardless of
       * how tall the body content grows. */
      flex-shrink: 0;
    }
    .foot:empty {
      display: none;
    }
    /* Local fallback for the @keyframes declared in tokens.css —
     * keyframes do NOT inherit into shadow roots, so we redeclare
     * the one we use here. */
    @keyframes rm-rise {
      from { opacity: 0; transform: translateY(8px); }
      to   { opacity: 1; transform: none; }
    }
  `;

  override updated(changed: Map<string, unknown>) {
    if (changed.has('open')) {
      if (this.open) this.openDialog();
      else this.closeDialog('programmatic');
    }
    if (changed.has('width')) {
      this.style.setProperty('--rm-dialog-width', this.width);
    }
  }

  override firstUpdated() {
    this.style.setProperty('--rm-dialog-width', this.width);
    if (this.open) this.openDialog();
  }

  private openDialog() {
    if (this.dialogEl && !this.dialogEl.open) {
      this.dialogEl.showModal();
    }
  }

  private closeDialog(reason: DialogCloseReason) {
    if (this.dialogEl?.open) {
      this.dialogEl.close();
    }
    if (this.open) {
      this.open = false;
    }
    this.dispatchEvent(
      new CustomEvent<{ reason: DialogCloseReason }>('close', {
        detail: { reason },
        bubbles: true,
        composed: true,
      }),
    );
  }

  private onCancel = (e: Event) => {
    // Native <dialog> fires `cancel` on ESC. Prevent default to
    // suppress close if the consumer disabled it.
    if (!this.closeOnEsc) {
      e.preventDefault();
      return;
    }
    e.preventDefault();
    this.closeDialog('esc');
  };

  private onDialogClick = (e: MouseEvent) => {
    if (!this.closeOnBackdrop) return;
    // The native <dialog> swallows backdrop clicks as clicks on the
    // <dialog> element itself; checking that the target IS the
    // dialog (not a descendant) reliably detects backdrop hits.
    if (e.target === this.dialogEl) {
      this.closeDialog('backdrop');
    }
  };

  override render() {
    return html`
      <dialog
        @cancel=${this.onCancel}
        @click=${this.onDialogClick}
      >
        ${this.title
          ? html`
              <div class="hd">
                <h3>${this.title}</h3>
                <button
                  type="button"
                  class="x"
                  aria-label="Close"
                  @click=${() => this.closeDialog('x')}
                >×</button>
              </div>
            `
          : nothing}
        <div class="body">
          <slot></slot>
        </div>
        <div class="foot">
          <slot name="footer"></slot>
        </div>
      </dialog>
    `;
  }
}
