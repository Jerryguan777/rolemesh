// <rm-approvals-inbox> — top-bar popover surfacing every pending tool
// approval across the current tenant the user is the approver for
// (.hitl-ui/spec.md §4; Appendix C.2). Triage-only: a row navigates to
// the chat where the decision card lives — the inbox NEVER decides, so
// it dispatches no `approval-decision` frame and renders no approve /
// reject controls.
//
// State scope: the component self-holds its own `PendingApprovalRequest[]`
// fetched from `GET /api/v1/approval-requests` WITHOUT a conversation_id
// filter (tenant-wide; RLS scopes to the user's tenant server-side). The
// approval-store is deliberately NOT lifted to the shell — the inbox and
// the chat panel each keep their own view; the inbox is fed by explicit
// re-fetch triggers (§4.8 "Option A" minus the shared singleton):
//
//   1. popover open                  (willUpdate on `open` false→true)
//   2. active conversation switch    (willUpdate on `activeConversationId`)
//   3. tab becomes visible           (document `visibilitychange`)
//   4. an `approval-activity` event  (the shell calls `refresh()`)
//   + a ~30s slow poll WHILE OPEN only (cleared on close) as a backstop.
//
// There is no high-frequency standing timer — the badge is event-driven,
// not polled.
//
// Light DOM (createRenderRoot → this) so the shell's design-token CSS and
// the global highlight keyframe (defined in chat-shell) apply, matching
// the other v2 surfaces.

import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import {
  getApiClient,
  type Conversation,
  type Coworker,
  type PendingApprovalRequest,
} from '../api/client.js';
import { iconInbox } from './icons.js';

/** Countdown turns urgent (deep red) under this many ms remaining (§4.1
 *  / §4.3). Matches the chat card's 5-minute threshold. */
const URGENT_MS = 5 * 60 * 1000;
/** Slow backstop poll cadence while the popover is open (§4.8). */
const POLL_MS = 30_000;
/** Highlight pulse duration on the jumped-to card (§4.7). */
const HIGHLIGHT_MS = 1800;

/** Join up to the first 4 param entries as `k: v · k: v · …`, each value
 *  stringified and truncated to 30 chars (§4.4 / Appendix C.2). A missing
 *  or non-object `params` (or an empty object) collapses to '' so the row
 *  omits the line. Exported for unit testing. */
export function paramsInline(params: unknown): string {
  if (!params || typeof params !== 'object' || Array.isArray(params)) return '';
  const entries = Object.entries(params as Record<string, unknown>);
  if (entries.length === 0) return '';
  return entries
    .slice(0, 4)
    .map(([k, v]) => {
      let val = typeof v === 'string' ? v : JSON.stringify(v) ?? String(v);
      if (val.length > 30) val = val.slice(0, 30) + '…';
      return `${k}: ${val}`;
    })
    .join(' · ');
}

/** Countdown text from an ISO `expires_at` against `now` (epoch ms):
 *  `Xm left` / `Xs left` / `expired` (Appendix C.2 `formatCountdown`).
 *  An unparseable / missing timestamp yields '' so the row drops it.
 *  Exported for unit testing. */
export function formatCountdown(expiresAt: string | null, now: number): string {
  if (!expiresAt) return '';
  const exp = Date.parse(expiresAt);
  if (Number.isNaN(exp)) return '';
  const ms = exp - now;
  if (ms <= 0) return 'expired';
  const totalSec = Math.floor(ms / 1000);
  if (totalSec < 60) return `${totalSec}s left`;
  return `${Math.floor(totalSec / 60)}m left`;
}

/** Is this request within the urgent window (or already past expiry)?
 *  Exported for unit testing the badge threshold. */
export function isUrgent(expiresAt: string | null, now: number): boolean {
  if (!expiresAt) return false;
  const exp = Date.parse(expiresAt);
  if (Number.isNaN(exp)) return false;
  return exp - now < URGENT_MS;
}

/** {total, urgent-count} emitted on the `approvals-count` event so the
 *  shell can paint its badge without owning the store. */
export interface ApprovalsCountDetail {
  total: number;
  urgent: number;
}

@customElement('rm-approvals-inbox')
export class ApprovalsInbox extends LitElement {
  /** Whether the popover panel is visible. Driven by the shell's top-bar
   *  toggle (the shell owns which popover is open). */
  @property({ type: Boolean }) open = false;
  /** The chat panel's active conversation — a change is a re-fetch
   *  trigger (§4.8). */
  @property() activeConversationId: string | null = null;
  /** Tenant coworkers, for `coworker_id → name` resolution (§8.3). */
  @property({ attribute: false }) coworkers: Coworker[] = [];
  /** Conversations known to the shell (active coworker's), for best-effort
   *  `conversation_id → title` resolution on the row meta line. */
  @property({ attribute: false }) conversations: Conversation[] = [];
  /** Wired by the shell: switch sidebar coworker + open the conversation
   *  the gated card lives in. Awaited before the inbox attempts to scroll
   *  to and highlight the card (§4.7). */
  @property({ attribute: false }) jumpHandler:
    | ((
        conversationId: string | null,
        coworkerId: string | null,
      ) => void | Promise<void>)
    | null = null;

  /** Tenant-wide pending set — the inbox's own store (NOT the chat
   *  panel's). */
  @state() private requests: PendingApprovalRequest[] = [];
  /** Re-read at 1Hz while open so the countdowns tick. */
  @state() private now = Date.now();

  private readonly api = getApiClient();
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private tickTimer: ReturnType<typeof setInterval> | null = null;
  private highlightTimer: ReturnType<typeof setTimeout> | null = null;
  /** Last {total,urgent} dispatched, so we only emit on change. */
  private lastCount: ApprovalsCountDetail | null = null;

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback(): void {
    super.connectedCallback();
    document.addEventListener('visibilitychange', this.onVisibility);
    // Seed the badge once on mount: the count must be visible before the
    // user ever opens the popover.
    void this.refresh();
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    document.removeEventListener('visibilitychange', this.onVisibility);
    this.stopPoll();
    this.stopTick();
    if (this.highlightTimer != null) {
      clearTimeout(this.highlightTimer);
      this.highlightTimer = null;
    }
  }

  protected override willUpdate(changed: Map<string, unknown>): void {
    if (changed.has('open')) {
      if (this.open) {
        void this.refresh();
        this.startPoll();
        this.startTick();
      } else {
        this.stopPoll();
        this.stopTick();
      }
    }
    // Conversation switch — re-fetch, but skip the initial undefined→value
    // assignment the shell makes on first paint (the connectedCallback
    // seed already covers that, and double-fetching is wasteful).
    if (
      changed.has('activeConversationId') &&
      changed.get('activeConversationId') !== undefined
    ) {
      void this.refresh();
    }
  }

  // --- Triggers --------------------------------------------------------------

  private onVisibility = (): void => {
    if (document.visibilityState === 'visible') void this.refresh();
  };

  /** Re-fetch the tenant-wide pending set. Public so the shell can call it
   *  on an `approval-activity` bubble from the chat panel. Failures are
   *  swallowed (logged) — a stale list is better than a thrown render. */
  async refresh(): Promise<void> {
    try {
      const rows = await this.api.listPendingApprovals();
      this.requests = rows;
      this.now = Date.now();
      this.emitCount();
    } catch (err) {
      console.warn('approvals-inbox: listPendingApprovals failed', err);
    }
  }

  private startPoll(): void {
    if (this.pollTimer != null) return;
    this.pollTimer = setInterval(() => void this.refresh(), POLL_MS);
  }

  private stopPoll(): void {
    if (this.pollTimer != null) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  private startTick(): void {
    if (this.tickTimer != null) return;
    this.tickTimer = setInterval(() => {
      this.now = Date.now();
      this.emitCount();
    }, 1000);
  }

  private stopTick(): void {
    if (this.tickTimer != null) {
      clearInterval(this.tickTimer);
      this.tickTimer = null;
    }
  }

  /** Dispatch {total, urgent} to the shell badge, but only when it changes
   *  — keeps the badge from re-rendering every 1Hz tick when nothing
   *  crossed the urgency line. */
  private emitCount(): void {
    const total = this.requests.length;
    const urgent = this.requests.filter((r) =>
      isUrgent(r.expires_at ?? null, this.now),
    ).length;
    if (
      this.lastCount &&
      this.lastCount.total === total &&
      this.lastCount.urgent === urgent
    ) {
      return;
    }
    this.lastCount = { total, urgent };
    this.dispatchEvent(
      new CustomEvent<ApprovalsCountDetail>('approvals-count', {
        detail: { total, urgent },
        bubbles: true,
        composed: true,
      }),
    );
  }

  // --- Lookups ---------------------------------------------------------------

  private coworkerName(id: string | null | undefined): string | null {
    if (!id) return null;
    return this.coworkers.find((c) => c.id === id)?.name ?? null;
  }

  private conversationTitle(id: string | null | undefined): string | null {
    if (!id) return null;
    const conv = this.conversations.find((c) => c.id === id);
    const name = conv?.name?.trim();
    return name ? name : null;
  }

  // --- Navigation (§4.7) -----------------------------------------------------

  /** Triage tap: close the popover, ask the shell to switch coworker +
   *  conversation, then (after our own update settles — NO setTimeout, per
   *  §4.7) scroll the matching card into view and pulse it. Cross-coworker
   *  jumps reload the page in the shell, so the scroll is best-effort there
   *  and authoritative for an already-loaded conversation. */
  private async jumpToConv(req: PendingApprovalRequest): Promise<void> {
    // Tell the shell to close the popover (it owns `open`).
    this.dispatchEvent(
      new CustomEvent('inbox-close', { bubbles: true, composed: true }),
    );
    this.open = false;
    if (this.jumpHandler) {
      await this.jumpHandler(
        req.conversation_id ?? null,
        req.coworker_id ?? null,
      );
    }
    await this.updateComplete;
    const card = document.querySelector<HTMLElement>(
      `[data-appr-id="${req.request_id}"]`,
    );
    if (!card) return;
    if (typeof card.scrollIntoView === 'function') {
      card.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
    card.classList.add('rm-appr-highlight');
    if (this.highlightTimer != null) clearTimeout(this.highlightTimer);
    this.highlightTimer = setTimeout(() => {
      card.classList.remove('rm-appr-highlight');
      this.highlightTimer = null;
    }, HIGHLIGHT_MS);
  }

  // --- Render ----------------------------------------------------------------

  /** Ascending by `expires_at` — most urgent first (§4.5). A missing
   *  timestamp sorts last (it has no deadline pressure). */
  private sorted(): PendingApprovalRequest[] {
    return [...this.requests].sort((a, b) => {
      const ta = a.expires_at ? Date.parse(a.expires_at) : Infinity;
      const tb = b.expires_at ? Date.parse(b.expires_at) : Infinity;
      return ta - tb;
    });
  }

  override render(): TemplateResult | typeof nothing {
    if (!this.open) return nothing;
    const items = this.sorted();
    const total = items.length;
    const urgent = items.filter((r) =>
      isUrgent(r.expires_at ?? null, this.now),
    ).length;
    let sub = 'all clear';
    if (total > 0) {
      sub = `${total} to review`;
    }
    return html`
      <style>
        rm-approvals-inbox .appr-panel {
          position: absolute;
          z-index: 50;
          top: calc(100% + 8px);
          right: 0;
          width: 380px;
          max-height: 70vh;
          overflow-y: auto;
          background: var(--rm-surface);
          border: 1px solid var(--rm-border-2);
          border-radius: var(--rm-r);
          box-shadow: var(--rm-shadow-md);
          animation: rm-pop 0.12s ease both;
        }
        rm-approvals-inbox .appr-hd {
          padding: 13px 15px;
          border-bottom: 1px solid var(--rm-border);
          font-size: 13px;
          font-weight: 600;
          color: var(--rm-ink);
        }
        rm-approvals-inbox .appr-hd small {
          font-weight: 400;
          color: var(--rm-ink-3);
          margin-left: 6px;
        }
        rm-approvals-inbox .appr-hd .hd-urgent {
          color: var(--rm-bad);
          font-weight: 500;
        }
        rm-approvals-inbox .appr-item {
          padding: 12px 15px;
          border-bottom: 1px solid var(--rm-border);
          cursor: pointer;
          transition: background 0.1s;
          width: 100%;
          text-align: left;
          background: none;
          border-left: none;
          border-right: none;
          border-top: none;
          color: inherit;
          font-family: inherit;
          display: block;
        }
        rm-approvals-inbox .appr-item:hover {
          background: var(--rm-surface-2);
        }
        rm-approvals-inbox .appr-item:last-of-type {
          border-bottom: none;
        }
        rm-approvals-inbox .appr-item .h {
          font-size: 13.5px;
          font-weight: 500;
          color: var(--rm-ink);
          margin: 0 0 3px;
          line-height: 1.4;
          display: flex;
          align-items: center;
          flex-wrap: wrap;
          gap: 2px;
        }
        rm-approvals-inbox .appr-item .toolname {
          font-family: var(--rm-font-mono, monospace);
          font-size: 11px;
          color: var(--rm-ink-2);
          background: rgba(0, 0, 0, 0.05);
          padding: 1px 6px;
          border-radius: 4px;
          margin-left: 6px;
          font-weight: 400;
        }
        rm-approvals-inbox .appr-item .m {
          font-size: 11.5px;
          color: var(--rm-ink-3);
          margin: 0 0 8px;
          line-height: 1.5;
        }
        rm-approvals-inbox .appr-item .exp {
          font-variant-numeric: tabular-nums;
        }
        rm-approvals-inbox .appr-item .exp.urgent {
          color: var(--rm-bad);
          font-weight: 600;
        }
        rm-approvals-inbox .appr-item .params-inline {
          font-size: 11.5px;
          color: var(--rm-ink-3);
          font-family: var(--rm-font-mono, monospace);
          line-height: 1.45;
          margin: 0 0 9px;
          display: -webkit-box;
          -webkit-line-clamp: 2;
          line-clamp: 2;
          -webkit-box-orient: vertical;
          overflow: hidden;
          word-break: break-word;
        }
        rm-approvals-inbox .appr-item .a {
          display: flex;
        }
        rm-approvals-inbox .appr-item .a .btn-review {
          flex: 1;
          padding: 5px 12px;
          border-radius: 7px;
          font-size: 12.5px;
          font-weight: 500;
          border: 1px solid var(--rm-border-2);
          background: var(--rm-surface-2);
          color: var(--rm-ink-2);
          cursor: pointer;
          font-family: inherit;
          text-align: center;
          transition: 0.13s;
        }
        rm-approvals-inbox .appr-item .a .btn-review:hover {
          border-color: var(--rm-accent);
          color: var(--rm-ink);
        }
        rm-approvals-inbox .appr-empty {
          padding: 36px 24px 32px;
          text-align: center;
        }
        rm-approvals-inbox .appr-empty .empty-check {
          width: 42px;
          height: 42px;
          border-radius: 50%;
          background: var(--rm-good-subtle, rgba(47, 125, 91, 0.12));
          color: var(--rm-good);
          display: flex;
          align-items: center;
          justify-content: center;
          margin: 0 auto 12px;
        }
        rm-approvals-inbox .appr-empty p {
          font-size: 13.5px;
          color: var(--rm-ink-2);
          margin: 0 0 4px;
          line-height: 1.5;
        }
        rm-approvals-inbox .appr-empty .empty-sub {
          font-size: 12px;
          color: var(--rm-ink-3);
        }
        rm-approvals-inbox .appr-foot {
          padding: 9px 14px 11px;
          border-top: 1px solid var(--rm-border-2);
          font-size: 11.5px;
          color: var(--rm-ink-3);
          text-align: center;
        }
      </style>
      <div class="appr-panel" data-menu="approvals" role="dialog" aria-label="Approvals inbox" data-testid="approvals-panel">
        <div class="appr-hd" data-testid="approvals-heading">
          Approvals
          <small>
            ${total === 0
              ? sub
              : html`${total} to review${urgent > 0
                  ? html` ·
                      <span class="hd-urgent" data-testid="approvals-urgent-note"
                        >${urgent} expiring soon</span
                      >`
                  : nothing}`}
          </small>
        </div>
        ${total === 0
          ? this.renderEmpty()
          : items.map((r) => this.renderRow(r))}
        <div class="appr-foot">Updates live as coworkers request approvals.</div>
      </div>
    `;
  }

  private renderEmpty(): TemplateResult {
    return html`
      <div class="appr-empty" data-testid="approvals-empty">
        <div class="empty-check">
          <svg
            width="22"
            height="22"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2.5"
            aria-hidden="true"
          >
            <path d="M20 6 9 17l-5-5" />
          </svg>
        </div>
        <p>Nothing waiting for you.</p>
        <p class="empty-sub">Decisions you've made show up in Activity.</p>
      </div>
    `;
  }

  private renderRow(r: PendingApprovalRequest): TemplateResult {
    const name = this.coworkerName(r.coworker_id);
    const tool = [r.mcp_server_name, r.tool_name].filter(Boolean).join('.');
    const title = this.conversationTitle(r.conversation_id);
    const cd = formatCountdown(r.expires_at ?? null, this.now);
    const urgent = isUrgent(r.expires_at ?? null, this.now);
    const inline = paramsInline(r.params);
    return html`
      <div
        class="appr-item"
        role="button"
        tabindex="0"
        data-testid="approvals-row"
        data-appr-row-id=${r.request_id}
        @click=${() => void this.jumpToConv(r)}
        @keydown=${(e: KeyboardEvent) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            void this.jumpToConv(r);
          }
        }}
      >
        <div class="h">
          ${name ? `${name} coworker` : 'Coworker'}
          ${tool
            ? html`<span class="toolname" data-testid="approvals-row-tool"
                >${tool}</span
              >`
            : nothing}
        </div>
        <div class="m">
          ${title ? html`"${title}" · ` : nothing}
          ${cd
            ? html`<span
                class="exp ${urgent ? 'urgent' : ''}"
                data-testid="approvals-row-countdown"
                data-urgent=${urgent ? 'true' : 'false'}
                >${cd}</span
              >`
            : nothing}
        </div>
        ${inline
          ? html`<div class="params-inline" data-testid="approvals-row-params">
              ${inline}
            </div>`
          : nothing}
        <div class="a">
          <button
            type="button"
            class="btn-review"
            data-testid="approvals-row-open"
            @click=${(e: Event) => {
              e.stopPropagation();
              void this.jumpToConv(r);
            }}
          >
            Open in chat →
          </button>
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'rm-approvals-inbox': ApprovalsInbox;
  }
}
