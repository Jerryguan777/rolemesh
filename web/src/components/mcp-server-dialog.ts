// <rm-mcp-server-dialog> — small modal form to register an MCP
// server inline from the coworker wizard's Tools step.
//
// The v1.1 <rm-mcp-servers-page> hosts the same write via an inline
// panel; we deliberately do not reuse that panel because reopening
// the wizard mid-flow to switch to another page would lose draft.
// This dialog is a minimal subset (name / type / url / auth_mode +
// optional description) — enough to register the server so the
// wizard's tools list can pick it up. Detailed editing
// (extra_headers, tool_reversibility) stays on the dedicated page.

import { LitElement, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import './dialog.js';
import { ApiError, getApiClient } from '../api/client.js';
import type { MCPServer, MCPServerCreate } from '../api/client.js';

@customElement('rm-mcp-server-dialog')
export class MCPServerDialog extends LitElement {
  @property({ type: Boolean }) open = false;
  /** When set, the dialog runs in **edit mode**: form is seeded from
   *  this row, the submit button PATCHes instead of POSTs, and the
   *  emitted event is `mcp-server-updated` (not `-created`). Pass
   *  `null` (or omit) for the create flow. */
  @property({ attribute: false }) editing: MCPServer | null = null;

  @state() private form: MCPServerCreate = this.emptyForm();
  @state() private busy = false;
  @state() private err: string | null = null;

  private readonly api = getApiClient();

  protected override createRenderRoot() {
    return this;
  }

  override willUpdate(changed: Map<string, unknown>) {
    if (changed.has('open') && this.open) {
      // Seed form from `editing` for the edit flow; otherwise start
      // blank. The seed runs once per open transition so a parent
      // changing `editing` mid-open won't clobber the user's edits.
      this.form = this.editing
        ? {
            name: this.editing.name,
            type: this.editing.type,
            url: this.editing.url,
            auth_mode: this.editing.auth_mode,
            description: this.editing.description ?? null,
          }
        : this.emptyForm();
      this.err = null;
      this.busy = false;
    }
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

  private async save(): Promise<void> {
    if (!this.form.name.trim() || !this.form.url.trim()) {
      this.err = 'Name and URL are required.';
      return;
    }
    this.busy = true;
    this.err = null;
    try {
      // PATCH vs POST depending on which mode we're in. The events
      // are also distinct so the parent can refresh its list and tell
      // the user what just happened without inspecting the row id.
      const result = this.editing
        ? await this.api.updateMCPServer(this.editing.id, { ...this.form })
        : await this.api.createMCPServer({ ...this.form });
      this.dispatchEvent(
        new CustomEvent<{ server: MCPServer }>(
          this.editing ? 'mcp-server-updated' : 'mcp-server-created',
          {
            detail: { server: result },
            bubbles: true,
            composed: true,
          },
        ),
      );
      this.open = false;
      this.dispatchEvent(
        new CustomEvent('close', { bubbles: true, composed: true }),
      );
    } catch (err) {
      this.err =
        err instanceof ApiError
          ? err.body?.message ?? `${err.status}`
          : (err as Error).message;
    } finally {
      this.busy = false;
    }
  }

  private close = () => {
    this.open = false;
    this.dispatchEvent(new CustomEvent('close', { bubbles: true, composed: true }));
  };

  override render() {
    const title = this.editing
      ? `Edit MCP server: ${this.editing.name}`
      : 'Connect MCP server';
    return html`
      <rm-dialog
        title=${title}
        ?open=${this.open}
        ?close-on-backdrop=${!this.busy}
        ?close-on-esc=${!this.busy}
        width="480px"
        @close=${this.close}
      >
        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">Name</label>
          <input
            type="text"
            class="w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3
              dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
              text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand"
            placeholder="e.g. shopify-admin"
            .value=${this.form.name}
            @input=${(e: Event) => {
              this.form = { ...this.form, name: (e.target as HTMLInputElement).value };
            }}
            ?disabled=${this.busy}
          />
        </div>
        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">Transport</label>
          <select
            class="w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3
              dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
              text-ink-0 dark:text-d-ink-0"
            .value=${this.form.type}
            @change=${(e: Event) => {
              this.form = {
                ...this.form,
                type: (e.target as HTMLSelectElement).value as MCPServerCreate['type'],
              };
            }}
            ?disabled=${this.busy}
          >
            <option value="http">HTTP</option>
            <option value="sse">SSE</option>
          </select>
        </div>
        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">URL</label>
          <input
            type="text"
            class="w-full text-[13px] px-3 py-2 rounded-md border border-surface-3
              dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
              text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand
              font-mono"
            placeholder="https://mcp.example.com/sse"
            .value=${this.form.url}
            @input=${(e: Event) => {
              this.form = { ...this.form, url: (e.target as HTMLInputElement).value };
            }}
            ?disabled=${this.busy}
          />
        </div>
        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">Auth mode</label>
          <select
            class="w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3
              dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
              text-ink-0 dark:text-d-ink-0"
            .value=${this.form.auth_mode}
            @change=${(e: Event) => {
              this.form = {
                ...this.form,
                auth_mode: (e.target as HTMLSelectElement)
                  .value as MCPServerCreate['auth_mode'],
              };
            }}
            ?disabled=${this.busy}
          >
            <option value="service">Service credential</option>
            <option value="user">Per-user OIDC token</option>
            <option value="both">Either</option>
          </select>
        </div>

        ${this.err
          ? html`<div
              class="text-[12.5px] text-red-600 dark:text-red-300 mt-2"
              role="alert"
            >${this.err}</div>`
          : nothing}

        <div slot="footer" class="flex items-center gap-2">
          <button
            type="button"
            class="text-[12.5px] px-3 py-1.5 rounded-md border border-surface-3 dark:border-d-surface-3
              text-ink-2 dark:text-d-ink-2 hover:bg-surface-2 dark:hover:bg-d-surface-2 cursor-pointer
              disabled:opacity-60"
            ?disabled=${this.busy}
            @click=${this.close}
          >Cancel</button>
          <button
            type="button"
            class="text-[12.5px] px-3 py-1.5 rounded-md bg-brand text-white
              hover:bg-brand-dark transition-colors cursor-pointer
              disabled:opacity-60"
            ?disabled=${this.busy}
            @click=${() => void this.save()}
          >${this.busy ? 'Saving…' : this.editing ? 'Save changes' : 'Add server'}</button>
        </div>
      </rm-dialog>
    `;
  }
}
