// <rm-approval-policy-dialog> — create / edit / duplicate an HITL approval
// policy (spec §5.7-5.14), including the structured condition builder (§7
// grammar). One dialog backs all three flows:
//   - `editing` non-null  → edit  (PATCH; title "Edit approval policy")
//   - `duplicating` non-null → create, pre-filled (POST; "Duplicate …")
//   - both null            → create, defaults (POST; "New approval policy")
//
// Every field is visible at top level — no "More options" disclosure (§5.11);
// Priority and Enabled are core fields used on most policy creations. A live
// preview (§5.13) regenerates on every change using the same `conditionSentence`
// renderer as the list cards, so what the user previews is what the card shows.
//
// The condition builder exposes the shallow subset of the §7 grammar
// (`{always}` or a flat and/or of `{field,op,value}` leaves) — see
// `condition-form.ts`. A policy whose stored expression is too complex for the
// flat builder opens read-only on the condition (the other fields stay
// editable), so we never silently flatten a hand-crafted nested condition.

import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';

import './dialog.js';
import './combobox.js';
import { ApiError, getApiClient } from '../api/client.js';
import type {
  ApprovalPolicy,
  ApprovalPolicyCreate,
  ApprovalPolicyUpdate,
  MCPServer,
} from '../api/client.js';
import {
  CONDITION_OPS,
  type ConditionForm,
  type ConditionMode,
  type LeafRow,
  buildConditionExpr,
  conditionSentence,
  emptyRow,
  exprToForm,
} from './condition-form.js';

const INPUT_CLASS =
  'w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3 ' +
  'dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1 ' +
  'text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand';

@customElement('rm-approval-policy-dialog')
export class ApprovalPolicyDialog extends LitElement {
  @property({ type: Boolean }) open = false;
  @property({ attribute: false }) editing: ApprovalPolicy | null = null;
  /** Source policy when opening in duplicate mode — create flow, pre-filled. */
  @property({ attribute: false }) duplicating: ApprovalPolicy | null = null;

  @state() private mcpServerName = '';
  @state() private toolName = '*';
  @state() private priority = 0;
  @state() private enabled = true;
  @state() private mode: ConditionMode = 'always';
  @state() private connective: 'and' | 'or' = 'and';
  @state() private rows: LeafRow[] = [emptyRow()];
  /** False when the loaded condition is too complex for the flat builder. */
  @state() private conditionEditable = true;
  @state() private busy = false;
  @state() private err: string | null = null;
  /** Tenant's configured MCP servers, fetched on open to seed the server /
   *  tool suggestion lists. Best-effort: empty on fetch failure (the fields
   *  stay free-text regardless). */
  @state() private servers: MCPServer[] = [];

  private readonly api = getApiClient();

  protected override createRenderRoot() {
    return this;
  }

  override willUpdate(changed: Map<string, unknown>) {
    if (changed.has('open') && this.open) {
      this.seedForm();
      this.err = null;
      this.busy = false;
      void this.loadServers();
    }
  }

  /** Load the tenant's MCP servers to seed the suggestion lists. Best-effort —
   *  a failure just means no suggestions; the fields are still usable. */
  private async loadServers(): Promise<void> {
    try {
      this.servers = await this.api.listMCPServers();
    } catch {
      this.servers = [];
    }
  }

  /** Tool-name suggestions for the currently-typed server: the `*` wildcard
   *  plus the keys of that server's `tool_reversibility` map (the tool names
   *  the operator declared). There is no live tool list — tools are only known
   *  inside a connected container — so these declared names are the best
   *  always-available source, and they work even before the server connects.
   *  Returns just `*` when the typed server isn't a configured one; the field
   *  stays free-text either way. */
  private suggestedTools(): string[] {
    const match = this.servers.find((s) => s.name === this.mcpServerName);
    const declared = match ? Object.keys(match.tool_reversibility ?? {}) : [];
    return ['*', ...declared.filter((t) => t && t !== '*')];
  }

  /** The policy the form should seed from: the edit target, else the
   *  duplicate source, else nothing (defaults). */
  private seedSource(): ApprovalPolicy | null {
    return this.editing ?? this.duplicating;
  }

  private seedForm(): void {
    const src = this.seedSource();
    if (src) {
      this.mcpServerName = src.mcp_server_name;
      this.toolName = src.tool_name;
      this.priority = src.priority;
      this.enabled = src.enabled;
      const form: ConditionForm = exprToForm(src.condition_expr);
      this.mode = form.mode;
      this.connective = form.connective;
      this.rows = form.rows.length ? form.rows : [emptyRow()];
      this.conditionEditable = form.editable;
    } else {
      this.mcpServerName = '';
      this.toolName = '*';
      this.priority = 0;
      this.enabled = true;
      this.mode = 'always';
      this.connective = 'and';
      this.rows = [emptyRow()];
      this.conditionEditable = true;
    }
  }

  private close = () => {
    this.open = false;
    this.dispatchEvent(new CustomEvent('close', { bubbles: true, composed: true }));
  };

  private async submit(): Promise<void> {
    if (this.busy) return;
    if (this.mcpServerName.trim() === '' || this.toolName.trim() === '') {
      this.err = 'MCP server name and tool name are required.';
      return;
    }
    this.busy = true;
    this.err = null;
    try {
      let saved: ApprovalPolicy;
      if (this.editing) {
        const body: ApprovalPolicyUpdate = {
          mcp_server_name: this.mcpServerName.trim(),
          tool_name: this.toolName.trim(),
          priority: this.priority,
          enabled: this.enabled,
        };
        // Only resend the condition if it's still editable here — otherwise
        // leave the (complex) stored expression untouched.
        if (this.conditionEditable) {
          body.condition_expr = this.currentExpr();
        }
        saved = await this.api.updateApprovalPolicy(this.editing.id, body);
      } else {
        // Create — covers both the New and Duplicate flows (duplicate just
        // seeds the form; the POST is identical, server assigns a new id).
        const body: ApprovalPolicyCreate = {
          mcp_server_name: this.mcpServerName.trim(),
          tool_name: this.toolName.trim(),
          priority: this.priority,
          enabled: this.enabled,
          condition_expr: this.currentExpr(),
        };
        saved = await this.api.createApprovalPolicy(body);
      }
      this.dispatchEvent(
        new CustomEvent('approval-policy-saved', {
          detail: { policy: saved },
          bubbles: true,
          composed: true,
        }),
      );
      this.close();
    } catch (err) {
      this.err =
        err instanceof ApiError
          ? err.body?.message ?? `HTTP ${err.status}`
          : (err as Error).message;
    } finally {
      this.busy = false;
    }
  }

  /** condition_expr from the current form state (fail-closed per §5.14). */
  private currentExpr() {
    return buildConditionExpr({
      mode: this.mode,
      connective: this.connective,
      rows: this.rows,
    });
  }

  private updateRow(i: number, patch: Partial<LeafRow>): void {
    this.rows = this.rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r));
  }

  private addRow(): void {
    this.rows = [...this.rows, emptyRow()];
  }

  private removeRow(i: number): void {
    const next = this.rows.filter((_, idx) => idx !== i);
    this.rows = next.length ? next : [emptyRow()];
  }

  private renderConditionBuilder(): TemplateResult {
    if (!this.conditionEditable) {
      return html`
        <div
          class="text-[12.5px] text-ink-2 dark:text-d-ink-2 rounded-md border border-surface-3 dark:border-d-surface-3 p-2"
          data-testid="condition-readonly"
        >
          This policy uses an advanced condition that the form can't edit.
          The other fields are still editable; the condition is left as-is.
          <pre class="mt-1 text-[11.5px] overflow-x-auto">${JSON.stringify(
            this.seedSource()?.condition_expr ?? {},
            null,
            2,
          )}</pre>
        </div>
      `;
    }
    return html`
      <div class="flex flex-col gap-2">
        <div class="rm-seg" role="radiogroup" aria-label="When to require approval">
          <button
            type="button"
            class="${this.mode === 'always' ? 'rm-seg--on' : ''}"
            data-testid="mode-always"
            aria-pressed=${this.mode === 'always'}
            @click=${() => { this.mode = 'always'; }}
            ?disabled=${this.busy}
          >Every time</button>
          <button
            type="button"
            class="${this.mode === 'match' ? 'rm-seg--on' : ''}"
            data-testid="mode-match"
            aria-pressed=${this.mode === 'match'}
            @click=${() => {
              this.mode = 'match';
              if (this.rows.length === 0) this.rows = [emptyRow()];
            }}
            ?disabled=${this.busy}
          >Only when…</button>
        </div>
        ${this.mode === 'match' ? this.renderLeaves() : nothing}
      </div>
    `;
  }

  private renderLeaves(): TemplateResult {
    return html`
      <div class="pl-1 flex flex-col gap-2" data-testid="leaf-rows">
        ${this.rows.length > 1
          ? html`<div class="flex items-center gap-2 text-[12px]">
              <span class="text-ink-2 dark:text-d-ink-2">Combine with</span>
              <div class="rm-seg">
                <button
                  type="button"
                  class="${this.connective === 'and' ? 'rm-seg--on' : ''}"
                  data-testid="connective-and"
                  @click=${() => { this.connective = 'and'; }}
                  ?disabled=${this.busy}
                >All (AND)</button>
                <button
                  type="button"
                  class="${this.connective === 'or' ? 'rm-seg--on' : ''}"
                  data-testid="connective-or"
                  @click=${() => { this.connective = 'or'; }}
                  ?disabled=${this.busy}
                >Any (OR)</button>
              </div>
            </div>`
          : nothing}
        ${this.rows.map((r, i) => this.renderLeaf(r, i))}
        <button
          type="button"
          class="rm-add-secondary self-start"
          data-testid="add-row"
          @click=${this.addRow}
          ?disabled=${this.busy}
        >+ Add condition</button>
      </div>
    `;
  }

  private renderLeaf(r: LeafRow, i: number): TemplateResult {
    return html`
      <div class="flex items-center gap-2" data-testid="leaf-row">
        <input
          type="text"
          class="${INPUT_CLASS} flex-1"
          placeholder="field (e.g. amount)"
          data-testid="leaf-field"
          .value=${r.field}
          @input=${(e: Event) =>
            this.updateRow(i, { field: (e.target as HTMLInputElement).value })}
          ?disabled=${this.busy}
        />
        <select
          class="text-[13px] px-2 py-2 rounded-md border border-surface-3 dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1 text-ink-0 dark:text-d-ink-0"
          data-testid="leaf-op"
          .value=${r.op}
          @change=${(e: Event) =>
            this.updateRow(i, {
              op: (e.target as HTMLSelectElement).value as LeafRow['op'],
            })}
          ?disabled=${this.busy}
        >
          ${CONDITION_OPS.map((op) => html`<option value=${op}>${op}</option>`)}
        </select>
        <input
          type="text"
          class="${INPUT_CLASS} flex-1 font-mono"
          placeholder="value (100, &quot;USD&quot;, [&quot;a&quot;,&quot;b&quot;])"
          data-testid="leaf-value"
          .value=${r.value}
          @input=${(e: Event) =>
            this.updateRow(i, { value: (e.target as HTMLInputElement).value })}
          ?disabled=${this.busy}
        />
        <button
          type="button"
          class="rm-iconbtn rm-iconbtn--danger"
          title="Remove condition"
          data-testid="remove-row"
          @click=${() => this.removeRow(i)}
          ?disabled=${this.busy}
        >×</button>
      </div>
    `;
  }

  /** Live preview sentence (§5.13) — single source of truth shared with the
   *  list cards via `conditionSentence`. */
  private renderPreview(): TemplateResult {
    const server = this.mcpServerName.trim() || 'a server';
    const toolDisp = this.toolName.trim() === '*' ? 'any tool' : this.toolName.trim() || 'a tool';
    const expr =
      this.editing && !this.conditionEditable
        ? this.editing.condition_expr
        : this.currentExpr();
    const sentence = conditionSentence(expr);
    return html`
      <div class="rm-pol-preview" data-testid="policy-preview">
        When a coworker calls <code>${server} · ${toolDisp}</code>
        ${unsafeHTML(sentence)}, pause and ask the requester to confirm before
        running. Priority <b>${this.priority}</b>.${this.enabled
          ? nothing
          : html` <i>(disabled — won’t match until re-enabled)</i>`}
      </div>
    `;
  }

  private dialogTitle(): string {
    if (this.editing) return 'Edit approval policy';
    if (this.duplicating) return 'Duplicate approval policy';
    return 'New approval policy';
  }

  private saveLabel(): string {
    return this.editing ? 'Save changes' : 'Create policy';
  }

  override render(): TemplateResult {
    return html`
      <rm-dialog
        title=${this.dialogTitle()}
        ?open=${this.open}
        ?close-on-backdrop=${!this.busy}
        ?close-on-esc=${!this.busy}
        width="560px"
        @close=${this.close}
      >
        <p class="text-[12.5px] text-ink-2 dark:text-d-ink-2 mb-3">
          Require a human to sign off before a coworker runs a specific tool
          call. Approvals time out after 5 minutes and auto-reject.
        </p>
        <div class="mb-1 flex gap-3">
          <div class="flex-1 min-w-0">
            <label class="block text-[12.5px] font-medium mb-1">MCP server name</label>
            <rm-combobox
              .value=${this.mcpServerName}
              .options=${this.servers.map((s) => s.name)}
              placeholder="e.g. stripe"
              testid="mcp-server-name"
              ?disabled=${this.busy}
              @change=${(e: CustomEvent<{ value: string }>) => {
                this.mcpServerName = e.detail.value;
              }}
            ></rm-combobox>
          </div>
          <div class="flex-1 min-w-0">
            <label class="block text-[12.5px] font-medium mb-1">Tool name</label>
            <rm-combobox
              mono
              .value=${this.toolName}
              .options=${this.suggestedTools()}
              placeholder="exact tool, or *"
              testid="tool-name"
              ?disabled=${this.busy}
              @change=${(e: CustomEvent<{ value: string }>) => {
                this.toolName = e.detail.value;
              }}
            ></rm-combobox>
          </div>
        </div>
        <p class="mb-3 text-[11px] text-ink-3 dark:text-d-ink-3">
          Pick a configured server/tool, or type one that isn't connected yet.
        </p>
        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">Require approval</label>
          ${this.renderConditionBuilder()}
        </div>
        <div class="mb-2 flex items-end gap-4">
          <label class="text-[12.5px] font-medium">
            Priority
            <span class="text-ink-3 dark:text-d-ink-3 font-normal">higher wins on ties</span>
            <input
              type="number"
              class="${INPUT_CLASS} w-24 mt-1 block"
              data-testid="priority"
              .value=${String(this.priority)}
              @input=${(e: Event) => {
                this.priority = parseInt((e.target as HTMLInputElement).value, 10) || 0;
              }}
              ?disabled=${this.busy}
            />
          </label>
          <div class="text-[12.5px] font-medium">
            Status
            <button
              type="button"
              class="rm-pol-toggle ${this.enabled ? 'rm-pol-toggle--on' : ''} mt-1"
              data-testid="enabled"
              aria-pressed=${this.enabled}
              @click=${() => { this.enabled = !this.enabled; }}
              ?disabled=${this.busy}
            >
              <span>${this.enabled ? 'Enabled' : 'Disabled'}</span>
              <span class="rm-switch"></span>
            </button>
          </div>
        </div>

        ${this.renderPreview()}

        ${this.err
          ? html`<div
              class="text-[12.5px] text-red-600 dark:text-red-300 mt-2"
              role="alert"
              data-testid="form-error"
            >${this.err}</div>`
          : nothing}

        <div slot="footer" style="display: flex; gap: 8px; justify-content: flex-end;">
          <button
            type="button"
            class="rm-btn rm-btn--secondary"
            ?disabled=${this.busy}
            @click=${this.close}
          >Cancel</button>
          <button
            type="button"
            class="rm-btn rm-btn--primary"
            data-testid="submit"
            ?disabled=${this.busy}
            @click=${() => void this.submit()}
          >${this.busy ? 'Saving…' : this.saveLabel()}</button>
        </div>
      </rm-dialog>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'rm-approval-policy-dialog': ApprovalPolicyDialog;
  }
}
