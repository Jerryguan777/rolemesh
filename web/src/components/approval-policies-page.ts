// Approval policies page (#/manage/approval-policies).
//
// HITL tool-approval policy CRUD (spec §5). The list ranks rules in the same
// order the server evaluates them (priority desc, then created_at desc), shows
// a priority badge + always-visible enable/disable switch per row, and reveals
// Edit / Duplicate / Delete on hover. Create/edit/duplicate share one dialog;
// delete goes through a confirmation modal that restates the rule.

import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';

import { ApiError, getApiClient } from '../api/client.js';
import type { ApprovalPolicy } from '../api/client.js';
import './approval-policy-dialog.js';
import './confirm-dialog.js';
import { conditionSentence } from './condition-form.js';
import { iconCopy, iconPencil, iconTrash } from './icons.js';

/** Badge tint class for a priority value (Appendix C.3): amber when ≥10,
 *  muted when exactly 0, neutral otherwise. */
export function priorityBadgeClass(priority: number): string {
  if (priority >= 10) return 'rm-pol-pri--hi';
  if (priority === 0) return 'rm-pol-pri--zero';
  return '';
}

/** List order = server evaluation order (spec §5.5): priority desc, then the
 *  newest rule first on ties. created_at is an ISO-8601 string from the API. */
export function sortPolicies(rows: ApprovalPolicy[]): ApprovalPolicy[] {
  return [...rows].sort(
    (a, b) =>
      b.priority - a.priority ||
      Date.parse(b.created_at) - Date.parse(a.created_at),
  );
}

@customElement('rm-approval-policies-page')
export class ApprovalPoliciesPage extends LitElement {
  @state() private rows: ApprovalPolicy[] = [];
  @state() private loading = true;
  @state() private listError: string | null = null;
  @state() private dialogOpen = false;
  @state() private editTarget: ApprovalPolicy | null = null;
  @state() private duplicateSource: ApprovalPolicy | null = null;
  @state() private deleteTarget: ApprovalPolicy | null = null;
  @state() private deleteInFlight = false;
  /** Ids currently mid-PATCH on their toggle — disables the control so a
   *  double-click can't queue two conflicting writes. */
  @state() private togglingIds: Set<string> = new Set();
  @state() private toast: string | null = null;
  /** Id to pulse after a create/duplicate save (spec §5.7). */
  @state() private highlightId: string | null = null;

  private readonly api = getApiClient();
  private toastTimer: number | null = null;
  private highlightTimer: number | null = null;

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback() {
    super.connectedCallback();
    void this.refresh();
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    if (this.toastTimer) clearTimeout(this.toastTimer);
    if (this.highlightTimer) clearTimeout(this.highlightTimer);
  }

  private async refresh(): Promise<void> {
    this.loading = true;
    this.listError = null;
    try {
      this.rows = await this.api.listApprovalPolicies();
    } catch (err) {
      this.rows = [];
      this.listError = this.errMessage(err);
    } finally {
      this.loading = false;
    }
  }

  private errMessage(err: unknown): string {
    if (err instanceof ApiError) return err.body?.message ?? `HTTP ${err.status}`;
    return (err as Error).message;
  }

  private showToast(msg: string): void {
    this.toast = msg;
    if (this.toastTimer) clearTimeout(this.toastTimer);
    this.toastTimer = window.setTimeout(() => {
      this.toast = null;
    }, 3200);
  }

  private openCreate = (): void => {
    this.editTarget = null;
    this.duplicateSource = null;
    this.dialogOpen = true;
  };

  private openEdit(row: ApprovalPolicy): void {
    this.editTarget = row;
    this.duplicateSource = null;
    this.dialogOpen = true;
  }

  private openDuplicate(row: ApprovalPolicy): void {
    this.editTarget = null;
    this.duplicateSource = row;
    this.dialogOpen = true;
  }

  private closeDialog = (): void => {
    this.dialogOpen = false;
    this.editTarget = null;
    this.duplicateSource = null;
  };

  /** Optimistic enable/disable (spec §5.3): flip immediately, PATCH in the
   *  background, revert + toast on failure. No confirm — fully reversible. */
  private async toggleEnabled(row: ApprovalPolicy): Promise<void> {
    if (this.togglingIds.has(row.id)) return;
    const next = !row.enabled;
    this.rows = this.rows.map((r) =>
      r.id === row.id ? { ...r, enabled: next } : r,
    );
    this.togglingIds = new Set(this.togglingIds).add(row.id);
    try {
      await this.api.updateApprovalPolicy(row.id, { enabled: next });
    } catch {
      // Revert to the value we flipped away from.
      this.rows = this.rows.map((r) =>
        r.id === row.id ? { ...r, enabled: row.enabled } : r,
      );
      this.showToast('Couldn’t update — try again');
    } finally {
      const ids = new Set(this.togglingIds);
      ids.delete(row.id);
      this.togglingIds = ids;
    }
  }

  private askDelete(row: ApprovalPolicy): void {
    this.deleteTarget = row;
  }

  private cancelDelete = (): void => {
    if (this.deleteInFlight) return;
    this.deleteTarget = null;
  };

  private async performDelete(): Promise<void> {
    const row = this.deleteTarget;
    if (!row || this.deleteInFlight) return;
    this.deleteInFlight = true;
    try {
      await this.api.deleteApprovalPolicy(row.id);
      this.rows = this.rows.filter((r) => r.id !== row.id);
      this.deleteTarget = null;
    } catch (err) {
      this.deleteTarget = null;
      this.showToast(this.errMessage(err));
    } finally {
      this.deleteInFlight = false;
    }
  }

  /** Splice the saved policy into the local list (no full re-fetch) so we can
   *  pulse the exact card. Create/duplicate append; edit replaces in place. */
  private onSaved(policy: ApprovalPolicy): void {
    const existing = this.rows.some((r) => r.id === policy.id);
    this.rows = existing
      ? this.rows.map((r) => (r.id === policy.id ? policy : r))
      : [...this.rows, policy];
    this.pulse(policy.id);
  }

  private pulse(id: string): void {
    this.highlightId = id;
    if (this.highlightTimer) clearTimeout(this.highlightTimer);
    void this.updateComplete.then(() => {
      const card = this.querySelector(`[data-policy-id="${id}"]`);
      card?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
    this.highlightTimer = window.setTimeout(() => {
      this.highlightId = null;
    }, 1800);
  }

  override render() {
    const sorted = sortPolicies(this.rows);
    return html`
      <div class="rm-spane">
        <div class="rm-ch">
          <h2>Approval policies</h2>
          <button type="button" class="rm-add" @click=${this.openCreate}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M12 5v14M5 12h14"/>
            </svg>
            New policy
          </button>
        </div>
        <p class="rm-sub">
          Which actions your coworkers should pause and confirm with you before
          running. Confirmations appear in the chat; for scheduled tasks they go
          to whoever set up the task.
        </p>

        ${this.loading
          ? html`<div class="rm-banner-loading">Loading…</div>`
          : this.listError
            ? html`<div class="rm-banner-err">${this.listError}</div>`
            : sorted.length === 0
              ? this.renderEmpty()
              : html`
                  ${sorted.map((r) => this.renderRow(r))}
                  ${this.renderHint()}
                `}

        <rm-approval-policy-dialog
          ?open=${this.dialogOpen}
          .editing=${this.editTarget}
          .duplicating=${this.duplicateSource}
          @close=${this.closeDialog}
          @approval-policy-saved=${(e: CustomEvent<{ policy: ApprovalPolicy }>) => {
            this.onSaved(e.detail.policy);
          }}
        ></rm-approval-policy-dialog>
        ${this.renderDeleteDialog()}
        ${this.toast
          ? html`<div class="rm-toast" role="status" data-testid="policy-toast">
              ${this.toast}
            </div>`
          : nothing}
      </div>
    `;
  }

  private renderHint(): TemplateResult {
    // Timeout copy is 5 minutes (APPROVAL_TIMEOUT = 300_000ms); the spec's
    // "20-minute" text is stale.
    return html`
      <p class="rm-pol-hint" data-testid="policy-hint">
        Anything not matching above runs without asking. When multiple rules
        match the same call, the highest priority wins; ties go to the newest.
        Approvals time out after 5 minutes and auto-reject — the coworker can
        re-request next turn.
      </p>
    `;
  }

  private renderEmpty(): TemplateResult {
    return html`
      <div class="rm-pol-empty" data-testid="policy-empty">
        <div class="rm-pol-empty-icon">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" stroke-width="1.6" aria-hidden="true">
            <path d="M9 12l2 2 4-4"/>
            <path d="M21 12c0 4.97-4.03 9-9 9s-9-4.03-9-9 4.03-9 9-9 9 4.03 9 9z"/>
          </svg>
        </div>
        <p>No approval policies yet.</p>
        <p class="rm-pol-empty-sub">
          Every tool call runs without asking. Create your first policy to gate
          consequential actions.
        </p>
        <button type="button" class="rm-add" @click=${this.openCreate}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" stroke-width="2.2" aria-hidden="true">
            <path d="M12 5v14M5 12h14"/>
          </svg>
          Create your first policy
        </button>
      </div>
    `;
  }

  private renderRow(r: ApprovalPolicy): TemplateResult {
    const toolDisp =
      r.tool_name === '*'
        ? html`* <span class="rm-pol-alltools">(all tools)</span>`
        : r.tool_name;
    const dim = r.enabled ? '' : ' rm-card--dim';
    const hi = this.highlightId === r.id ? ' rm-card--highlight' : '';
    const toggling = this.togglingIds.has(r.id);
    return html`
      <div class="rm-card${dim}${hi}" data-policy-id=${r.id} data-testid="policy-row">
        <span
          class="rm-pol-pri ${priorityBadgeClass(r.priority)}"
          data-testid="policy-priority"
        >priority ${r.priority}</span>
        <span class="rm-mn">
          <b>${r.mcp_server_name} · ${toolDisp}</b>
          <span class="rm-pol-sent" data-testid="policy-sentence"
            >${unsafeHTML(conditionSentence(r.condition_expr))} → pause to
            confirm</span
          >
        </span>
        <button
          type="button"
          class="rm-pol-toggle ${r.enabled ? 'rm-pol-toggle--on' : ''}"
          title=${r.enabled ? 'Click to disable' : 'Click to enable'}
          data-testid="policy-toggle"
          ?disabled=${toggling}
          @click=${(e: Event) => {
            e.stopPropagation();
            void this.toggleEnabled(r);
          }}
        >
          <span>${r.enabled ? 'Enabled' : 'Disabled'}</span>
          <span class="rm-switch"></span>
        </button>
        <span class="rm-row-acts">
          <button
            type="button"
            class="rm-iconbtn"
            title="Edit policy"
            data-testid="policy-edit"
            @click=${(e: Event) => {
              e.stopPropagation();
              this.openEdit(r);
            }}
          >${iconPencil(15)}</button>
          <button
            type="button"
            class="rm-iconbtn"
            title="Duplicate policy"
            data-testid="policy-duplicate"
            @click=${(e: Event) => {
              e.stopPropagation();
              this.openDuplicate(r);
            }}
          >${iconCopy(15)}</button>
          <button
            type="button"
            class="rm-iconbtn rm-iconbtn--danger"
            title="Delete policy"
            data-testid="policy-delete"
            @click=${(e: Event) => {
              e.stopPropagation();
              this.askDelete(r);
            }}
          >${iconTrash(15)}</button>
        </span>
      </div>
    `;
  }

  private renderDeleteDialog(): TemplateResult {
    const target = this.deleteTarget;
    const toolDisp = target?.tool_name === '*' ? 'any tool' : target?.tool_name;
    return html`
      <rm-confirm-dialog
        title="Delete approval policy?"
        ?open=${target !== null}
        tone="danger"
        confirm-label="Delete policy"
        busy-label="Deleting…"
        ?busy=${this.deleteInFlight}
        data-testid="confirm-delete-dialog"
        @cancel=${this.cancelDelete}
        @confirm=${() => void this.performDelete()}
      >
        ${target
          ? html`<p style="margin: 0 0 10px;" data-testid="delete-desc">
                You’re about to delete the policy that pauses for
                <code>${target.mcp_server_name} · ${toolDisp}</code>
                ${unsafeHTML(conditionSentence(target.condition_expr))}.
              </p>
              <p style="margin: 0; font-size: 12.5px; color: var(--rm-ink-3); line-height: 1.55;">
                After deletion, matching calls will run without asking. Pending
                approvals already raised under this policy stay live until
                decided or expired; only future calls change.
              </p>`
          : nothing}
      </rm-confirm-dialog>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'rm-approval-policies-page': ApprovalPoliciesPage;
  }
}
