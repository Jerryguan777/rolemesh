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
import type { MCPServer, MCPServerCreate } from '../api/client.js';
import './mcp-server-dialog.js';
import { iconPencil, iconTrash } from './icons.js';

type MCPType = MCPServerCreate['type'];
type AuthMode = MCPServerCreate['auth_mode'];

@customElement('rm-mcp-servers-page')
export class MCPServersPage extends LitElement {
  @state() private rows: MCPServer[] = [];
  @state() private loading = true;
  @state() private listError: string | null = null;
  @state() private formOpen = false;
  @state() private form: MCPServerCreate = this.emptyForm();
  @state() private formError: string | null = null;
  @state() private busy = false;
  @state() private deleteError: Record<string, string> = {};
  @state() private editDialogOpen = false;
  @state() private editTarget: MCPServer | null = null;
  private readonly api = getApiClient();

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback() {
    super.connectedCallback();
    void this.refresh();
  }

  private emptyForm(): MCPServerCreate {
    return {
      name: '',
      type: 'http',
      url: '',
      auth_mode: 'service',
      description: null,
    };
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

  private async submit(): Promise<void> {
    if (!this.form.name || !this.form.url) {
      this.formError = 'Name and URL are required.';
      return;
    }
    this.busy = true;
    this.formError = null;
    try {
      await this.api.createMCPServer({ ...this.form });
      this.formOpen = false;
      this.form = this.emptyForm();
      await this.refresh();
    } catch (err) {
      this.formError = this.errMessage(err);
    } finally {
      this.busy = false;
    }
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
    this.editDialogOpen = true;
  }

  override render() {
    return html`
      <div class="h-full w-full overflow-y-auto px-6 py-6">
        <div class="max-w-3xl mx-auto">
          <div class="flex items-baseline justify-between mb-4">
            <div>
              <h1 class="text-[20px] font-semibold text-ink-0 dark:text-d-ink-0">
                MCP servers
              </h1>
              <p class="text-[13px] text-ink-3 dark:text-d-ink-3 mt-0.5">
                Tenant-scoped registry. Changes hot-reload to the egress gateway.
              </p>
            </div>
            <button
              type="button"
              class="text-[12px] px-3 py-1.5 rounded-md bg-brand text-white
                hover:bg-brand-dark transition-colors cursor-pointer"
              @click=${() => {
                this.formOpen = !this.formOpen;
                this.formError = null;
              }}
            >${this.formOpen ? 'Cancel' : '+ New MCP server'}</button>
          </div>

          ${this.formOpen ? this.renderForm() : nothing}

          ${this.loading
            ? html`<div class="text-[13px] text-ink-3 dark:text-d-ink-3">Loading…</div>`
            : this.listError
              ? html`<div
                  class="border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20
                    text-red-700 dark:text-red-300 text-[13px] px-3 py-2 rounded-lg"
                >${this.listError}</div>`
              : this.rows.length === 0
                ? this.renderEmpty()
                : this.renderList()}
        </div>
        <rm-mcp-server-dialog
          ?open=${this.editDialogOpen}
          .editing=${this.editTarget}
          @close=${() => {
            this.editDialogOpen = false;
            this.editTarget = null;
          }}
          @mcp-server-updated=${() => { void this.refresh(); }}
        ></rm-mcp-server-dialog>
      </div>
    `;
  }

  private renderForm() {
    const f = this.form;
    return html`
      <section
        class="border border-surface-3 dark:border-d-surface-3 rounded-xl px-4 py-3 mb-4 space-y-3"
      >
        <div class="grid grid-cols-2 gap-3">
          <label class="text-[12px] text-ink-2 dark:text-d-ink-2">
            <span class="block mb-1">Name</span>
            <input
              type="text"
              class="w-full text-[13px] px-3 py-1.5 rounded-md border border-surface-3 dark:border-d-surface-3
                bg-surface-1 dark:bg-d-surface-1"
              .value=${f.name}
              @input=${(e: Event) =>
                (this.form = {
                  ...this.form,
                  name: (e.target as HTMLInputElement).value,
                })}
            />
          </label>
          <label class="text-[12px] text-ink-2 dark:text-d-ink-2">
            <span class="block mb-1">URL</span>
            <input
              type="url"
              class="w-full text-[13px] px-3 py-1.5 rounded-md border border-surface-3 dark:border-d-surface-3
                bg-surface-1 dark:bg-d-surface-1"
              placeholder="https://mcp.example.com"
              .value=${f.url}
              @input=${(e: Event) =>
                (this.form = {
                  ...this.form,
                  url: (e.target as HTMLInputElement).value,
                })}
            />
          </label>
          <label class="text-[12px] text-ink-2 dark:text-d-ink-2">
            <span class="block mb-1">Type</span>
            <select
              class="w-full text-[13px] px-3 py-1.5 rounded-md border border-surface-3 dark:border-d-surface-3
                bg-surface-1 dark:bg-d-surface-1"
              .value=${f.type}
              @change=${(e: Event) =>
                (this.form = {
                  ...this.form,
                  type: (e.target as HTMLSelectElement).value as MCPType,
                })}
            >
              <option value="http">http</option>
              <option value="sse">sse</option>
            </select>
          </label>
          <label class="text-[12px] text-ink-2 dark:text-d-ink-2">
            <span class="block mb-1">Auth mode</span>
            <select
              class="w-full text-[13px] px-3 py-1.5 rounded-md border border-surface-3 dark:border-d-surface-3
                bg-surface-1 dark:bg-d-surface-1"
              .value=${f.auth_mode}
              @change=${(e: Event) =>
                (this.form = {
                  ...this.form,
                  auth_mode: (e.target as HTMLSelectElement).value as AuthMode,
                })}
            >
              <option value="service">service</option>
              <option value="user">user</option>
              <option value="both">both</option>
            </select>
          </label>
        </div>
        ${f.auth_mode === 'user' || f.auth_mode === 'both'
          ? html`<div
              class="text-[12px] text-amber-700 dark:text-amber-300
                border border-amber-200 dark:border-amber-800
                bg-amber-50 dark:bg-amber-900/20 rounded-md px-3 py-2"
            >
              <strong>Requires user session</strong> — end-to-end
              verification pending until the OIDC branch lands.
            </div>`
          : null}
        <label class="block text-[12px] text-ink-2 dark:text-d-ink-2">
          <span class="block mb-1">Description (optional)</span>
          <textarea
            rows="2"
            class="w-full text-[13px] px-3 py-1.5 rounded-md border border-surface-3 dark:border-d-surface-3
              bg-surface-1 dark:bg-d-surface-1"
            .value=${f.description ?? ''}
            @input=${(e: Event) =>
              (this.form = {
                ...this.form,
                description: (e.target as HTMLTextAreaElement).value || null,
              })}
          ></textarea>
        </label>
        ${this.formError
          ? html`<div class="text-[12px] text-red-600 dark:text-red-300">${this.formError}</div>`
          : null}
        <div class="flex items-center justify-end gap-2">
          <button
            type="button"
            class="text-[12px] px-3 py-1.5 rounded-md border border-surface-3 dark:border-d-surface-3
              text-ink-2 dark:text-d-ink-2 cursor-pointer"
            @click=${() => {
              this.formOpen = false;
              this.form = this.emptyForm();
              this.formError = null;
            }}
            ?disabled=${this.busy}
          >Cancel</button>
          <button
            type="button"
            class="text-[12px] px-3 py-1.5 rounded-md bg-brand text-white hover:bg-brand-dark cursor-pointer
              disabled:opacity-60 disabled:cursor-not-allowed"
            ?disabled=${this.busy}
            @click=${() => void this.submit()}
          >Create</button>
        </div>
      </section>
    `;
  }

  private renderEmpty() {
    return html`
      <div
        class="border border-dashed border-surface-3 dark:border-d-surface-3
          rounded-xl px-6 py-10 text-center text-[13px] text-ink-2 dark:text-d-ink-2"
      >
        <p class="mb-1.5 font-medium text-ink-1 dark:text-d-ink-1">
          No MCP servers yet
        </p>
        <p class="leading-relaxed">
          Click <strong>+ New MCP server</strong> to register one.
        </p>
      </div>
    `;
  }

  private renderList() {
    return html`
      <style>
        rm-mcp-servers-page .row-acts {
          opacity: 0;
          transition: opacity 0.13s;
        }
        rm-mcp-servers-page .mcp-row:hover .row-acts,
        rm-mcp-servers-page .mcp-row:focus-within .row-acts {
          opacity: 1;
        }
        rm-mcp-servers-page .icon-btn {
          width: 28px;
          height: 28px;
          border-radius: 7px;
          display: grid;
          place-items: center;
          color: var(--rm-ink-3);
          background: none;
          border: none;
          cursor: pointer;
          transition: 0.13s;
        }
        rm-mcp-servers-page .icon-btn:hover {
          background: var(--rm-surface-3);
          color: var(--rm-ink);
        }
        rm-mcp-servers-page .icon-btn.danger:hover {
          background: var(--rm-bad-subtle);
          color: var(--rm-bad);
        }
      </style>
      <ul class="divide-y divide-surface-3 dark:divide-d-surface-3 border border-surface-3 dark:border-d-surface-3 rounded-xl overflow-hidden">
        ${this.rows.map((r) => this.renderRow(r))}
      </ul>
    `;
  }

  private renderRow(r: MCPServer) {
    const delErr = this.deleteError[r.id] || '';
    // Wrap row in a hover-reveal action group identical in spirit to
    // the prototype's `cardActsHTML` pattern: icons hide at rest, ride
    // in on hover/focus, and use the shared icon-btn styles.
    return html`
      <li class="mcp-row px-4 py-3" data-mcp-id=${r.id}>
        <div class="flex items-start gap-3">
          <div class="min-w-0 flex-1">
            <div class="text-[14px] font-medium text-ink-0 dark:text-d-ink-0 truncate">
              ${r.name}
            </div>
            <div class="text-[11.5px] text-ink-3 dark:text-d-ink-3 flex flex-wrap items-center gap-2 mt-0.5">
              <span>${r.type}</span>
              <span class="text-ink-4">·</span>
              <span>auth: ${r.auth_mode}</span>
              ${r.auth_mode !== 'service'
                ? html`<span class="text-amber-700 dark:text-amber-300">
                    requires user session
                  </span>`
                : nothing}
            </div>
            <div class="text-[11.5px] text-ink-3 dark:text-d-ink-3 font-mono truncate mt-0.5">
              ${r.url}
            </div>
            ${r.description
              ? html`<div class="text-[12px] text-ink-2 dark:text-d-ink-2 mt-1">
                  ${r.description}
                </div>`
              : nothing}
          </div>
          <div class="row-acts flex items-center gap-1 shrink-0">
            <button
              type="button"
              class="icon-btn"
              title="Edit MCP server"
              data-testid="mcp-edit"
              @click=${() => this.openEdit(r)}
            >${iconPencil(15)}</button>
            <button
              type="button"
              class="icon-btn danger"
              title="Delete MCP server"
              data-testid="mcp-delete"
              @click=${() => void this.removeServer(r)}
            >${iconTrash(15)}</button>
          </div>
        </div>
        ${delErr
          ? html`<div class="text-[12px] text-red-600 dark:text-red-300 mt-2">${delErr}</div>`
          : null}
      </li>
    `;
  }
}
