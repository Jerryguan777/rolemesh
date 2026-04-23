import { LitElement, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  downloadDecisionsCsv,
  getDecision,
  getTenantId,
  listCoworkers,
  listDecisions,
  type CoworkerSummary,
  type DecisionsPage,
  type SafetyDecision,
  type SafetyStage,
  type SafetyVerdictAction,
} from '../services/safety-admin-client.js';

const PAGE_SIZE = 25;

@customElement('rm-safety-decisions-page')
export class SafetyDecisionsPage extends LitElement {
  @state() private tenantId: string | null = null;
  @state() private page: DecisionsPage = { total: 0, items: [] };
  @state() private coworkers: CoworkerSummary[] = [];
  @state() private loading = true;
  @state() private error: string | null = null;
  @state() private offset = 0;
  @state() private filters: {
    verdict_action?: SafetyVerdictAction;
    coworker_id?: string;
    stage?: SafetyStage;
    from_ts?: string;
    to_ts?: string;
  } = {};
  @state() private selected: SafetyDecision | null = null;

  protected override createRenderRoot() {
    return this;
  }

  override async connectedCallback(): Promise<void> {
    super.connectedCallback();
    try {
      this.tenantId = await getTenantId();
      this.coworkers = await listCoworkers();
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
    }
    await this.refresh();
  }

  private async refresh(): Promise<void> {
    if (!this.tenantId) return;
    this.loading = true;
    this.error = null;
    try {
      this.page = await listDecisions(this.tenantId, {
        ...this.filters,
        limit: PAGE_SIZE,
        offset: this.offset,
      });
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
    } finally {
      this.loading = false;
    }
  }

  private coworkerName(id: string | null): string {
    if (!id) return '—';
    const cw = this.coworkers.find((c) => c.id === id);
    return cw ? cw.name : id.slice(0, 8);
  }

  private setFilter<K extends keyof typeof this.filters>(
    key: K,
    value: (typeof this.filters)[K] | '',
  ): void {
    const next = { ...this.filters };
    if (value === '' || value === undefined) {
      delete next[key];
    } else {
      next[key] = value;
    }
    this.filters = next;
    this.offset = 0;
    void this.refresh();
  }

  private next(): void {
    if (this.offset + PAGE_SIZE < this.page.total) {
      this.offset += PAGE_SIZE;
      void this.refresh();
    }
  }

  private prev(): void {
    this.offset = Math.max(0, this.offset - PAGE_SIZE);
    void this.refresh();
  }

  private async exportCsv(): Promise<void> {
    if (!this.tenantId) return;
    try {
      const blob = await downloadDecisionsCsv(this.tenantId, this.filters);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      const today = new Date().toISOString().slice(0, 10);
      a.download = `safety-decisions-${today}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
    }
  }

  private async openDetail(row: SafetyDecision): Promise<void> {
    if (!this.tenantId) return;
    try {
      // Re-fetch to pick up fields the list view omits (approval_context,
      // context_digest). The list payload already has most of what we
      // show, but the detail endpoint is the source of truth for the
      // audit-trail read.
      this.selected = await getDecision(this.tenantId, row.id);
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
      this.selected = row;
    }
  }

  private closeDetail(): void {
    this.selected = null;
  }

  private verdictBadge(action: SafetyVerdictAction) {
    const colors: Record<SafetyVerdictAction, string> = {
      allow: 'bg-green-100 text-green-800',
      block: 'bg-red-100 text-red-800',
      redact: 'bg-yellow-100 text-yellow-800',
      warn: 'bg-orange-100 text-orange-800',
      require_approval: 'bg-purple-100 text-purple-800',
    };
    return html`
      <span class="px-2 py-0.5 text-xs rounded ${colors[action] ?? ''}">
        ${action}
      </span>
    `;
  }

  private renderFilters() {
    return html`
      <div class="flex flex-wrap gap-2 items-end mb-4">
        <label class="text-xs flex flex-col">
          verdict
          <select
            class="border rounded px-2 py-1 bg-transparent"
            .value=${this.filters.verdict_action ?? ''}
            @change=${(e: Event) =>
              this.setFilter(
                'verdict_action',
                (e.target as HTMLSelectElement).value as SafetyVerdictAction | '',
              )}
          >
            <option value="">any</option>
            <option value="allow">allow</option>
            <option value="block">block</option>
            <option value="redact">redact</option>
            <option value="warn">warn</option>
            <option value="require_approval">require_approval</option>
          </select>
        </label>
        <label class="text-xs flex flex-col">
          stage
          <select
            class="border rounded px-2 py-1 bg-transparent"
            .value=${this.filters.stage ?? ''}
            @change=${(e: Event) =>
              this.setFilter(
                'stage',
                (e.target as HTMLSelectElement).value as SafetyStage | '',
              )}
          >
            <option value="">any</option>
            <option value="input_prompt">input_prompt</option>
            <option value="pre_tool_call">pre_tool_call</option>
            <option value="post_tool_result">post_tool_result</option>
            <option value="model_output">model_output</option>
            <option value="pre_compaction">pre_compaction</option>
          </select>
        </label>
        <label class="text-xs flex flex-col">
          coworker
          <select
            class="border rounded px-2 py-1 bg-transparent"
            .value=${this.filters.coworker_id ?? ''}
            @change=${(e: Event) =>
              this.setFilter('coworker_id', (e.target as HTMLSelectElement).value)}
          >
            <option value="">any</option>
            ${this.coworkers.map(
              (c) => html`<option value=${c.id}>${c.name}</option>`,
            )}
          </select>
        </label>
        <label class="text-xs flex flex-col">
          from
          <input
            type="datetime-local"
            class="border rounded px-2 py-1 bg-transparent"
            .value=${this.filters.from_ts ?? ''}
            @change=${(e: Event) =>
              this.setFilter('from_ts', (e.target as HTMLInputElement).value)}
          />
        </label>
        <label class="text-xs flex flex-col">
          to
          <input
            type="datetime-local"
            class="border rounded px-2 py-1 bg-transparent"
            .value=${this.filters.to_ts ?? ''}
            @change=${(e: Event) =>
              this.setFilter('to_ts', (e.target as HTMLInputElement).value)}
          />
        </label>
        <button
          class="px-3 py-1 border rounded text-sm"
          @click=${() => void this.refresh()}
        >
          Refresh
        </button>
        <button
          class="px-3 py-1 border rounded text-sm"
          @click=${() => void this.exportCsv()}
        >
          Export CSV
        </button>
      </div>
    `;
  }

  private renderDetail() {
    if (!this.selected) return nothing;
    const d = this.selected;
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
            <h3 class="font-semibold">Decision ${d.id.slice(0, 8)}</h3>
            <button class="text-sm px-2" @click=${this.closeDetail}>×</button>
          </div>
          <dl class="grid grid-cols-3 gap-2 text-sm mb-4">
            <dt class="text-gray-500">When</dt>
            <dd class="col-span-2">${new Date(d.created_at).toLocaleString()}</dd>
            <dt class="text-gray-500">Verdict</dt>
            <dd class="col-span-2">${this.verdictBadge(d.verdict_action)}</dd>
            <dt class="text-gray-500">Stage</dt>
            <dd class="col-span-2">${d.stage}</dd>
            <dt class="text-gray-500">Coworker</dt>
            <dd class="col-span-2">${this.coworkerName(d.coworker_id)}</dd>
            <dt class="text-gray-500">Rule ids</dt>
            <dd class="col-span-2 font-mono text-xs">
              ${d.triggered_rule_ids.length === 0 ? '—' : d.triggered_rule_ids.join(', ')}
            </dd>
            <dt class="text-gray-500">Summary</dt>
            <dd class="col-span-2 font-mono text-xs">${d.context_summary || '—'}</dd>
            <dt class="text-gray-500">Digest</dt>
            <dd class="col-span-2 font-mono text-xs">${d.context_digest || '—'}</dd>
          </dl>
          <h4 class="font-semibold text-sm mb-2">Findings</h4>
          ${d.findings.length === 0
            ? html`<div class="text-sm text-gray-500">No findings.</div>`
            : html`
                <table class="w-full text-xs border">
                  <thead class="bg-surface-1 dark:bg-d-surface-1">
                    <tr class="text-left">
                      <th class="p-1">Code</th>
                      <th>Severity</th>
                      <th>Message</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${d.findings.map(
                      (f) => html`
                        <tr class="border-t">
                          <td class="p-1 font-mono">${f.code}</td>
                          <td>${f.severity}</td>
                          <td>${f.message}</td>
                        </tr>
                      `,
                    )}
                  </tbody>
                </table>
              `}
          ${d.approval_context
            ? html`
                <h4 class="font-semibold text-sm mt-4 mb-2">
                  Approval context
                  <span class="text-xs text-gray-500 font-normal">
                    (cleared 24h after row creation)
                  </span>
                </h4>
                <pre class="bg-surface-1 dark:bg-d-surface-1 p-2 text-xs overflow-auto">
${JSON.stringify(d.approval_context, null, 2)}</pre
                >
              `
            : nothing}
        </div>
      </div>
    `;
  }

  override render() {
    const start = this.page.total === 0 ? 0 : this.offset + 1;
    const end = Math.min(this.offset + PAGE_SIZE, this.page.total);
    return html`
      <div class="p-6 max-w-6xl mx-auto">
        <div class="flex items-center justify-between mb-4">
          <h2 class="text-xl font-semibold">Safety decisions</h2>
          <div class="text-sm text-gray-500">
            ${this.page.total === 0
              ? '0 records'
              : `${start}–${end} of ${this.page.total}`}
          </div>
        </div>

        ${this.renderFilters()}
        ${this.error
          ? html`<div class="text-red-500 text-sm mb-2">${this.error}</div>`
          : nothing}
        ${this.loading
          ? html`<div class="text-gray-500">Loading…</div>`
          : this.page.items.length === 0
            ? html`<div class="text-gray-500">No decisions match these filters.</div>`
            : html`
                <table class="w-full text-sm border">
                  <thead class="bg-surface-1 dark:bg-d-surface-1">
                    <tr class="text-left">
                      <th class="p-2">When</th>
                      <th>Verdict</th>
                      <th>Stage</th>
                      <th>Coworker</th>
                      <th>Findings</th>
                      <th>Summary</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${this.page.items.map(
                      (d) => html`
                        <tr
                          class="border-t cursor-pointer hover:bg-surface-1 dark:hover:bg-d-surface-1"
                          @click=${() => void this.openDetail(d)}
                        >
                          <td class="p-2 text-xs">
                            ${new Date(d.created_at).toLocaleString()}
                          </td>
                          <td>${this.verdictBadge(d.verdict_action)}</td>
                          <td class="text-xs">${d.stage}</td>
                          <td class="text-xs">${this.coworkerName(d.coworker_id)}</td>
                          <td class="text-xs font-mono">
                            ${d.findings.map((f) => f.code).join(', ') || '—'}
                          </td>
                          <td class="text-xs font-mono truncate max-w-[20ch]">
                            ${d.context_summary || '—'}
                          </td>
                        </tr>
                      `,
                    )}
                  </tbody>
                </table>
                <div class="flex justify-end gap-2 mt-3">
                  <button
                    class="px-3 py-1 border rounded text-sm"
                    ?disabled=${this.offset === 0}
                    @click=${this.prev}
                  >
                    ← Prev
                  </button>
                  <button
                    class="px-3 py-1 border rounded text-sm"
                    ?disabled=${this.offset + PAGE_SIZE >= this.page.total}
                    @click=${this.next}
                  >
                    Next →
                  </button>
                </div>
              `}
        ${this.renderDetail()}
      </div>
    `;
  }
}
