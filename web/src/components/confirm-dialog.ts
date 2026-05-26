// <rm-confirm-dialog> — destructive / confirm action modal.
//
// Wraps <rm-dialog> with a fixed body+footer layout: heading + body
// slot + [Cancel] [Confirm] footer. Centralizes the "Are you sure?"
// pattern so coworkers, MCP servers, skills, credentials, and skill-
// files all render the same modal instead of a mix of `window.confirm`
// (theme-ignoring) and bespoke per-page rm-dialog markup.
//
// API: parent sets `?open`, `title`, slots a body description into
// the default slot, and listens for `@confirm` / `@cancel` events.
// The component does NOT close itself on confirm — the parent is in
// charge of the lifecycle so a failed API call can keep the modal up
// (or close it with an error banner).
//
// Render-root: light DOM. The footer buttons use the global
// `.rm-btn` / `.rm-btn--*` classes shipped in settings-pages.css;
// living in light DOM lets those classes apply without re-declaring
// them inside a shadow root.

import { LitElement, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';

import './dialog.js';

export type ConfirmTone = 'primary' | 'danger';

@customElement('rm-confirm-dialog')
export class RmConfirmDialog extends LitElement {
  @property() title = 'Are you sure?';
  @property({ type: Boolean, reflect: true }) open = false;
  /** Visual treatment of the Confirm button. `danger` paints it red
   *  for destructive actions; `primary` reuses the terracotta accent. */
  @property() tone: ConfirmTone = 'primary';
  @property({ attribute: 'confirm-label' }) confirmLabel = 'Confirm';
  @property({ attribute: 'cancel-label' }) cancelLabel = 'Cancel';
  /** When true, both buttons are disabled and Confirm shows
   *  `busy-label` (or "Working…" if unset). Parent flips this while
   *  the underlying API call is in flight. */
  @property({ type: Boolean }) busy = false;
  @property({ attribute: 'busy-label' }) busyLabel = '';

  protected override createRenderRoot() {
    return this;
  }

  private onCancel = (): void => {
    if (this.busy) return;
    this.dispatchEvent(new CustomEvent('cancel', { bubbles: true, composed: true }));
  };

  private onConfirm = (): void => {
    if (this.busy) return;
    this.dispatchEvent(new CustomEvent('confirm', { bubbles: true, composed: true }));
  };

  override render() {
    const confirmClass =
      this.tone === 'danger' ? 'rm-btn rm-btn--danger' : 'rm-btn rm-btn--primary';
    return html`
      <rm-dialog
        title=${this.title}
        ?open=${this.open}
        @close=${this.onCancel}
      >
        <div data-testid="confirm-body">
          <slot></slot>
        </div>
        <div
          slot="footer"
          style="display: flex; gap: 8px; justify-content: flex-end;"
        >
          <button
            type="button"
            class="rm-btn rm-btn--secondary"
            data-testid="confirm-cancel"
            ?disabled=${this.busy}
            @click=${this.onCancel}
          >${this.cancelLabel}</button>
          <button
            type="button"
            class=${confirmClass}
            data-testid="confirm-confirm"
            ?disabled=${this.busy}
            @click=${this.onConfirm}
          >${this.busy
              ? (this.busyLabel || 'Working…')
              : this.confirmLabel}</button>
        </div>
        ${nothing}
      </rm-dialog>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'rm-confirm-dialog': RmConfirmDialog;
  }
}
