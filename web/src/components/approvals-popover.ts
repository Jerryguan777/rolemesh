// <rm-approvals-popover> — topbar Approvals inbox.
//
// Composition:
//   * Parent (chat-shell) owns the `open` boolean + the
//     `UserApprovalsClient`. It passes the row list in as a property.
//   * This component renders the popover surface: header, list, empty
//     state, footer link to the full Activity log. Each row is the
//     v1.1 `<rm-inline-approval>` so the decide buttons + tone live
//     in one place.
//   * The parent positions us anchored under the Approvals icon —
//     we render in light DOM and inherit the parent's CSS so the
//     existing `.cs-menu.approvals` class controls placement and
//     animation (no duplicated positioning logic).
//
// Why we don't own the list ourselves: the chat-shell badge needs the
// same row count permanently (popover closed or not). Lifting the WS
// subscription to the parent means one subscription, one source of
// truth, and no race between "popover is the canonical list" and
// "badge has its own count". This component is a pure view.

import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, property } from 'lit/decorators.js';

import type { ApprovalRequest, Me } from '../api/client.js';
import './inline-approval.js';

/** Cap the popover at this many rows — anything beyond shows a
 *  "+N more" link to the Activity surface. 5 keeps the popover
 *  shorter than a viewport on a 13" laptop. */
export const POPOVER_MAX_ROWS = 5;

@customElement('rm-approvals-popover')
export class RmApprovalsPopover extends LitElement {
  /** Pending approval rows to render. Sorted newest-first by the
   *  parent so this component does not need to know the sort key. */
  @property({ attribute: false }) rows: ApprovalRequest[] = [];
  /** Signed-in user — needed so each row can decide whether to show
   *  the decide buttons (caller must be in `resolved_approvers`). */
  @property({ attribute: false }) me: Me | null = null;
  /** Loading flag — surface a hint instead of "no pending approvals"
   *  while the initial REST fetch is in flight. */
  @property({ type: Boolean }) loading = false;
  /** Connection status from the parent's UserApprovalsClient. When
   *  not 'open' we render a "live updates stalled" footer hint so
   *  the user knows the list may be stale. */
  @property() connectionStatus: string = 'open';

  protected override createRenderRoot() {
    return this;
  }

  private viewAllInActivity = (e: MouseEvent) => {
    e.preventDefault();
    // Bubble to the parent so it can close the popover (no shared state).
    this.dispatchEvent(
      new CustomEvent('rm-popover-navigate', {
        detail: { hash: '#/activity/approvals' },
        bubbles: true,
        composed: true,
      }),
    );
    location.hash = '#/activity/approvals';
  };

  private renderRow(r: ApprovalRequest): TemplateResult {
    const action = (r.actions ?? [])[0] ?? ({} as Record<string, unknown>);
    const toolName = String(action.tool_name ?? '');
    const args = (action.params ?? {}) as Record<string, unknown>;
    const canDecide =
      !!this.me && (r.resolved_approvers ?? []).includes(this.me.user_id);
    return html`
      <div class="appr-row" data-testid="approval-row" data-approval-id=${r.id}>
        <rm-inline-approval
          approval-id=${r.id}
          tool-name=${toolName}
          .args=${args}
          mcp-server=${r.mcp_server_name}
          status="pending"
          .canDecide=${canDecide}
        ></rm-inline-approval>
      </div>
    `;
  }

  private renderEmpty(): TemplateResult {
    if (this.loading) {
      return html`<div class="appr-empty" data-testid="approval-loading">
        Loading approvals…
      </div>`;
    }
    return html`<div class="appr-empty" data-testid="approval-empty">
      No pending approvals.
    </div>`;
  }

  override render(): TemplateResult {
    const visible = this.rows.slice(0, POPOVER_MAX_ROWS);
    const overflow = Math.max(0, this.rows.length - POPOVER_MAX_ROWS);
    const stale = this.connectionStatus !== 'open';
    return html`
      <div class="appr-hd">
        Pending approvals
        <small>${this.rows.length} active</small>
      </div>
      <div class="appr-body" data-testid="approvals-popover-body">
        ${this.rows.length === 0
          ? this.renderEmpty()
          : html`<div class="appr-rows">
              ${visible.map((r) => this.renderRow(r))}
            </div>`}
      </div>
      <div class="appr-ft">
        ${overflow > 0
          ? html`<span class="appr-overflow" data-testid="approval-overflow">
              +${overflow} more in Activity log
            </span>`
          : nothing}
        <a
          href="#/activity/approvals"
          class="appr-link"
          data-testid="approvals-view-all"
          @click=${this.viewAllInActivity}
        >View all →</a>
      </div>
      ${stale
        ? html`<div
            class="appr-stale"
            data-testid="approvals-stale"
            title=${this.connectionStatus}
          >Live updates stalled — list may be out of date.</div>`
        : nothing}
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'rm-approvals-popover': RmApprovalsPopover;
  }
}
