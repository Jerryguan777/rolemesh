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
  /** Per-MCP-server bound-coworker count. Backend doesn't surface
   *  this on the MCPServer model (unlike Skill.bound_coworker_count),
   *  so we compute it here by walking every coworker's bindings.
   *  Map miss = "still loading" or "no bindings" — both render as 0. */
  @state() private coworkerCounts: Map<string, number> = new Map();
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
    // Kick off the coworker-count tally in the background — the list
    // paints right away, counts stream in. We don't await: a delayed
    // count is preferable to a blocked render.
    void this.recountBindings();
  }

  /** Tally `coworker_count` per MCP server by walking every coworker's
   *  bindings. Backend doesn't surface this on the MCPServer model
   *  (Skill.bound_coworker_count is the analog there); doing it
   *  client-side adds N small GETs (one per coworker, ~10-20 in a
   *  typical tenant). All failures are swallowed — the row's "0
   *  coworker(s)" hint is benign if the count never arrives. */
  private async recountBindings(): Promise<void> {
    let coworkers: { id: string }[] = [];
    try {
      coworkers = await this.api.listCoworkers();
    } catch {
      return;
    }
    const next = new Map<string, number>();
    const results = await Promise.allSettled(
      coworkers.map((c) => this.api.listCoworkerMCPServers(c.id)),
    );
    for (const r of results) {
      if (r.status !== 'fulfilled') continue;
      for (const binding of r.value) {
        const id = binding.mcp_server_id;
        next.set(id, (next.get(id) ?? 0) + 1);
      }
    }
    this.coworkerCounts = next;
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

  private renderRow(r: MCPServer) {
    const delErr = this.deleteError[r.id] || '';
    const count = this.coworkerCounts.get(r.id) ?? 0;
    return html`
      <div class="rm-card" data-mcp-id=${r.id}>
        <span class="rm-ic">${(r.name?.[0] ?? '?').toUpperCase()}</span>
        <span class="rm-mn">
          <b>${r.name}</b>
          <span>${r.type} · auth: ${r.auth_mode} · ${r.url}</span>
        </span>
        <span class="rm-meta">${count} coworker${count === 1 ? '' : 's'}</span>
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
