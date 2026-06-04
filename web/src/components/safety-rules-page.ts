import { LitElement, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  getApiClient,
  type SafetyCheck,
  type SafetyRule,
  type SafetyRuleAuditEntry,
  type SafetyStage,
} from '../api/client.js';
import {
  createRule,
  deleteRule,
  listCoworkers,
  updateRule,
  type CoworkerSummary,
} from '../services/safety-admin-client.js';

type DraftRule = {
  stage: SafetyStage | '';
  check_id: string;
  coworker_id: string | null;
  config: string; // JSON string being edited
  priority: number;
  enabled: boolean;
  description: string;
};

const EMPTY_DRAFT: DraftRule = {
  stage: '',
  check_id: '',
  coworker_id: null,
  config: '{}',
  priority: 100,
  enabled: true,
  description: '',
};

// Full action set, in the order the picker lists them. Whether each is
// offered for a given (check, stage) comes from the check's
// server-driven ``supported_actions`` — we never hardcode that here.
const ALL_ACTIONS = [
  'block',
  'redact',
  'warn',
  'allow',
  'require_approval',
] as const;

// Why an action is unavailable for a (check, stage), shown as a tooltip
// on the greyed-out picker option. The server is the source of truth for
// WHICH are unavailable; these strings only explain the common reasons.
const UNSUPPORTED_REASON: Record<string, string> = {
  redact: 'this check cannot rewrite the payload',
  warn: 'nothing consumes warning context at this stage',
  require_approval: 'no approval step exists at this stage',
  block: 'not meaningful for this check at this stage',
  allow: 'not meaningful for this check at this stage',
};

@customElement('rm-safety-rules-page')
export class SafetyRulesPage extends LitElement {
  @state() private rules: SafetyRule[] = [];
  @state() private checks: SafetyCheck[] = [];
  @state() private coworkers: CoworkerSummary[] = [];
  @state() private loading = true;
  @state() private error: string | null = null;
  @state() private editingId: string | null = null;
  @state() private draft: DraftRule = { ...EMPTY_DRAFT };
  @state() private draftMode: 'closed' | 'create' | 'edit' = 'closed';
  @state() private busy = false;
  @state() private detailRuleId: string | null = null;
  @state() private detailAudit: SafetyRuleAuditEntry[] = [];

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback(): void {
    super.connectedCallback();
    void this.refresh();
  }

  private async refresh(): Promise<void> {
    this.loading = true;
    this.error = null;
    try {
      // Reads go through the typed v1 ApiClient; writes (create /
      // update / delete) keep using safety-admin-client because
      // safety rule mutation is an admin-only operation per design
      // §3 Phase 4.
      const api = getApiClient();
      const [rules, checks, coworkers] = await Promise.all([
        api.listSafetyRules(),
        api.listSafetyChecks(),
        listCoworkers(),
      ]);
      this.rules = rules;
      this.checks = checks;
      this.coworkers = coworkers;
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
    } finally {
      this.loading = false;
    }
  }

  private coworkerName(id: string | null): string {
    if (!id) return '(tenant-wide)';
    const cw = this.coworkers.find((c) => c.id === id);
    return cw ? cw.name : id.slice(0, 8);
  }

  private checkMeta(id: string): SafetyCheck | null {
    return this.checks.find((c) => c.id === id) ?? null;
  }

  // -- Action matrix helpers (server-driven; see SafetyCheck Protocol) --

  // True only when the config textarea holds a JSON object — the
  // override picker writes into it, so we refuse to touch un-parseable
  // JSON rather than clobber the operator's in-progress edit.
  private configIsObject(): boolean {
    try {
      const o = JSON.parse(this.draft.config || '{}');
      return typeof o === 'object' && o !== null && !Array.isArray(o);
    } catch {
      return false;
    }
  }

  private currentActionOverride(): string {
    try {
      const o = JSON.parse(this.draft.config || '{}');
      const v = (o as Record<string, unknown>)?.action_override;
      return typeof v === 'string' ? v : '';
    } catch {
      return '';
    }
  }

  // Patch (or clear, when action === '') the ``action_override`` key in
  // the draft config JSON, keeping the textarea authoritative.
  private setActionOverride(action: string): void {
    let obj: Record<string, unknown> = {};
    try {
      const parsed = JSON.parse(this.draft.config || '{}');
      if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
        obj = parsed as Record<string, unknown>;
      }
    } catch {
      obj = {};
    }
    if (action === '') delete obj['action_override'];
    else obj['action_override'] = action;
    this.draft = { ...this.draft, config: JSON.stringify(obj, null, 2) };
  }

  // The action a hit defaults to for the selected stage, or null when
  // the stage is not declared by the check.
  private naturalAction(meta: SafetyCheck, stage: SafetyStage): string | null {
    const na = meta.natural_actions as
      | Record<string, string | undefined>
      | undefined;
    return na?.[stage] ?? null;
  }

  private supportedActions(meta: SafetyCheck, stage: SafetyStage): string[] {
    const sa = meta.supported_actions as
      | Record<string, string[] | undefined>
      | undefined;
    return sa?.[stage] ?? [];
  }

  private openCreate(): void {
    this.draft = { ...EMPTY_DRAFT };
    this.draftMode = 'create';
    this.editingId = null;
  }

  private openEdit(rule: SafetyRule): void {
    this.draft = {
      stage: rule.stage,
      check_id: rule.check_id,
      // v1 SafetyRule.coworker_id is `string | null | undefined`
      // (codegen surfaces "optional + nullable" as both). Collapse
      // to a strict `string | null` for the draft state so the
      // <select> never receives `undefined` (DOM coerces to "").
      coworker_id: rule.coworker_id ?? null,
      config: JSON.stringify(rule.config ?? {}, null, 2),
      priority: rule.priority,
      enabled: rule.enabled,
      description: rule.description,
    };
    this.draftMode = 'edit';
    this.editingId = rule.id;
  }

  private closeDraft(): void {
    this.draftMode = 'closed';
    this.editingId = null;
    this.error = null;
  }

  private async submitDraft(): Promise<void> {
    this.busy = true;
    this.error = null;
    try {
      let config: Record<string, unknown>;
      try {
        config = JSON.parse(this.draft.config || '{}');
      } catch (err) {
        throw new Error(
          `config must be valid JSON: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
      if (!this.draft.stage) throw new Error('stage is required');
      if (!this.draft.check_id) throw new Error('check_id is required');

      if (this.draftMode === 'create') {
        await createRule({
          stage: this.draft.stage,
          check_id: this.draft.check_id,
          coworker_id: this.draft.coworker_id,
          config,
          priority: this.draft.priority,
          enabled: this.draft.enabled,
          description: this.draft.description,
        });
      } else if (this.draftMode === 'edit' && this.editingId) {
        await updateRule(this.editingId, {
          stage: this.draft.stage,
          check_id: this.draft.check_id,
          config,
          priority: this.draft.priority,
          enabled: this.draft.enabled,
          description: this.draft.description,
        });
      }
      this.closeDraft();
      await this.refresh();
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
    } finally {
      this.busy = false;
    }
  }

  private async toggleEnabled(rule: SafetyRule): Promise<void> {
    this.busy = true;
    this.error = null;
    try {
      await updateRule(rule.id, { enabled: !rule.enabled });
      await this.refresh();
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
    } finally {
      this.busy = false;
    }
  }

  private async removeRule(rule: SafetyRule): Promise<void> {
    if (!confirm(`Delete rule for ${rule.check_id} at ${rule.stage}?`)) return;
    this.busy = true;
    this.error = null;
    try {
      await deleteRule(rule.id);
      await this.refresh();
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
    } finally {
      this.busy = false;
    }
  }

  private async openDetail(rule: SafetyRule): Promise<void> {
    this.detailRuleId = rule.id;
    this.detailAudit = [];
    try {
      // tenant_id flows from the bearer token server-side (v1
      // surface) — no admin /tenant round-trip needed.
      this.detailAudit = await getApiClient().listSafetyRuleAudit(rule.id);
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
    }
  }

  private closeDetail(): void {
    this.detailRuleId = null;
    this.detailAudit = [];
  }

  // Server-driven action panel: a "defaults to" badge whose wording
  // depends on action_model, plus an override picker that greys out
  // actions the check cannot carry out for the selected stage.
  private renderActionPanel(meta: SafetyCheck | null) {
    const stage = this.draft.stage;
    if (!meta || !stage || !meta.stages.includes(stage as SafetyStage)) {
      return nothing;
    }
    const st = stage as SafetyStage;
    const natural = this.naturalAction(meta, st);
    const supported = this.supportedActions(meta, st);
    const model = meta.action_model;

    const badge =
      model === 'fixed'
        ? html`This check defaults to:
            <span class="font-mono font-semibold uppercase"
              >${natural ?? 'allow'}</span
            >`
        : model === 'config_routed'
          ? html`No fixed default — the action is chosen per-category in
              the config below; the check is inert until configured.`
          : html`This check votes
              <span class="font-mono">allow</span> on a match; the gateway
              blocks when no rule allows (effective action decided by
              aggregation).`;

    const override = this.currentActionOverride();
    const canEdit = this.configIsObject();

    return html`
      <div
        class="col-span-2 text-xs border rounded p-2 bg-surface-2 dark:bg-d-surface-2"
        data-testid="action-panel"
      >
        <div class="mb-2" data-testid="action-badge">${badge}</div>
        <label class="flex flex-col gap-1">
          <span class="text-gray-500"
            >Action override${canEdit
              ? nothing
              : html` <span class="text-amber-600"
                  >(fix config JSON to edit)</span
                >`}</span
          >
          <select
            class="border rounded px-2 py-1 bg-transparent"
            data-testid="action-override-select"
            ?disabled=${!canEdit}
            .value=${override}
            @change=${(e: Event) =>
              this.setActionOverride((e.target as HTMLSelectElement).value)}
          >
            <option value="">
              (use default${natural ? `: ${natural}` : ''})
            </option>
            ${ALL_ACTIONS.map((a) => {
              const ok = supported.includes(a);
              return html`<option
                value=${a}
                ?disabled=${!ok}
                title=${ok ? '' : (UNSUPPORTED_REASON[a] ?? '')}
              >
                ${a}${ok ? '' : ' — unavailable'}
              </option>`;
            })}
          </select>
        </label>
      </div>
    `;
  }

  private renderDraftForm() {
    if (this.draftMode === 'closed') return nothing;
    const meta = this.checkMeta(this.draft.check_id);
    const supportedStages = meta?.stages ?? [];
    return html`
      <div class="border rounded-md p-4 mb-4 bg-surface-1 dark:bg-d-surface-1">
        <h3 class="font-semibold mb-3">
          ${this.draftMode === 'create' ? 'New rule' : 'Edit rule'}
        </h3>
        <div class="grid grid-cols-2 gap-3">
          <label class="flex flex-col text-sm">
            Check
            <select
              class="mt-1 border rounded px-2 py-1 bg-transparent"
              .value=${this.draft.check_id}
              @change=${(e: Event) => {
                this.draft = {
                  ...this.draft,
                  check_id: (e.target as HTMLSelectElement).value,
                };
              }}
            >
              <option value="">(select)</option>
              ${this.checks.map(
                (c) => html`<option value=${c.id}>${c.id} (${c.cost_class})</option>`,
              )}
            </select>
          </label>
          <label class="flex flex-col text-sm">
            Stage
            <select
              class="mt-1 border rounded px-2 py-1 bg-transparent"
              .value=${this.draft.stage}
              @change=${(e: Event) => {
                this.draft = {
                  ...this.draft,
                  stage: (e.target as HTMLSelectElement).value as SafetyStage,
                };
              }}
            >
              <option value="">(select)</option>
              ${supportedStages.map((s) => html`<option value=${s}>${s}</option>`)}
              ${supportedStages.length === 0
                ? ['input_prompt', 'pre_tool_call', 'post_tool_result', 'model_output'].map(
                    (s) => html`<option value=${s}>${s}</option>`,
                  )
                : nothing}
            </select>
          </label>
          ${this.renderActionPanel(meta)}
          <label class="flex flex-col text-sm">
            Coworker
            <select
              class="mt-1 border rounded px-2 py-1 bg-transparent"
              .value=${this.draft.coworker_id ?? ''}
              @change=${(e: Event) => {
                const v = (e.target as HTMLSelectElement).value;
                this.draft = {
                  ...this.draft,
                  coworker_id: v === '' ? null : v,
                };
              }}
              ?disabled=${this.draftMode === 'edit'}
            >
              <option value="">(tenant-wide)</option>
              ${this.coworkers.map(
                (c) => html`<option value=${c.id}>${c.name}</option>`,
              )}
            </select>
          </label>
          <label class="flex flex-col text-sm">
            Priority
            <input
              type="number"
              class="mt-1 border rounded px-2 py-1 bg-transparent"
              .value=${String(this.draft.priority)}
              @change=${(e: Event) => {
                const v = Number((e.target as HTMLInputElement).value);
                this.draft = { ...this.draft, priority: Number.isFinite(v) ? v : 100 };
              }}
            />
          </label>
          <label class="flex items-center gap-2 text-sm col-span-2">
            <input
              type="checkbox"
              ?checked=${this.draft.enabled}
              @change=${(e: Event) => {
                this.draft = {
                  ...this.draft,
                  enabled: (e.target as HTMLInputElement).checked,
                };
              }}
            />
            enabled
          </label>
          <label class="flex flex-col text-sm col-span-2">
            Description
            <input
              type="text"
              class="mt-1 border rounded px-2 py-1 bg-transparent"
              .value=${this.draft.description}
              @input=${(e: Event) => {
                this.draft = {
                  ...this.draft,
                  description: (e.target as HTMLInputElement).value,
                };
              }}
            />
          </label>
          <label class="flex flex-col text-sm col-span-2">
            Config (JSON)
            <textarea
              class="mt-1 border rounded px-2 py-1 bg-transparent font-mono text-xs"
              rows="6"
              .value=${this.draft.config}
              @input=${(e: Event) => {
                this.draft = {
                  ...this.draft,
                  config: (e.target as HTMLTextAreaElement).value,
                };
              }}
            ></textarea>
            ${meta?.config_schema
              ? html`<details class="text-xs text-gray-500 mt-1">
                  <summary>schema</summary>
                  <pre class="overflow-x-auto">${JSON.stringify(meta.config_schema, null, 2)}</pre>
                </details>`
              : nothing}
          </label>
        </div>
        ${this.error
          ? html`<div class="text-red-500 text-sm mt-2">${this.error}</div>`
          : nothing}
        <div class="flex justify-end gap-2 mt-3">
          <button
            class="px-3 py-1 border rounded text-sm"
            ?disabled=${this.busy}
            @click=${this.closeDraft}
          >
            Cancel
          </button>
          <button
            class="px-3 py-1 bg-blue-600 text-white rounded text-sm"
            ?disabled=${this.busy}
            @click=${this.submitDraft}
          >
            ${this.busy ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    `;
  }

  private renderDetailModal() {
    if (!this.detailRuleId) return nothing;
    return html`
      <div
        class="fixed inset-0 bg-black/40 flex items-center justify-center z-50"
        @click=${this.closeDetail}
      >
        <div
          class="bg-surface-0 dark:bg-d-surface-0 border rounded-md w-[720px] max-w-[95vw] max-h-[80vh] overflow-auto p-4"
          @click=${(e: Event) => e.stopPropagation()}
        >
          <div class="flex justify-between items-center mb-3">
            <h3 class="font-semibold">Rule audit — ${this.detailRuleId.slice(0, 8)}</h3>
            <button class="text-sm px-2" @click=${this.closeDetail}>×</button>
          </div>
          ${this.detailAudit.length === 0
            ? html`<div class="text-sm text-gray-500">No audit events.</div>`
            : html`
                <table class="w-full text-sm">
                  <thead>
                    <tr class="text-left border-b">
                      <th class="py-1">When</th>
                      <th>Action</th>
                      <th>Actor</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${this.detailAudit.map(
                      (e) => html`
                        <tr class="border-b">
                          <td class="py-1">
                            ${new Date(e.created_at).toLocaleString()}
                          </td>
                          <td>${e.action}</td>
                          <td>${e.actor_user_id ?? '(system)'}</td>
                        </tr>
                      `,
                    )}
                  </tbody>
                </table>
              `}
        </div>
      </div>
    `;
  }

  override render() {
    return html`
      <div class="p-6 max-w-5xl mx-auto">
        <a href="#" class="inline-flex items-center gap-1 text-sm text-gray-500 hover:text-gray-900 dark:hover:text-gray-100 mb-3">
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M19 12H5"/><path d="M12 19l-7-7 7-7"/></svg>
          Back to chat
        </a>
        <div class="flex items-center justify-between mb-4">
          <h2 class="text-xl font-semibold">Safety rules</h2>
          <div class="flex gap-2">
            <button
              class="px-3 py-1 border rounded text-sm"
              @click=${() => void this.refresh()}
              ?disabled=${this.loading}
            >
              Refresh
            </button>
            <button
              class="px-3 py-1 bg-blue-600 text-white rounded text-sm"
              @click=${this.openCreate}
            >
              + New rule
            </button>
          </div>
        </div>

        ${this.renderDraftForm()}

        ${this.loading
          ? html`<div class="text-gray-500">Loading…</div>`
          : this.error && this.draftMode === 'closed'
            ? html`<div class="text-red-500">${this.error}</div>`
            : this.rules.length === 0
              ? html`<div class="text-gray-500">
                  No safety rules configured. Click <strong>+ New rule</strong> to
                  add one.
                </div>`
              : html`
                  <table class="w-full text-sm border">
                    <thead class="bg-surface-1 dark:bg-d-surface-1">
                      <tr class="text-left">
                        <th class="p-2">Check</th>
                        <th>Stage</th>
                        <th>Scope</th>
                        <th>Priority</th>
                        <th>Enabled</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      ${this.rules.map(
                        (r) => html`
                          <tr class="border-t hover:bg-surface-1 dark:hover:bg-d-surface-1">
                            <td class="p-2 font-mono text-xs">
                              <div>${r.check_id}</div>
                              ${r.description
                                ? html`<div class="text-gray-500 font-sans">
                                    ${r.description}
                                  </div>`
                                : nothing}
                            </td>
                            <td>${r.stage}</td>
                            <td>${this.coworkerName(r.coworker_id ?? null)}</td>
                            <td>${r.priority}</td>
                            <td>
                              <input
                                type="checkbox"
                                ?checked=${r.enabled}
                                ?disabled=${this.busy}
                                @change=${() => void this.toggleEnabled(r)}
                              />
                            </td>
                            <td class="text-right whitespace-nowrap p-2">
                              <button
                                class="text-xs underline mr-2"
                                @click=${() => void this.openDetail(r)}
                              >
                                audit
                              </button>
                              <button
                                class="text-xs underline mr-2"
                                @click=${() => this.openEdit(r)}
                              >
                                edit
                              </button>
                              <button
                                class="text-xs underline text-red-500"
                                @click=${() => void this.removeRule(r)}
                              >
                                delete
                              </button>
                            </td>
                          </tr>
                        `,
                      )}
                    </tbody>
                  </table>
                `}
        ${this.renderDetailModal()}
      </div>
    `;
  }
}
