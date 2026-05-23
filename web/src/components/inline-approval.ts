// Inline approval card — used inside chat-panel for an approver who
// is staring at a conversation and wants to decide without leaving
// it, and inside the approvals queue page for the list rows.
//
// Open Question 3 (locked at session-prompt time): we ship this as a
// standalone `<rm-inline-approval>` rather than threading approval
// state into <rm-chat-panel> directly. Two reasons:
//   1. The same component renders in the queue page (`<rm-approvals-page>`),
//      so duplicating the markup inside chat-panel would mean two
//      copies of the same affordance going out of sync.
//   2. The component owns its own minimal state (pending → resolved)
//      and emits an `rm-approval-decided` CustomEvent the parent can
//      ignore — chat-panel doesn't need to manage approval lifecycle.
//
// The component DOES NOT subscribe to the WS bus itself; the parent
// is responsible for `setStatus(...)` when an `event.approval.resolved`
// frame lands. This keeps it testable in isolation and avoids two
// components fighting over the same WS event.

import { LitElement, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import { ApiError, getApiClient } from '../api/client.js';

export type InlineApprovalStatus =
  | 'pending'
  | 'approved'
  | 'denied'
  | 'expired'
  | 'cancelled'
  | 'unknown';

@customElement('rm-inline-approval')
export class InlineApproval extends LitElement {
  /** approval_id. Required — the component is keyed by approval. */
  @property({ attribute: 'approval-id' }) approvalId = '';
  /** Display name of the tool ("refund", "rm -rf /", …). */
  @property({ attribute: 'tool-name' }) toolName = '';
  /** Best-effort summary of the tool args. */
  @property({ attribute: false }) args: Record<string, unknown> = {};
  /** Display name of the MCP server backing the tool. */
  @property({ attribute: 'mcp-server' }) mcpServer = '';
  /** Current resolution state. Parent updates via setStatus(). */
  @property({ reflect: true }) status: InlineApprovalStatus = 'pending';
  /** Who decided (if known). Displayed under the resolved state. */
  @property({ attribute: 'actor-name' }) actorName = '';
  /** Whether the current user is allowed to decide. When false the
   *  buttons are hidden and the card renders read-only. The server
   *  is the source of truth (it returns 403); this is purely a UI
   *  affordance hint. */
  @property({ type: Boolean, attribute: 'can-decide' }) canDecide = false;

  @state() private busy = false;
  @state() private error: string | null = null;

  protected override createRenderRoot() {
    return this;
  }

  /** Imperative API for the parent to mark this card resolved when a
   *  WS `event.approval.resolved` frame lands. */
  setStatus(status: InlineApprovalStatus, actorName?: string): void {
    this.status = status;
    if (actorName !== undefined) this.actorName = actorName;
    this.error = null;
  }

  private async decide(decision: 'approve' | 'reject'): Promise<void> {
    if (!this.approvalId || this.busy || this.status !== 'pending') return;
    this.busy = true;
    this.error = null;
    try {
      const updated = await getApiClient().decideApproval(this.approvalId, {
        action: decision,
      });
      // Optimistically reflect the decision; the WS event will follow
      // and re-confirm (or override if a concurrent decider won).
      this.status = updated.status === 'approved' ? 'approved' : 'denied';
      this.dispatchEvent(
        new CustomEvent('rm-approval-decided', {
          detail: { approvalId: this.approvalId, status: this.status },
          bubbles: true,
          composed: true,
        }),
      );
    } catch (err) {
      this.error =
        err instanceof ApiError
          ? `${err.status} ${err.body?.code ?? ''} — ${err.message}`
          : (err as Error).message ?? 'decide failed';
    } finally {
      this.busy = false;
    }
  }

  private renderArgs() {
    const keys = Object.keys(this.args);
    if (keys.length === 0) return nothing;
    return html`<pre
      class="text-[11px] font-mono text-ink-3 dark:text-d-ink-3 bg-surface-1
        dark:bg-d-surface-1 rounded p-2 overflow-x-auto mt-2 max-h-32"
    >${JSON.stringify(this.args, null, 2)}</pre>`;
  }

  private renderState() {
    if (this.status === 'pending') {
      if (!this.canDecide) {
        return html`<div
          class="text-[11px] text-ink-3 dark:text-d-ink-3 italic mt-2"
        >Waiting for an approver…</div>`;
      }
      return html`
        <div class="flex gap-2 mt-2">
          <button
            type="button"
            class="text-[12px] px-3 py-1 rounded-md bg-green-600 text-white
              hover:bg-green-700 disabled:opacity-50 cursor-pointer"
            ?disabled=${this.busy}
            @click=${() => void this.decide('approve')}
          >Approve</button>
          <button
            type="button"
            class="text-[12px] px-3 py-1 rounded-md bg-red-600 text-white
              hover:bg-red-700 disabled:opacity-50 cursor-pointer"
            ?disabled=${this.busy}
            @click=${() => void this.decide('reject')}
          >Reject</button>
        </div>
      `;
    }
    const label =
      this.status === 'approved'
        ? this.actorName
          ? `Approved by ${this.actorName}`
          : 'Approved'
        : this.status === 'denied'
          ? this.actorName
            ? `Rejected by ${this.actorName}`
            : 'Rejected'
          : this.status === 'expired'
            ? 'Expired before a decision'
            : this.status === 'cancelled'
              ? 'Cancelled'
              : this.status;
    const tone =
      this.status === 'approved'
        ? 'text-green-700 dark:text-green-300'
        : this.status === 'denied'
          ? 'text-red-700 dark:text-red-300'
          : 'text-ink-3 dark:text-d-ink-3';
    return html`<div class="text-[12px] font-medium ${tone} mt-2">${label}</div>`;
  }

  override render() {
    return html`
      <div
        class="border border-surface-3 dark:border-d-surface-3 rounded-lg
          px-3 py-2 bg-surface-1 dark:bg-d-surface-1"
        data-approval-id=${this.approvalId}
      >
        <div class="flex items-baseline justify-between gap-2">
          <div class="text-[13px] font-medium text-ink-0 dark:text-d-ink-0">
            ${this.toolName || 'tool call'}
          </div>
          <div class="text-[11px] text-ink-3 dark:text-d-ink-3 font-mono">
            ${this.mcpServer}
          </div>
        </div>
        ${this.renderArgs()}
        ${this.renderState()}
        ${this.error
          ? html`<div
              class="text-[11px] text-red-700 dark:text-red-300 mt-1"
            >${this.error}</div>`
          : nothing}
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'rm-inline-approval': InlineApproval;
  }
}
