// HITL approval card (.hitl-ui/spec.md §3; docs/12-hitl-approval-architecture.md §10 S5).
// Renders one tool-approval request inside the chat stream as a rich decision
// surface: tool identity, the raw params the decision turns on, the agent's
// optional rationale, a live countdown, and a Reject-with-note / Approve action
// pair. After resolution the card stays in place showing its terminal state —
// it is the user's scrollable record of the decision (§3.6).
//
// On Approve the card dispatches an `approval-decision` CustomEvent immediately.
// On Reject it first expands an inline note form (§3.5); only the form's Reject
// button dispatches. chat-panel relays the event to the orchestrator via the v1
// WS frame (`V1WsClient.sendApprovalDecision`). The card never sends the approver
// identity — that is stamped server-side from the verified WS ticket (IDOR
// guard), so this component only knows the request id + verb + optional note.
//
// Light DOM (createRenderRoot → this) so the chat surface's Tailwind utility
// classes apply, matching chat-panel / message-list.

import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import type { ApprovalStatus, ApprovalTriggeredBy } from './approval-store.js';
import { checkLabel } from './safety-catalog.js';
import { iconShield } from './icons.js';

/** Fired when the user confirms a decision. Bubbles + composed so chat-panel
 *  (the light-DOM host) catches it. `note` is present only on a Reject that
 *  went through the inline form with non-empty text (§3.5). */
export interface ApprovalDecisionDetail {
  requestId: string;
  decision: 'approve' | 'reject';
  note?: string;
}

/** Terminal-state header presentation (§3.6). */
const RESOLVED_PRESENTATION: Record<
  Exclude<ApprovalStatus, 'pending'>,
  { label: string; classes: string }
> = {
  approved: {
    label: '✓ Approved',
    classes:
      'bg-emerald-50 dark:bg-emerald-900/20 text-emerald-700 dark:text-emerald-300',
  },
  rejected: {
    label: '✕ Rejected',
    classes: 'bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-300',
  },
  expired: {
    label: '⏰ Timed out',
    classes: 'bg-surface-2 dark:bg-d-surface-2 text-ink-2 dark:text-d-ink-2',
  },
  cancelled: {
    label: '⊘ Cancelled',
    classes: 'bg-surface-2 dark:bg-d-surface-2 text-ink-2 dark:text-d-ink-2',
  },
};

/** How many params to show before collapsing behind a "Show all" (§3.3). */
const PARAMS_COLLAPSE_THRESHOLD = 8;
const PARAMS_COLLAPSED_COUNT = 6;
/** Soft truncation for the rationale (§3.4). */
const RATIONALE_TRUNCATE = 400;
/** Countdown turns red under this many ms remaining (§3.2). */
const URGENT_MS = 5 * 60 * 1000;

@customElement('rm-approval-card')
export class ApprovalCard extends LitElement {
  @property() requestId = '';
  @property() actionSummary: string | null = null;
  @property() status: ApprovalStatus = 'pending';
  /** Disables the buttons while a decision is in flight (set by the host
   *  between the click and the `event.approval.resolved` echo). */
  @property({ type: Boolean }) busy = false;

  // --- Rich-card fields (§3 / spec Appendix C.5) ---
  @property() mcpServerName: string | null = null;
  @property() toolName: string | null = null;
  @property({ attribute: false }) params: Record<string, unknown> = {};
  @property() rationale: string | null = null;
  /** ISO-8601; drives the "2m ago" meta line. */
  @property() requestedAt: string | null = null;
  /** ISO-8601; drives the live countdown. */
  @property() expiresAt: string | null = null;
  /** Resolved display name of the requesting coworker (null ⇒ omit). */
  @property() coworkerName: string | null = null;
  /** Epoch ms of the terminal flip; null while pending. */
  @property({ type: Number }) resolvedAt: number | null = null;
  /** The user's rejection note, echoed back as "YOUR REASON" on a resolved
   *  rejected card (§3.6). May arrive via prop (store) or be remembered from
   *  the form the user just submitted. */
  @property() note: string | null = null;
  /** Safety-rule provenance (§3.10). When kind is "safety_rule" the card shows
   *  an amber "paused by a safety rule" banner above the tool chip; null (a
   *  business-policy approval) or an unknown kind shows nothing. */
  @property({ attribute: false }) triggeredBy: ApprovalTriggeredBy = null;

  /** Inline reject-note form open? */
  @state() private rejecting = false;
  /** Params disclosure expanded (when over the collapse threshold)? */
  @state() private paramsExpanded = false;
  /** Rationale "▸ more" expanded? */
  @state() private rationaleExpanded = false;
  /** Wall clock, re-read at 1Hz so the countdown ticks (§3.2). */
  @state() private now = Date.now();
  /** Locally-remembered note from the form submit, so the resolved card can
   *  show it even when the host doesn't plumb `note` back through the store. */
  private submittedNote: string | null = null;

  private timer: ReturnType<typeof setInterval> | null = null;

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback(): void {
    super.connectedCallback();
    if (this.status === 'pending') this.startTicking();
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    this.stopTicking();
  }

  protected override willUpdate(changed: Map<string, unknown>): void {
    if (changed.has('status')) {
      if (this.status === 'pending') this.startTicking();
      else this.stopTicking();
    }
  }

  private startTicking(): void {
    if (this.timer != null) return;
    this.timer = setInterval(() => {
      this.now = Date.now();
    }, 1000);
  }

  private stopTicking(): void {
    if (this.timer != null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  // --- Decision dispatch -----------------------------------------------------

  private canAct(): boolean {
    return !this.busy && this.status === 'pending';
  }

  private approve(): void {
    if (!this.canAct()) return;
    this.dispatchEvent(
      new CustomEvent<ApprovalDecisionDetail>('approval-decision', {
        detail: { requestId: this.requestId, decision: 'approve' },
        bubbles: true,
        composed: true,
      }),
    );
  }

  /** First Reject click: open the note form. Does NOT submit (§3.5). */
  private openRejectForm(): void {
    if (!this.canAct()) return;
    this.rejecting = true;
  }

  private cancelReject(): void {
    this.rejecting = false;
  }

  private confirmReject(): void {
    if (!this.canAct()) return;
    const ta = this.querySelector<HTMLTextAreaElement>(
      '[data-testid="approval-note"]',
    );
    const note = ta?.value.trim() ?? '';
    this.submittedNote = note || null;
    this.dispatchEvent(
      new CustomEvent<ApprovalDecisionDetail>('approval-decision', {
        detail: {
          requestId: this.requestId,
          decision: 'reject',
          // Empty submit is fine — the field is nullable; omit it then (§3.5).
          ...(note ? { note } : {}),
        },
        bubbles: true,
        composed: true,
      }),
    );
  }

  // --- Value / time formatting ----------------------------------------------

  private paramDisplay(v: unknown): string {
    if (v === null || v === undefined) return 'null';
    if (typeof v === 'boolean' || typeof v === 'number') return String(v);
    if (typeof v === 'string') return `"${v}"`;
    try {
      return JSON.stringify(v);
    } catch {
      return String(v);
    }
  }

  private relativeTime(iso: string): string | null {
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return null;
    const diff = this.now - t;
    if (diff < 0) return 'just now';
    if (diff < 60_000) return 'just now';
    if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
    if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
    return 'yesterday';
  }

  private absoluteTime(ms: number): string {
    return new Date(ms).toLocaleTimeString([], {
      hour: 'numeric',
      minute: '2-digit',
    });
  }

  /** Countdown text + urgency from `expiresAt` and the ticking clock (§3.2). */
  private countdown(): { text: string; urgent: boolean } | null {
    if (!this.expiresAt) return null;
    const exp = Date.parse(this.expiresAt);
    if (Number.isNaN(exp)) return null;
    const ms = exp - this.now;
    if (ms <= 0) return { text: 'expired', urgent: true };
    const totalSec = Math.floor(ms / 1000);
    if (totalSec < 60) return { text: `${totalSec}s left`, urgent: true };
    return { text: `${Math.floor(totalSec / 60)}m left`, urgent: ms < URGENT_MS };
  }

  // --- Subrenders ------------------------------------------------------------

  private renderHeader(): TemplateResult {
    if (this.status !== 'pending') {
      const pres = RESOLVED_PRESENTATION[this.status];
      const ts =
        this.resolvedAt != null
          ? html`<span
              class="ml-1 font-normal opacity-70 text-[11px]"
              data-testid="approval-resolved-time"
              >at ${this.absoluteTime(this.resolvedAt)}</span
            >`
          : nothing;
      return html`
        <div
          class="flex items-center gap-2 px-3.5 py-2 text-[12px] font-semibold border-b border-black/5 dark:border-white/10 ${pres.classes}"
          data-testid="approval-header"
        >
          <span data-testid="approval-status">${pres.label}</span>${ts}
        </div>
      `;
    }
    const cd = this.countdown();
    return html`
      <div
        class="flex items-center gap-2 px-3.5 py-2 text-[12px] font-semibold border-b border-amber-200/60 dark:border-amber-700/40 bg-amber-50 dark:bg-amber-900/20 text-amber-700 dark:text-amber-400"
        data-testid="approval-header"
      >
        <span class="uppercase tracking-wide text-[11px]">⚠ Approval needed</span>
        ${cd
          ? html`<span
              class="ml-auto font-medium text-[11px] tabular-nums ${cd.urgent
                ? 'text-red-600 dark:text-red-400'
                : 'opacity-80'}"
              data-testid="approval-countdown"
              data-urgent=${cd.urgent ? 'true' : 'false'}
              >${cd.text}</span
            >`
          : nothing}
      </div>
    `;
  }

  private renderMeta(): TemplateResult | typeof nothing {
    const parts: string[] = [];
    if (this.coworkerName) parts.push(`${this.coworkerName} coworker`);
    if (this.requestedAt) {
      const rel = this.relativeTime(this.requestedAt);
      if (rel) parts.push(rel);
    }
    if (parts.length === 0) return nothing;
    return html`
      <div
        class="text-[11px] text-ink-3 dark:text-d-ink-3 mb-2"
        data-testid="approval-meta"
      >
        ${parts.join(' · ')}
      </div>
    `;
  }

  // §3.10 — amber provenance banner for a safety-triggered approval. Returns
  // nothing for a business-policy approval (triggeredBy null) OR an unknown
  // kind (forward-compatible: a future "scheduled_task" degrades to no banner).
  // Stage is intentionally omitted — the decision-maker cares WHICH check
  // caught it, not WHEN in the pipeline.
  private renderSafetyBanner(): TemplateResult | typeof nothing {
    const tb = this.triggeredBy;
    if (!tb || tb.kind !== 'safety_rule') return nothing;
    return html`
      <div
        class="flex items-start gap-2 mb-2.5 px-2.5 py-2 rounded-md text-[12.5px]
               bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200
               border-l-2 border-amber-400 dark:border-amber-600"
        data-testid="approval-safety-banner"
      >
        <span class="shrink-0 mt-px text-amber-600 dark:text-amber-400"
          >${iconShield(14)}</span
        >
        <span class="flex-1 min-w-0"
          >Paused by a safety rule — <b>${checkLabel(tb.check_id)}</b></span
        >
        <button
          type="button"
          class="shrink-0 underline whitespace-nowrap hover:text-amber-900 dark:hover:text-amber-100"
          data-testid="approval-safety-link"
          @click=${() => this.jumpToSafetyDecision(tb.rule_id)}
        >
          view in safety log →
        </button>
      </div>
    `;
  }

  // Navigate to Settings → Safety log filtered by rule_id (spec §3.10).
  // The log mounts the rule_id chip from the URL's ?rule_id= query param
  // (G6 / PR #57); landing on a filtered list lets the user see the rule's
  // broader behavior before deciding.
  private jumpToSafetyDecision(ruleId: string): void {
    location.hash = `#/manage/safety-log?rule_id=${encodeURIComponent(ruleId)}`;
  }

  private renderToolChip(): TemplateResult | typeof nothing {
    if (!this.mcpServerName && !this.toolName) return nothing;
    const label = [this.mcpServerName, this.toolName]
      .filter(Boolean)
      .join(' · ');
    return html`
      <div
        class="inline-block font-mono text-[13px] text-ink-1 dark:text-d-ink-1 bg-black/5 dark:bg-white/10 px-2.5 py-1 rounded-md mb-2.5"
        data-testid="approval-tool"
      >
        ${label}
      </div>
    `;
  }

  private renderParams(): TemplateResult | typeof nothing {
    const entries = Object.entries(this.params ?? {});
    if (entries.length === 0) return nothing;
    const over = entries.length > PARAMS_COLLAPSE_THRESHOLD;
    const shown =
      over && !this.paramsExpanded
        ? entries.slice(0, PARAMS_COLLAPSED_COUNT)
        : entries;
    return html`
      <div
        class="border border-border-2 dark:border-d-border-2 rounded-lg bg-surface-2 dark:bg-d-surface-2 mb-2.5 overflow-hidden"
        data-testid="approval-params"
      >
        ${shown.map(
          ([k, v]) => html`
            <div
              class="grid grid-cols-[130px_1fr] gap-3 px-3 py-1.5 text-[12.5px] border-b border-border-2 dark:border-d-border-2 last:border-b-0 items-start"
              data-testid="approval-param-row"
            >
              <span
                class="font-mono text-[11.5px] text-ink-3 dark:text-d-ink-3 break-words"
                data-testid="approval-param-key"
                >${k}</span
              >
              <span
                class="font-mono text-[12px] text-ink-1 dark:text-d-ink-1 break-words whitespace-pre-wrap leading-relaxed"
                data-testid="approval-param-value"
                >${this.paramDisplay(v)}</span
              >
            </div>
          `,
        )}
        ${over
          ? html`<button
              type="button"
              class="w-full text-left px-3 py-1.5 text-[11.5px] text-ink-3 dark:text-d-ink-3 hover:bg-black/5 dark:hover:bg-white/5 cursor-pointer"
              data-testid="approval-params-toggle"
              @click=${() => (this.paramsExpanded = !this.paramsExpanded)}
            >
              ${this.paramsExpanded
                ? '▾ Show fewer'
                : `▸ Show all ${entries.length}`}
            </button>`
          : nothing}
      </div>
    `;
  }

  private renderRationale(): TemplateResult | typeof nothing {
    const text = this.rationale?.trim();
    if (!text) return nothing;
    const long = text.length > RATIONALE_TRUNCATE;
    const body =
      long && !this.rationaleExpanded
        ? `${text.slice(0, RATIONALE_TRUNCATE)}…`
        : text;
    return html`
      <div
        class="text-[12.5px] text-ink-2 dark:text-d-ink-2 leading-relaxed border-l-[3px] border-accent dark:border-d-accent bg-accent/5 dark:bg-d-accent/10 px-3 py-2 rounded-r-lg mb-2.5"
        data-testid="approval-rationale"
      >
        <span
          class="text-[10px] tracking-wide uppercase font-semibold text-accent dark:text-d-accent mr-1.5 align-top"
          >Why</span
        >${body}${long
          ? html`<button
              type="button"
              class="ml-1 text-[11px] text-accent dark:text-d-accent cursor-pointer underline"
              data-testid="approval-rationale-toggle"
              @click=${() => (this.rationaleExpanded = !this.rationaleExpanded)}
            >
              ${this.rationaleExpanded ? 'less' : 'more'}
            </button>`
          : nothing}
      </div>
    `;
  }

  private renderResolvedNote(): TemplateResult | typeof nothing {
    if (this.status !== 'rejected') return nothing;
    const note = this.note ?? this.submittedNote;
    if (!note) return nothing;
    return html`
      <div
        class="text-[12.5px] text-ink-2 dark:text-d-ink-2 mt-2.5 px-3 py-2 bg-surface-2 dark:bg-d-surface-2 rounded-md leading-relaxed"
        data-testid="approval-resolved-note"
      >
        <span
          class="block text-[10px] tracking-wide uppercase font-medium text-ink-3 dark:text-d-ink-3 mb-1"
          >Your reason</span
        >${note}
      </div>
    `;
  }

  private renderActions(): TemplateResult | typeof nothing {
    if (this.status !== 'pending') return nothing;
    if (this.rejecting) {
      return html`
        <div
          class="mt-3 border-t border-border dark:border-d-border pt-3"
          data-testid="approval-reject-form"
        >
          <label
            class="block text-[11.5px] text-ink-3 dark:text-d-ink-3 mb-1.5 leading-relaxed"
          >
            Tell the coworker why (optional). This text becomes the tool-call
            rejection reason — they'll read it and adjust.
          </label>
          <textarea
            data-testid="approval-note"
            class="w-full min-h-[56px] px-2.5 py-2 border border-border-2 dark:border-d-border-2 rounded-md text-[12.5px] resize-y bg-surface dark:bg-d-surface focus:outline-none focus:border-accent"
            placeholder="e.g. amount is too high — needs senior signoff above $5k"
            ?disabled=${this.busy}
          ></textarea>
          <div class="flex items-center justify-end gap-2 mt-2">
            <button
              type="button"
              data-testid="approval-reject-cancel"
              class="text-[13px] font-medium px-3.5 py-1.5 rounded-lg border border-border-2 dark:border-d-border-2 text-ink-2 dark:text-d-ink-2 bg-surface dark:bg-d-surface hover:bg-surface-2 dark:hover:bg-d-surface-2 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
              ?disabled=${this.busy}
              @click=${() => this.cancelReject()}
            >
              Cancel
            </button>
            <button
              type="button"
              data-testid="approval-reject-confirm"
              class="text-[13px] font-medium px-3.5 py-1.5 rounded-lg bg-[var(--rm-bad)] text-white hover:brightness-90 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
              ?disabled=${this.busy}
              @click=${() => this.confirmReject()}
            >
              Reject
            </button>
          </div>
        </div>
      `;
    }
    return html`
      <div class="flex items-center justify-end gap-2 mt-3">
        <button
          type="button"
          data-testid="approval-reject"
          class="text-[13px] font-medium px-3.5 py-1.5 rounded-lg border border-border-2 dark:border-d-border-2 text-ink-2 dark:text-d-ink-2 bg-surface dark:bg-d-surface hover:bg-surface-2 dark:hover:bg-d-surface-2 transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          ?disabled=${this.busy}
          @click=${() => this.openRejectForm()}
        >
          Reject
        </button>
        <button
          type="button"
          data-testid="approval-approve"
          class="text-[13px] font-medium px-3.5 py-1.5 rounded-lg bg-accent text-[var(--rm-accent-ink)] hover:bg-[var(--rm-accent-2)] transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          ?disabled=${this.busy}
          @click=${() => this.approve()}
        >
          Approve
        </button>
      </div>
    `;
  }

  override render(): TemplateResult {
    const resolved = this.status !== 'pending';
    return html`
      <div
        class="my-2 max-w-[560px] rounded-xl border border-border-2 dark:border-d-border-2 bg-surface dark:bg-d-surface overflow-hidden ${resolved
          ? 'opacity-95'
          : ''}"
        data-testid="approval-card"
        data-appr-id=${this.requestId}
      >
        ${this.renderHeader()}
        <div class="px-3.5 py-3">
          ${this.renderMeta()}${this.renderSafetyBanner()}${this.renderToolChip()}
          <div class=${resolved ? 'opacity-75' : ''}>
            ${this.renderParams()}
          </div>
          <div class=${resolved ? 'opacity-55' : ''}>
            ${this.renderRationale()}
          </div>
          ${this.renderResolvedNote()}${this.renderActions()}
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
