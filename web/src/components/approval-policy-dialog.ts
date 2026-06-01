// <rm-approval-policy-dialog> — create / edit an HITL approval policy
// (docs/21-hitl-approval-plan.md §10 S5), including the structured condition
// builder (§7 grammar). One dialog backs both flows: `editing` null = create
// (POST), non-null = edit (PATCH).
//
// The condition builder exposes the shallow subset of the §7 grammar
// (`{always}` or a flat and/or of `{field,op,value}` leaves) — see
// `condition-form.ts`. A policy whose stored expression is too complex for the
// flat builder opens read-only on the condition (the other fields stay
// editable), so we never silently flatten a hand-crafted nested condition.

import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import './dialog.js';
import { ApiError, getApiClient } from '../api/client.js';
import type {
  ApprovalPolicy,
  ApprovalPolicyCreate,
  ApprovalPolicyUpdate,
} from '../api/client.js';
import {
  CONDITION_OPS,
  type ConditionForm,
  type ConditionMode,
  type LeafRow,
  buildConditionExpr,
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

  private readonly api = getApiClient();

  protected override createRenderRoot() {
    return this;
  }

  override willUpdate(changed: Map<string, unknown>) {
    if (changed.has('open') && this.open) {
      this.seedForm();
      this.err = null;
      this.busy = false;
    }
  }

  private seedForm(): void {
    if (this.editing) {
      this.mcpServerName = this.editing.mcp_server_name;
      this.toolName = this.editing.tool_name;
      this.priority = this.editing.priority;
      this.enabled = this.editing.enabled;
      const form: ConditionForm = exprToForm(this.editing.condition_expr);
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
          body.condition_expr = buildConditionExpr({
            mode: this.mode,
            connective: this.connective,
            rows: this.rows,
          });
        }
        await this.api.updateApprovalPolicy(this.editing.id, body);
        this.dispatchEvent(
          new CustomEvent('approval-policy-updated', { bubbles: true, composed: true }),
        );
      } else {
        const body: ApprovalPolicyCreate = {
          mcp_server_name: this.mcpServerName.trim(),
          tool_name: this.toolName.trim(),
          priority: this.priority,
          enabled: this.enabled,
          condition_expr: buildConditionExpr({
            mode: this.mode,
            connective: this.connective,
            rows: this.rows,
          }),
        };
        await this.api.createApprovalPolicy(body);
        this.dispatchEvent(
          new CustomEvent('approval-policy-created', { bubbles: true, composed: true }),
        );
      }
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
            this.editing?.condition_expr ?? {},
            null,
            2,
          )}</pre>
        </div>
      `;
    }
    return html`
      <div class="flex flex-col gap-2">
        <label class="flex items-center gap-2 text-[12.5px]">
          <input
            type="radio"
            name="cond-mode"
            data-testid="mode-always"
            ?checked=${this.mode === 'always'}
            @change=${() => { this.mode = 'always'; }}
            ?disabled=${this.busy}
          />
          Always require approval for this tool
        </label>
        <label class="flex items-center gap-2 text-[12.5px]">
          <input
            type="radio"
            name="cond-mode"
            data-testid="mode-match"
            ?checked=${this.mode === 'match'}
            @change=${() => { this.mode = 'match'; }}
            ?disabled=${this.busy}
          />
          Only when a condition matches
        </label>
        ${this.mode === 'match' ? this.renderLeaves() : nothing}
      </div>
    `;
  }

  private renderLeaves(): TemplateResult {
    return html`
      <div class="pl-6 flex flex-col gap-2" data-testid="leaf-rows">
        ${this.rows.length > 1
          ? html`<div class="text-[12px]">
              Match
              <select
                class="text-[12px] px-1 py-0.5 rounded border border-surface-3 dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1"
                .value=${this.connective}
                data-testid="connective"
                @change=${(e: Event) => {
                  this.connective = (e.target as HTMLSelectElement).value as
                    | 'and'
                    | 'or';
                }}
                ?disabled=${this.busy}
              >
                <option value="and">all</option>
                <option value="or">any</option>
              </select>
              of:
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

  override render(): TemplateResult {
    const title = this.editing ? 'Edit approval policy' : 'New approval policy';
    return html`
      <rm-dialog
        title=${title}
        ?open=${this.open}
        ?close-on-backdrop=${!this.busy}
        ?close-on-esc=${!this.busy}
        width="560px"
        @close=${this.close}
      >
        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">MCP server name</label>
          <input
            type="text"
            class=${INPUT_CLASS}
            placeholder="e.g. stripe"
            data-testid="mcp-server-name"
            .value=${this.mcpServerName}
            @input=${(e: Event) => {
              this.mcpServerName = (e.target as HTMLInputElement).value;
            }}
            ?disabled=${this.busy}
          />
        </div>
        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">Tool name</label>
          <input
            type="text"
            class="${INPUT_CLASS} font-mono"
            placeholder="exact tool name, or * for every tool"
            data-testid="tool-name"
            .value=${this.toolName}
            @input=${(e: Event) => {
              this.toolName = (e.target as HTMLInputElement).value;
            }}
            ?disabled=${this.busy}
          />
        </div>
        <div class="mb-3 flex items-center gap-4">
          <label class="text-[12.5px] font-medium">
            Priority
            <input
              type="number"
              class="${INPUT_CLASS} w-20 ml-2 inline-block"
              data-testid="priority"
              .value=${String(this.priority)}
              @input=${(e: Event) => {
                this.priority = Number((e.target as HTMLInputElement).value) || 0;
              }}
              ?disabled=${this.busy}
            />
          </label>
          <label class="flex items-center gap-2 text-[12.5px] font-medium">
            <input
              type="checkbox"
              data-testid="enabled"
              ?checked=${this.enabled}
              @change=${(e: Event) => {
                this.enabled = (e.target as HTMLInputElement).checked;
              }}
              ?disabled=${this.busy}
            />
            Enabled
          </label>
        </div>
        <div class="mb-2">
          <label class="block text-[12.5px] font-medium mb-1">Condition</label>
          ${this.renderConditionBuilder()}
        </div>

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
          >${this.busy ? 'Saving…' : this.editing ? 'Save' : 'Create'}</button>
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
