// Approval policies page (#/manage/approval-policies).
//
// HITL tool-approval policy CRUD (docs/21-hitl-approval-plan.md §10 S5). List +
// create/edit dialog (with the §7 condition builder) + delete confirm. Mirrors
// the <rm-mcp-servers-page> structure so the governance pages read alike.

import { LitElement, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import { ApiError, getApiClient } from '../api/client.js';
import type { ApprovalPolicy } from '../api/client.js';
import './approval-policy-dialog.js';
import './confirm-dialog.js';
import { iconPencil, iconTrash } from './icons.js';

/** One-line human summary of a condition_expr for the list row. */
export function summarizeCondition(expr: unknown): string {
  if (typeof expr !== 'object' || expr === null) return 'custom';
  const obj = expr as Record<string, unknown>;
  if ('always' in obj) return obj.always === true ? 'always' : 'never';
  if ('field' in obj && 'op' in obj) {
    return `${String(obj.field)} ${String(obj.op)} ${JSON.stringify(obj.value)}`;
  }
  for (const c of ['and', 'or'] as const) {
    if (c in obj && Array.isArray(obj[c])) {
      const subs = obj[c] as unknown[];
      return subs.map(summarizeCondition).join(` ${c} `);
    }
  }
  return 'custom';
}

@customElement('rm-approval-policies-page')
export class ApprovalPoliciesPage extends LitElement {
  @state() private rows: ApprovalPolicy[] = [];
  @state() private loading = true;
  @state() private listError: string | null = null;
  @state() private deleteError: Record<string, string> = {};
  @state() private dialogOpen = false;
  @state() private editTarget: ApprovalPolicy | null = null;
  @state() private deleteTarget: ApprovalPolicy | null = null;
  @state() private deleteInFlight = false;
  private readonly api = getApiClient();

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback() {
    super.connectedCallback();
    void this.refresh();
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

  private openCreate = (): void => {
    this.editTarget = null;
    this.dialogOpen = true;
  };

  private openEdit(row: ApprovalPolicy): void {
    this.editTarget = row;
    this.dialogOpen = true;
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
    this.deleteError = { ...this.deleteError, [row.id]: '' };
    try {
      await this.api.deleteApprovalPolicy(row.id);
      this.deleteTarget = null;
      await this.refresh();
    } catch (err) {
      this.deleteError = { ...this.deleteError, [row.id]: this.errMessage(err) };
      this.deleteTarget = null;
    } finally {
      this.deleteInFlight = false;
    }
  }

  override render() {
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
          Tenant-scoped gates: a matching MCP tool call blocks and waits for a
          human ✅/❌ before it runs.
        </p>

        ${this.loading
          ? html`<div class="rm-banner-loading">Loading…</div>`
          : this.listError
            ? html`<div class="rm-banner-err">${this.listError}</div>`
            : this.rows.length === 0
              ? this.renderEmpty()
              : this.rows.map((r) => this.renderRow(r))}

        <rm-approval-policy-dialog
          ?open=${this.dialogOpen}
          .editing=${this.editTarget}
          @close=${() => {
            this.dialogOpen = false;
            this.editTarget = null;
          }}
          @approval-policy-created=${() => { void this.refresh(); }}
          @approval-policy-updated=${() => { void this.refresh(); }}
        ></rm-approval-policy-dialog>
        ${this.renderDeleteDialog()}
      </div>
    `;
  }

  private renderEmpty() {
    return html`
      <div class="rm-empty">
        <span class="rm-empty-title">No approval policies yet</span>
        Click <b>+ New policy</b> above to gate an MCP tool behind a human ✅/❌.
      </div>
    `;
  }

  private renderRow(r: ApprovalPolicy) {
    const delErr = this.deleteError[r.id] || '';
    const tool = r.tool_name === '*' ? 'every tool' : r.tool_name;
    return html`
      <div class="rm-card" data-policy-id=${r.id} data-testid="policy-row">
        <span class="rm-ic">${(r.mcp_server_name?.[0] ?? '?').toUpperCase()}</span>
        <span class="rm-mn">
          <b>${r.mcp_server_name} · ${tool}</b>
          <span>when ${summarizeCondition(r.condition_expr)} · priority ${r.priority}</span>
        </span>
        <span class="rm-pill ${r.enabled ? 'rm-pill-on' : 'rm-pill-off'}">
          ${r.enabled ? 'enabled' : 'disabled'}
        </span>
        <span class="rm-row-acts">
          <button
            type="button"
            class="rm-iconbtn"
            title="Edit policy"
            data-testid="policy-edit"
            @click=${() => this.openEdit(r)}
          >${iconPencil(15)}</button>
          <button
            type="button"
            class="rm-iconbtn rm-iconbtn--danger"
            title="Delete policy"
            data-testid="policy-delete"
            @click=${() => this.askDelete(r)}
          >${iconTrash(15)}</button>
        </span>
        ${delErr ? html`<div class="rm-row-error">${delErr}</div>` : nothing}
      </div>
    `;
  }

  private renderDeleteDialog() {
    const target = this.deleteTarget;
    return html`
      <rm-confirm-dialog
        title=${target
          ? `Delete policy for "${target.mcp_server_name}"?`
          : 'Delete policy?'}
        ?open=${target !== null}
        tone="danger"
        confirm-label="Delete"
        busy-label="Deleting…"
        ?busy=${this.deleteInFlight}
        data-testid="confirm-delete-dialog"
        @cancel=${this.cancelDelete}
        @confirm=${() => void this.performDelete()}
      >
        <p style="margin: 0;">
          The gate stops applying immediately. In-flight requests are
          unaffected. This cannot be undone.
        </p>
      </rm-confirm-dialog>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'rm-approval-policies-page': ApprovalPoliciesPage;
  }
}
