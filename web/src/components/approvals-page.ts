// Approvals queue page (#/approvals) — design §6.3 I.
//
// Default list: requests where the signed-in user is in
// resolved_approvers AND status=pending. Admins can flip
// `scope=all` to see the full tenant view (server gates this).
//
// Real-time refresh: the page subscribes to its own short-lived
// WebSocket via V1WsClient — design §4 carries
// `event.approval.required` / `event.approval.resolved` on the
// per-conversation stream, but the queue page is *cross-conversation*
// by nature. Two simplifications:
//   1. We re-fetch the list on every WS event (cheap; the response
//      is the few-row Pending set, not the whole audit history).
//   2. Without a known conversation_id we can't open a v1 stream
//      WS; instead the page polls the REST list on a 15s cadence
//      so a missed event delays the UI by at most that long.
//
// Design §6.3 I calls out that "Phase 1 全 auto_execute, 渲染占位
// 提示"; Phase 3 fills that placeholder in.

import { LitElement, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import { ApiError, getApiClient } from '../api/client.js';
import type { ApprovalRequest, Me } from '../api/client.js';

import './inline-approval.js';

type Scope = 'mine' | 'all';
/** `pending` = default queue (inline decide buttons + 'mine'/'all'
 *  scope toggle). `resolved` = Activity log surface; backend has no
 *  single `status=resolved` enum, so we fetch unfiltered and drop
 *  pending client-side. */
export type ApprovalsPageMode = 'pending' | 'resolved';

const REFRESH_INTERVAL_MS = 15_000;

@customElement('rm-approvals-page')
export class ApprovalsPage extends LitElement {
  /** Which slice of the approval list to render. Default `pending`
   *  keeps the legacy `<rm-approvals-page>` behaviour intact. Activity
   *  shell passes `resolved` for the audit log tab. */
  @property() mode: ApprovalsPageMode = 'pending';

  @state() private rows: ApprovalRequest[] = [];
  @state() private loading = true;
  @state() private error: string | null = null;
  @state() private me: Me | null = null;
  @state() private scope: Scope = 'mine';

  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly api = getApiClient();

  protected override createRenderRoot() {
    return this;
  }

  override async connectedCallback() {
    super.connectedCallback();
    // Fetch identity so we can decide which rows offer the
    // approve/reject affordance (must be in resolved_approvers).
    try {
      this.me = await this.api.getMe();
    } catch {
      this.me = null;
    }
    await this.refresh();
    this.timer = setInterval(() => void this.refresh(), REFRESH_INTERVAL_MS);
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  override updated(changed: Map<string, unknown>): void {
    // Toggling `mode` between the chat-shell "Approvals" page and the
    // activity-shell "Approval log" tab must trigger a refetch — the
    // resolved view fetches unfiltered rows where the pending view
    // would have fetched only status='pending'.
    if (changed.has('mode')) {
      void this.refresh();
    }
  }

  private async refresh(): Promise<void> {
    this.loading = true;
    this.error = null;
    try {
      const rows = await this.api.listApprovals({
        scope: this.scope,
        // `resolved` view fetches unfiltered (backend has no
        // collective resolved status) and prunes pending below.
        status: this.mode === 'pending' ? 'pending' : null,
      });
      this.rows =
        this.mode === 'resolved'
          ? rows.filter((r) => r.status !== 'pending')
          : rows;
    } catch (err) {
      this.rows = [];
      this.error =
        err instanceof ApiError
          ? `${err.status} ${err.body?.code ?? ''} — ${err.message}`
          : (err as Error).message ?? 'unknown error';
    } finally {
      this.loading = false;
    }
  }

  private setScope(scope: Scope): void {
    this.scope = scope;
    void this.refresh();
  }

  private onDecided(): void {
    // Pull truth after a decision so the row leaves the list (it
    // is no longer status=pending). Cheaper than locally mutating
    // because the server is canonical and a concurrent
    // approver/expiry could change other rows too.
    void this.refresh();
  }

  private renderEmpty() {
    const title =
      this.mode === 'pending' ? 'No pending approvals' : 'No approval history';
    const subtitle =
      this.mode === 'pending'
        ? `You're all caught up — new approvals will appear here in real time.`
        : 'Resolved approvals will be listed here as they accumulate.';
    return html`
      <div
        class="border border-dashed border-surface-3 dark:border-d-surface-3
          rounded-lg px-4 py-8 text-center text-ink-3 dark:text-d-ink-3"
      >
        <div class="text-[13px] font-medium">${title}</div>
        <div class="text-[12px] mt-1">${subtitle}</div>
      </div>
    `;
  }

  private renderRow(r: ApprovalRequest) {
    const action = (r.actions ?? [])[0] ?? {};
    const toolName = String(action.tool_name ?? '');
    const args = (action.params ?? {}) as Record<string, unknown>;
    // `resolved` view renders read-only — no decide affordance for
    // already-decided rows, regardless of who the caller is.
    const canDecide =
      this.mode === 'pending' &&
      !!this.me &&
      (r.resolved_approvers ?? []).includes(this.me.user_id);
    // For the log surface, surface the actual final state so the
    // inline-approval card renders "Approved / Rejected / Expired"
    // tone instead of the pending decide buttons.
    // Backend lifecycle status (`rejected`) maps to the inline-approval
    // UI status (`denied`); `executing` / `executed` / `skipped` /
    // `execution_failed` / `execution_stale` collapse to `unknown`
    // because the card has no dedicated tone for those — they only
    // exist for the post-decide execution phase.
    const rowStatus =
      this.mode === 'pending'
        ? 'pending'
        : r.status === 'approved'
          ? 'approved'
          : r.status === 'rejected'
            ? 'denied'
            : r.status === 'expired'
              ? 'expired'
              : r.status === 'cancelled'
                ? 'cancelled'
                : 'unknown';
    return html`
      <li class="list-none">
        <rm-inline-approval
          approval-id=${r.id}
          tool-name=${toolName}
          .args=${args}
          mcp-server=${r.mcp_server_name}
          status=${rowStatus}
          .canDecide=${canDecide}
          @rm-approval-decided=${() => this.onDecided()}
        ></rm-inline-approval>
        <div class="text-[11px] text-ink-3 dark:text-d-ink-3 mt-1 ml-1">
          requested ${r.requested_at.slice(0, 19).replace('T', ' ')} · expires
          ${r.expires_at.slice(0, 19).replace('T', ' ')}
        </div>
      </li>
    `;
  }

  override render() {
    return html`
      <div class="h-full w-full overflow-y-auto px-6 py-6">
        <div class="max-w-3xl mx-auto">
          <div class="flex items-baseline justify-between mb-4">
            <div>
              <h1 class="text-[20px] font-semibold text-ink-0 dark:text-d-ink-0">
                ${this.mode === 'pending' ? 'Approvals' : 'Approval log'}
              </h1>
              <p class="text-[13px] text-ink-3 dark:text-d-ink-3 mt-0.5">
                ${this.mode === 'pending'
                  ? 'Pending tool calls awaiting an approver decision.'
                  : 'Past approval decisions on this tenant.'}
              </p>
            </div>
            <div class="flex gap-2">
              <button
                type="button"
                class=${this.scopeBtnClass('mine')}
                @click=${() => this.setScope('mine')}
              >Mine</button>
              <button
                type="button"
                class=${this.scopeBtnClass('all')}
                @click=${() => this.setScope('all')}
              >All</button>
              <button
                type="button"
                class="text-[12px] px-2.5 py-1 rounded-md border border-surface-3
                  dark:border-d-surface-3 text-ink-2 dark:text-d-ink-2
                  hover:bg-surface-2 dark:hover:bg-d-surface-2 cursor-pointer"
                @click=${() => void this.refresh()}
              >Refresh</button>
            </div>
          </div>

          ${this.loading && this.rows.length === 0
            ? html`<div class="text-[13px] text-ink-3 dark:text-d-ink-3">Loading…</div>`
            : this.error
              ? html`
                  <div
                    class="border border-red-200 dark:border-red-800 bg-red-50
                      dark:bg-red-900/20 text-red-700 dark:text-red-300
                      text-[13px] px-3 py-2 rounded-lg"
                  >${this.error}</div>
                `
              : this.rows.length === 0
                ? this.renderEmpty()
                : html`<ul class="flex flex-col gap-3">
                    ${this.rows.map((r) => this.renderRow(r))}
                  </ul>`}
        </div>
      </div>
    `;
  }

  private scopeBtnClass(target: Scope): string {
    const base =
      'text-[12px] px-2.5 py-1 rounded-md border cursor-pointer';
    if (this.scope === target) {
      return `${base} border-brand bg-brand text-white`;
    }
    return (
      `${base} border-surface-3 dark:border-d-surface-3 text-ink-2 ` +
      `dark:text-d-ink-2 hover:bg-surface-2 dark:hover:bg-d-surface-2`
    );
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'rm-approvals-page': ApprovalsPage;
  }
}
