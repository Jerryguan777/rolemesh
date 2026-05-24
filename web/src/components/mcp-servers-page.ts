// MCP servers page (#/mcp-servers).
//
// List + minimal create form. Detail edit + tool_reversibility
// editor stay out for now — design §6.3 D names the affordances
// but the form here is intentionally narrow to keep the diff
// reviewable. ``auth_mode=user`` shows a "requires user session"
// hint per design ("e2e 验收 pending — OIDC 分支合入后启用").

import { LitElement, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import { ApiError, getApiClient } from '../api/client.js';
import type { MCPServer } from '../api/client.js';
import './mcp-server-dialog.js';
import { iconPencil, iconTrash } from './icons.js';

@customElement('rm-mcp-servers-page')
export class MCPServersPage extends LitElement {
  @state() private rows: MCPServer[] = [];
  @state() private loading = true;
  @state() private listError: string | null = null;
  @state() private deleteError: Record<string, string> = {};
  /** Single dialog backs BOTH create AND edit. `editTarget` null =
   *  create flow; non-null = edit flow (rm-mcp-server-dialog branches
   *  on its `editing` prop). v2-C dropped the inline create form to
   *  collapse the two surfaces into one. */
  @state() private dialogOpen = false;
  @state() private editTarget: MCPServer | null = null;
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
      this.rows = await this.api.listMCPServers();
    } catch (err) {
      this.rows = [];
      this.listError = this.errMessage(err);
    } finally {
      this.loading = false;
    }
  }

  private errMessage(err: unknown): string {
    if (err instanceof ApiError) {
      if (err.status === 409 && err.body?.details) {
        const ids = (err.body.details as Record<string, unknown>).coworker_ids;
        if (Array.isArray(ids)) {
          return `MCP server is in use by ${ids.length} coworker(s); unbind them before deleting.`;
        }
      }
      return err.body?.message ?? `${err.status}`;
    }
    return (err as Error).message;
  }

  // Renamed from `remove` (v1.1) — `HTMLElement.prototype.remove`
  // exists as a no-arg "detach this element from the DOM" method, and
  // Lit's NodePart._$clear calls it during teardown. Overriding it
  // with a 1-arg method (taking a row) made every Lit-driven unmount
  // throw "Cannot read properties of undefined (reading 'id')" mid-
  // clear, leaving the old <rm-mcp-servers-page> stranded in the DOM
  // whenever the settings shell switched tabs.
  private async removeServer(row: MCPServer): Promise<void> {
    const ok = window.confirm(
      `Delete MCP server "${row.name}"?\n\n` +
        'Coworkers bound to this server will lose access to its tools. ' +
        'Cannot be undone.',
    );
    if (!ok) return;
    this.deleteError = { ...this.deleteError, [row.id]: '' };
    try {
      await this.api.deleteMCPServer(row.id);
      await this.refresh();
    } catch (err) {
      this.deleteError = {
        ...this.deleteError,
        [row.id]: this.errMessage(err),
      };
    }
  }

  private openEdit(row: MCPServer): void {
    this.editTarget = row;
    this.dialogOpen = true;
  }

  private openCreate(): void {
    this.editTarget = null;
    this.dialogOpen = true;
  }

  override render() {
    return html`
      <div class="rm-spane">
        <div class="rm-ch">
          <h2>MCP servers</h2>
          <button
            type="button"
            class="rm-add"
            @click=${this.openCreate}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M12 5v14M5 12h14"/>
            </svg>
            New MCP server
          </button>
        </div>
        <p class="rm-sub">
          Tenant-scoped registry. Changes hot-reload to the egress gateway.
        </p>

        ${this.loading
          ? html`<div class="rm-banner-loading">Loading…</div>`
          : this.listError
            ? html`<div class="rm-banner-err">${this.listError}</div>`
            : this.rows.length === 0
              ? this.renderEmpty()
              : this.renderList()}

        <rm-mcp-server-dialog
          ?open=${this.dialogOpen}
          .editing=${this.editTarget}
          @close=${() => {
            this.dialogOpen = false;
            this.editTarget = null;
          }}
          @mcp-server-created=${() => { void this.refresh(); }}
          @mcp-server-updated=${() => { void this.refresh(); }}
        ></rm-mcp-server-dialog>
      </div>
    `;
  }

  private renderEmpty() {
    return html`
      <div class="rm-empty">
        <span class="rm-empty-title">No MCP servers yet</span>
        Click <b>+ New MCP server</b> above to register one.
      </div>
    `;
  }

  private renderList() {
    return html`
      ${this.rows.map((r) => this.renderRow(r))}
    `;
  }

  /** Pick a pill modifier based on auth_mode. `service` is the
   *  no-fuss state; `user` / `both` requires a per-user OIDC token
   *  so we warn-tint to surface the dependency. */
  private authPillClass(authMode: MCPServer['auth_mode']): string {
    if (authMode === 'service') return 'rm-pill rm-pill-on';
    return 'rm-pill rm-pill-warn';
  }

  private renderRow(r: MCPServer) {
    const delErr = this.deleteError[r.id] || '';
    return html`
      <div class="rm-card" data-mcp-id=${r.id}>
        <span class="rm-ic">${(r.name?.[0] ?? '?').toUpperCase()}</span>
        <span class="rm-mn">
          <b>${r.name}</b>
          <span>${r.type} · ${r.url}</span>
        </span>
        <span class=${this.authPillClass(r.auth_mode)}>${r.auth_mode}</span>
        <span class="rm-row-acts">
          <button
            type="button"
            class="rm-iconbtn"
            title="Edit MCP server"
            data-testid="mcp-edit"
            @click=${() => this.openEdit(r)}
          >${iconPencil(15)}</button>
          <button
            type="button"
            class="rm-iconbtn rm-iconbtn--danger"
            title="Delete MCP server"
            data-testid="mcp-delete"
            @click=${() => void this.removeServer(r)}
          >${iconTrash(15)}</button>
        </span>
        ${delErr
          ? html`<div class="rm-row-error">${delErr}</div>`
          : nothing}
      </div>
    `;
  }
}
