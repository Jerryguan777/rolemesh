// <rm-activity-shell> — v2 activity surface.
//
// Most of the activity surface (per-run timeline, approvals
// real-time feed, audit search) is a v2-C deliverable; this shell
// exists in v2-A so:
//   1. `<rm-chat-shell>`'s topbar Activity icon has somewhere to
//      navigate to (a coming-soon placeholder).
//   2. The redirect from `#/admin/safety/decisions` →
//      `#/activity/safety-decisions` keeps the v1.1 safety
//      decisions page reachable; that page was working in v1.1 and
//      regressing it would violate the "v1.1 不退化" acceptance
//      criterion.
//
// Routing inside the shell is one switch: anything under
// `safety-decisions` slots the existing `<rm-safety-decisions-page>`;
// everything else renders `<rm-coming-soon>`. v2-C replaces the
// coming-soon branch with a real Activity index.

import { LitElement, html, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import './coming-soon.js';
import './safety-decisions-page.js';
import { iconClose } from './icons.js';

@customElement('rm-activity-shell')
export class RmActivityShell extends LitElement {
  @state() private hash: string = location.hash;

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback() {
    super.connectedCallback();
    this.style.display = 'block';
    this.style.height = '100%';
    window.addEventListener('hashchange', this.onHashChange);
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener('hashchange', this.onHashChange);
  }

  private onHashChange = () => {
    this.hash = location.hash;
  };

  private backToChat = () => {
    location.hash = '#/';
  };

  override render(): TemplateResult {
    const body = this.hash.startsWith('#/activity/safety-decisions')
      ? html`<rm-safety-decisions-page></rm-safety-decisions-page>`
      : html`<rm-coming-soon label="Activity" phase=${3}></rm-coming-soon>`;
    return html`
      <style>
        rm-activity-shell {
          display: flex;
          flex-direction: column;
          height: 100%;
          min-height: 0;
          background: var(--rm-bg);
          color: var(--rm-ink);
          font-family: var(--rm-font-body);
        }
        rm-activity-shell .as-hd {
          height: 52px;
          display: flex;
          align-items: center;
          padding: 0 22px;
          border-bottom: 1px solid var(--rm-border);
          gap: 12px;
        }
        rm-activity-shell .as-hd h2 {
          font-size: 16px;
          font-weight: 600;
          margin: 0;
        }
        rm-activity-shell .as-hd .spacer { flex: 1; }
        rm-activity-shell .as-close {
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
        rm-activity-shell .as-close:hover {
          background: var(--rm-surface-3);
          color: var(--rm-ink);
        }
        rm-activity-shell .as-body {
          flex: 1;
          min-height: 0;
          display: flex;
          flex-direction: column;
          overflow: hidden;
        }
      </style>
      <div class="as-hd">
        <h2>Activity</h2>
        <span class="spacer"></span>
        <button
          class="as-close"
          data-testid="activity-back"
          aria-label="Back to chat"
          @click=${this.backToChat}
        >${iconClose(16)}</button>
      </div>
      <div class="as-body">${body}</div>
    `;
  }
}
