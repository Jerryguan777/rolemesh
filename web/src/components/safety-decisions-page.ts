// Safety log page (#/manage/safety-log) — spec §7.
//
// The audit-visible expression of the safety framework: every check decision
// lands here (allows and blocks). Three workflows — tune false positives,
// investigate a blocked workflow, audit. Moved from the Activity surface to
// Settings → Governance (spec §7); the old paths redirect here (router.ts).
//
// Reads go through the typed v1 ApiClient. CSV export stays on the admin
// surface (v1 is GET-only, no CSV — design §3 Phase 4), so this file is on
// the lint:no-admin-chat allowlist.
//
// Filter bar caveat: the v1 `/safety/decisions` endpoint filters by verdict /
// stage / coworker / time, but NOT by check — there is no `check_id` query
// param on the contract and we don't extend it here. So the spec's 4th "check"
// dropdown is intentionally omitted (a note in the bar says so). Finding codes
// in each row already convey what a decision caught.

import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import {
  getApiClient,
  type SafetyDecision,
  type SafetyDecisionPage,
  type SafetyRule,
  type SafetyStage,
  type SafetyVerdictAction,
} from '../api/client.js';
import {
  downloadDecisionsCsv,
  getTenantId,
  listCoworkers,
  type CoworkerSummary,
} from '../services/safety-admin-client.js';
import './safety-decision-detail-dialog.js';
import { checkLabel } from './safety-catalog.js';

const PAGE_SIZE = 10;

type Filters = {
  verdict_action?: SafetyVerdictAction;
  coworker_id?: string;
  stage?: SafetyStage;
  from_ts?: string;
  to_ts?: string;
};

const VERDICTS: SafetyVerdictAction[] = [
  'allow',
  'block',
  'redact',
  'warn',
  'require_approval',
];
const STAGES: [SafetyStage, string][] = [
  ['input_prompt', 'Input'],
  ['pre_tool_call', 'Before tool calls'],
  ['post_tool_result', 'After tool results'],
  ['model_output', 'Model output'],
  ['pre_compaction', 'Compaction'],
  ['egress_request', 'Network egress'],
];

@customElement('rm-safety-decisions-page')
export class SafetyDecisionsPage extends LitElement {
  // Cached so the admin CSV endpoint (not on v1) gets the tenant in its URL.
  // Decisions reads themselves derive tenant from auth.
  @state() private tenantId: string | null = null;
  @state() private decisions: SafetyDecisionPage = { total: 0, items: [] };
  @state() private coworkers: CoworkerSummary[] = [];
  // rule_id → check label, for the detail modal's "triggered rule" cell.
  @state() private ruleLabels: Record<string, string> = {};
  @state() private loading = true;
  @state() private error: string | null = null;
  @state() private offset = 0;
  @state() private filters: Filters = {};
  @state() private selected: SafetyDecision | null = null;

  private readonly api = getApiClient();

  protected override createRenderRoot() {
    return this;
  }

  override async connectedCallback(): Promise<void> {
    super.connectedCallback();
    // Admin-surface lookups (tenant id for CSV, coworker names) and the rule
    // list (for the detail modal's rule→label map) are best-effort — a failure
    // must not block the v1 decisions read.
    try {
      this.tenantId = await getTenantId();
    } catch {
      this.tenantId = null;
    }
    try {
      this.coworkers = await listCoworkers();
    } catch {
      this.coworkers = [];
    }
    try {
      const rules = await this.api.listSafetyRules();
      this.ruleLabels = Object.fromEntries(
        rules.map((r: SafetyRule) => [r.id, checkLabel(r.check_id)]),
      );
    } catch {
      this.ruleLabels = {};
    }
    await this.refresh();
  }

  private async refresh(): Promise<void> {
    this.loading = true;
    this.error = null;
    try {
      this.decisions = await this.api.listSafetyDecisions({
        verdictAction: this.filters.verdict_action,
        coworkerId: this.filters.coworker_id,
        stage: this.filters.stage,
        fromTs: this.filters.from_ts,
        toTs: this.filters.to_ts,
        limit: PAGE_SIZE,
        offset: this.offset,
      });
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
    } finally {
      this.loading = false;
    }
  }

  private coworkerName(id: string | null | undefined): string | null {
    if (!id) return null;
    const cw = this.coworkers.find((c) => c.id === id);
    return cw ? cw.name : id.slice(0, 8);
  }

  private setFilter<K extends keyof Filters>(key: K, value: string): void {
    const next = { ...this.filters };
    if (value === '') delete next[key];
    else next[key] = value as Filters[K];
    this.filters = next;
    this.offset = 0;
    void this.refresh();
  }

  private clearFilters(): void {
    this.filters = {};
    this.offset = 0;
    void this.refresh();
  }

  private next(): void {
    if (this.offset + PAGE_SIZE < this.decisions.total) {
      this.offset += PAGE_SIZE;
      void this.refresh();
    }
  }

  private prev(): void {
    this.offset = Math.max(0, this.offset - PAGE_SIZE);
    void this.refresh();
  }

  private async openDetail(row: SafetyDecision): Promise<void> {
    try {
      // Re-fetch for fields the list view may not project (full findings).
      this.selected = await this.api.getSafetyDecision(row.id);
    } catch {
      this.selected = row;
    }
  }

  private closeDetail = (): void => {
    this.selected = null;
  };

  private async exportCsv(): Promise<void> {
    if (!this.tenantId) return;
    try {
      const blob = await downloadDecisionsCsv(this.tenantId, this.filters);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `safety-log-${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
    }
  }

  override render(): TemplateResult {
    const items = this.decisions.items ?? [];
    const total = this.decisions.total;
    const start = total === 0 ? 0 : this.offset + 1;
    const end = Math.min(this.offset + PAGE_SIZE, total);
    return html`
      <div class="rm-spane">
        <div class="rm-ch">
          <h2>Safety log</h2>
          <div style="display: flex; gap: 8px; margin-left: auto">
            <button type="button" class="rm-btn rm-btn--secondary"
              @click=${() => void this.refresh()} ?disabled=${this.loading}>
              Refresh
            </button>
            <button type="button" class="rm-btn rm-btn--secondary"
              data-testid="saf-export-csv"
              @click=${() => void this.exportCsv()} ?disabled=${!this.tenantId}>
              Export CSV
            </button>
          </div>
        </div>
        <p class="rm-sub">
          Every check decision lands here — both allows and blocks. Raw payloads
          are never stored, only a digest and a short summary. To root-cause,
          open the conversation around the timestamp.
        </p>

        ${this.renderFilters()}

        ${this.error
          ? html`<div class="rm-banner-err">${this.error}</div>`
          : this.loading
            ? html`<div class="rm-banner-loading">Loading…</div>`
            : items.length === 0
              ? this.renderEmpty()
              : html`
                  <div class="rm-saf-decisions" data-testid="saf-log-list">
                    ${items.map((d) => this.renderRow(d))}
                  </div>
                  <div class="rm-saf-pager">
                    <span>Showing ${start}–${end} of ${total}</span>
                    <div class="rm-saf-pager-btns">
                      <button type="button" class="rm-btn rm-btn--secondary"
                        ?disabled=${this.offset === 0} @click=${() => this.prev()}>
                        ← Previous
                      </button>
                      <button type="button" class="rm-btn rm-btn--secondary"
                        ?disabled=${this.offset + PAGE_SIZE >= total}
                        @click=${() => this.next()}>
                        Next →
                      </button>
                    </div>
                  </div>
                `}

        <rm-safety-decision-detail-dialog
          ?open=${this.selected !== null}
          .decision=${this.selected}
          .coworkerName=${this.coworkerName(this.selected?.coworker_id)}
          .ruleLabels=${this.ruleLabels}
          @close=${this.closeDetail}
        ></rm-safety-decision-detail-dialog>
      </div>
    `;
  }

  private renderFilters(): TemplateResult {
    return html`
      <div class="rm-saf-filters" data-testid="saf-filters">
        <select
          aria-label="Filter by verdict"
          data-testid="saf-filter-verdict"
          @change=${(e: Event) =>
            this.setFilter('verdict_action', (e.target as HTMLSelectElement).value)}
        >
          <option value="" ?selected=${!this.filters.verdict_action}>all verdicts</option>
          ${VERDICTS.map(
            (v) => html`<option value=${v} ?selected=${this.filters.verdict_action === v}>
              ${v}
            </option>`,
          )}
        </select>
        <select
          aria-label="Filter by stage"
          data-testid="saf-filter-stage"
          @change=${(e: Event) =>
            this.setFilter('stage', (e.target as HTMLSelectElement).value)}
        >
          <option value="" ?selected=${!this.filters.stage}>all stages</option>
          ${STAGES.map(
            ([v, l]) => html`<option value=${v} ?selected=${this.filters.stage === v}>
              ${l}
            </option>`,
          )}
        </select>
        <select
          aria-label="Filter by coworker"
          data-testid="saf-filter-coworker"
          @change=${(e: Event) =>
            this.setFilter('coworker_id', (e.target as HTMLSelectElement).value)}
        >
          <option value="" ?selected=${!this.filters.coworker_id}>all coworkers</option>
          ${this.coworkers.map(
            (c) => html`<option value=${c.id} ?selected=${this.filters.coworker_id === c.id}>
              ${c.name}
            </option>`,
          )}
        </select>
        <input
          type="datetime-local"
          aria-label="From"
          data-testid="saf-filter-from"
          .value=${this.filters.from_ts ?? ''}
          @change=${(e: Event) =>
            this.setFilter('from_ts', (e.target as HTMLInputElement).value)}
        />
        <input
          type="datetime-local"
          aria-label="To"
          data-testid="saf-filter-to"
          .value=${this.filters.to_ts ?? ''}
          @change=${(e: Event) =>
            this.setFilter('to_ts', (e.target as HTMLInputElement).value)}
        />
        <button type="button" class="rm-saf-clear" data-testid="saf-clear-filters"
          @click=${() => this.clearFilters()}>
          Clear filters
        </button>
      </div>
    `;
  }

  private renderRow(d: SafetyDecision): TemplateResult {
    const ts = new Date(d.created_at).toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
    // SafetyDecision carries no check_id; the finding codes are the wire's
    // signal of what the decision caught.
    const findingCodes =
      (d.findings ?? []).map((f) => f.code).join(', ') || '—';
    const cw = this.coworkerName(d.coworker_id) ?? 'organization-wide';
    return html`
      <button type="button" class="rm-saf-row" data-testid="saf-log-row"
        @click=${() => void this.openDetail(d)}>
        <span class="rm-saf-ts">${ts}</span>
        <span class="rm-saf-verdict rm-saf-v-${d.verdict_action}">${d.verdict_action}</span>
        <span class="rm-saf-stage">${d.stage}</span>
        <span class="rm-saf-check" title=${findingCodes}>${findingCodes}</span>
        <span class="rm-saf-summary">${d.context_summary || '—'}</span>
        <span class="rm-saf-cw">${cw}</span>
        <span class="rm-saf-arr">›</span>
      </button>
    `;
  }

  private renderEmpty(): TemplateResult {
    return html`
      <div class="rm-pol-empty" data-testid="saf-log-empty">
        <div class="rm-pol-empty-icon" style="color: var(--rm-good)">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" stroke-width="2.4" aria-hidden="true">
            <path d="M20 6 9 17l-5-5" />
          </svg>
        </div>
        <p>No decisions match your filters.</p>
        <p class="rm-pol-empty-sub">
          Try clearing some filters, or wait for new agent activity.
        </p>
      </div>
    `;
  }
}
