// HITL approval card (docs/21-hitl-approval-plan.md §10 S5). Renders one pending
// tool-approval request inside the chat stream: the human-readable action
// summary plus ✅ / ❌ buttons. On click it dispatches an `approval-decision`
// CustomEvent; chat-panel relays it to the orchestrator via the v1 WS frame
// (`V1WsClient.sendApprovalDecision`). The card never sends the approver
// identity — that is stamped server-side from the verified WS ticket (IDOR
// guard), so this component only knows the request id + verb.
//
// Light DOM (createRenderRoot → this) so the chat surface's Tailwind utility
// classes apply, matching chat-panel / message-list.

import { LitElement, html, type TemplateResult } from 'lit';
import { customElement, property } from 'lit/decorators.js';

import type { ApprovalStatus } from './approval-store.js';

/** Fired when the user taps ✅ or ❌. Bubbles + composed so chat-panel
 *  (the light-DOM host) catches it. */
export interface ApprovalDecisionDetail {
  requestId: string;
  decision: 'approve' | 'reject';
}

const STATUS_LABEL: Record<Exclude<ApprovalStatus, 'pending'>, string> = {
  approved: '✅ Approved',
  rejected: '❌ Rejected',
  expired: '⏰ Expired',
};

@customElement('rm-approval-card')
export class ApprovalCard extends LitElement {
  @property() requestId = '';
  @property() actionSummary: string | null = null;
  @property() status: ApprovalStatus = 'pending';
  /** Disables the buttons while a decision is in flight (set by the host
   *  between the click and the `event.approval.resolved` echo). */
  @property({ type: Boolean }) busy = false;

  protected override createRenderRoot() {
    return this;
  }

  private emit(decision: 'approve' | 'reject'): void {
    if (this.busy || this.status !== 'pending') return;
    this.dispatchEvent(
      new CustomEvent<ApprovalDecisionDetail>('approval-decision', {
        detail: { requestId: this.requestId, decision },
        bubbles: true,
        composed: true,
      }),
    );
  }

  private renderActions(): TemplateResult {
    if (this.status !== 'pending') {
      return html`
        <span
          class="text-[12px] font-medium text-ink-2 dark:text-d-ink-2"
          data-testid="approval-status"
          >${STATUS_LABEL[this.status]}</span
        >
      `;
    }
    return html`
      <div class="flex items-center gap-2">
        <button
          type="button"
          data-testid="approval-approve"
          class="text-[12px] px-3 py-1 rounded-md border border-emerald-300 dark:border-emerald-700 text-emerald-700 dark:text-emerald-300 hover:bg-emerald-50 dark:hover:bg-emerald-900/30 transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          ?disabled=${this.busy}
          @click=${() => this.emit('approve')}
        >
          ✅ Approve
        </button>
        <button
          type="button"
          data-testid="approval-reject"
          class="text-[12px] px-3 py-1 rounded-md border border-red-300 dark:border-red-700 text-red-600 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-900/30 transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          ?disabled=${this.busy}
          @click=${() => this.emit('reject')}
        >
          ❌ Reject
        </button>
      </div>
    `;
  }

  override render(): TemplateResult {
    const summary =
      this.actionSummary && this.actionSummary.trim()
        ? this.actionSummary
        : 'A tool call needs your approval.';
    return html`
      <div
        class="my-2 rounded-xl border border-amber-300 dark:border-amber-700/60 bg-amber-50 dark:bg-amber-900/15 px-4 py-3"
        data-testid="approval-card"
      >
        <div class="flex items-start justify-between gap-3">
          <div class="min-w-0">
            <div
              class="text-[11px] font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-400"
            >
              Approval needed
            </div>
            <div
              class="mt-0.5 text-[13px] text-ink-1 dark:text-d-ink-1 break-words"
              data-testid="approval-summary"
            >
              ${summary}
            </div>
          </div>
          <div class="shrink-0">${this.renderActions()}</div>
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'rm-approval-card': ApprovalCard;
  }
}
