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
// Render-root: SHADOW DOM. An earlier draft used light DOM so the
// global `.rm-btn` classes from settings-pages.css would style the
// footer for free — but light-DOM `<slot>` is a no-op (slots only
// project from a shadow root), so the parent's body children were
// being clobbered every render and the modal showed buttons over an
// empty body. The shadow root makes `<slot>` work; the price is that
// the button styles have to be redeclared here, kept in lockstep
// with `.rm-btn` in settings-pages.css. CSS custom properties
// (--rm-*) still cascade through the shadow boundary, so the
// terracotta/cream palette is shared automatically.

import { LitElement, css, html, nothing } from 'lit';
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

  static styles = css`
    :host {
      display: contents;
    }
    /* Body wrapper — gives the slotted paragraphs a sensible default
     * color/typography so callers don't have to repeat themselves. */
    .body {
      color: var(--rm-ink);
      font-family: var(--rm-font-body);
      font-size: var(--rm-text-sm);
      line-height: 1.5;
    }
    .footer {
      display: flex;
      gap: 8px;
      justify-content: flex-end;
    }
    /* Mirror of the .rm-btn--* family declared in
     * src/styles/settings-pages.css. Required here because shadow
     * roots do not inherit class-based stylesheets — only custom
     * properties cascade in. If you tweak --primary/--secondary/
     * --danger in one file, update the other. */
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 7px 14px;
      border-radius: var(--rm-radius-sm);
      font-size: var(--rm-text-sm);
      font-weight: 500;
      border: 1px solid transparent;
      cursor: pointer;
      font-family: inherit;
      transition: var(--rm-transition);
    }
    .btn:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .btn--primary {
      background: var(--rm-accent);
      color: var(--rm-accent-ink);
    }
    .btn--primary:hover:not(:disabled) {
      background: var(--rm-accent-2);
    }
    .btn--secondary {
      background: none;
      color: var(--rm-ink-2);
      border-color: var(--rm-border-2);
    }
    .btn--secondary:hover:not(:disabled) {
      background: var(--rm-surface-2);
      color: var(--rm-ink);
    }
    .btn--danger {
      background: var(--rm-bad);
      color: var(--rm-accent-ink);
    }
    .btn--danger:hover:not(:disabled) {
      filter: brightness(0.92);
    }
  `;

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
      this.tone === 'danger' ? 'btn btn--danger' : 'btn btn--primary';
    return html`
      <rm-dialog
        title=${this.title}
        ?open=${this.open}
        @close=${this.onCancel}
      >
        <div class="body" data-testid="confirm-body">
          <slot></slot>
        </div>
        <div slot="footer" class="footer">
          <button
            type="button"
            class="btn btn--secondary"
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
