// Safety rules page (#/manage/safety) — spec §6.
//
// Lists the organization's safety rules in two tiers (spec §6.2): platform
// defaults (PR #49 — cross-tenant, read-only, audit-only) on top, then the
// organization's own rules. Each row reuses the approval-policy card chrome
// (priority badge / sentence / always-visible toggle / hover actions) plus
// safety-specific chips (slow / scope / action pill). Create/edit/duplicate
// go through <rm-safety-rule-dialog>; delete through <rm-confirm-dialog>; a
// per-rule change history opens in an audit drawer.
//
// Reads go through the typed v1 ApiClient. Writes (toggle / delete) stay on
// safety-admin-client because safety-rule mutation is admin-privileged
// (design §3 Phase 4); this file is on the lint:no-admin-chat allowlist.

import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';

import { getApiClient } from '../api/client.js';
import type {
  SafetyCheck,
  SafetyRule,
  SafetyRuleAuditEntry,
} from '../api/client.js';
import {
  deleteRule,
  listCoworkers,
  updateRule,
  type CoworkerSummary,
} from '../services/safety-admin-client.js';
import './dialog.js';
import './confirm-dialog.js';
import './safety-rule-dialog.js';
import { iconCopy, iconPencil, iconTrash } from './icons.js';
import {
  checkLabel,
  effectiveAction,
  safActionPillClass,
  safSentence,
} from './safety-catalog.js';

/** Priority badge tint — mirrors approval policies (≥10 amber, 0 muted). */
export function priorityBadgeClass(priority: number): string {
  if (priority >= 10) return 'rm-pol-pri--hi';
  if (priority === 0) return 'rm-pol-pri--zero';
  return '';
}

/** Sort within a tier: priority desc, then newest first on ties (§6.6). */
export function sortRules(rows: SafetyRule[]): SafetyRule[] {
  return [...rows].sort(
    (a, b) =>
      b.priority - a.priority ||
      Date.parse(b.created_at) - Date.parse(a.created_at),
  );
}

/** Human one-line summary of an audit entry (§6.9). The wire ships
 *  before_state / after_state snapshots (no server summary), so we diff the
 *  fields an operator cares about. Kept deliberately small + deterministic. */
export function auditSummary(entry: SafetyRuleAuditEntry): string {
  if (entry.action === 'created') {
    const a = entry.after_state ?? {};
    const check = checkLabel(String(a['check_id'] ?? ''));
    return `Created — ${check}${a['stage'] ? `, ${String(a['stage'])}` : ''}`;
  }
  if (entry.action === 'deleted') return 'Deleted';
  const before = entry.before_state ?? {};
  const after = entry.after_state ?? {};
  const fields = ['priority', 'enabled', 'stage'];
  const parts: string[] = [];
  for (const f of fields) {
    if (JSON.stringify(before[f]) !== JSON.stringify(after[f])) {
      parts.push(`${f}: ${fmtVal(before[f])} → ${fmtVal(after[f])}`);
    }
  }
  // action_override lives inside config; surface it specifically.
  const bOv = (before['config'] as Record<string, unknown> | undefined)?.['action_override'];
  const aOv = (after['config'] as Record<string, unknown> | undefined)?.['action_override'];
  if (JSON.stringify(bOv) !== JSON.stringify(aOv)) {
    parts.push(`action: ${fmtVal(bOv ?? 'default')} → ${fmtVal(aOv ?? 'default')}`);
  }
  return parts.length ? parts.join('; ') : 'Configuration updated';
}

function fmtVal(v: unknown): string {
  if (v === true) return 'on';
  if (v === false) return 'off';
  return String(v);
}

@customElement('rm-safety-rules-page')
export class SafetyRulesPage extends LitElement {
  @state() private rules: SafetyRule[] = [];
  @state() private checks: SafetyCheck[] = [];
  @state() private coworkers: CoworkerSummary[] = [];
  @state() private loading = true;
  @state() private listError: string | null = null;

  @state() private dialogOpen = false;
  @state() private editTarget: SafetyRule | null = null;
  @state() private duplicateSource: SafetyRule | null = null;

  @state() private deleteTarget: SafetyRule | null = null;
  @state() private deleteInFlight = false;

  @state() private auditTarget: SafetyRule | null = null;
  @state() private auditEntries: SafetyRuleAuditEntry[] = [];
  @state() private auditLoading = false;

  @state() private togglingIds: Set<string> = new Set();
  @state() private toast: string | null = null;
  @state() private highlightId: string | null = null;

  private readonly api = getApiClient();
  private toastTimer: number | null = null;
  private highlightTimer: number | null = null;

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback(): void {
    super.connectedCallback();
    void this.refresh();
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    if (this.toastTimer) clearTimeout(this.toastTimer);
    if (this.highlightTimer) clearTimeout(this.highlightTimer);
  }

  private async refresh(): Promise<void> {
    this.loading = true;
    this.listError = null;
    try {
      const [rules, checks, coworkers] = await Promise.all([
        this.api.listSafetyRules(),
        this.api.listSafetyChecks(),
        listCoworkers(),
      ]);
      this.rules = rules;
      this.checks = checks;
      this.coworkers = coworkers;
    } catch (err) {
      this.listError = this.errMessage(err);
    } finally {
      this.loading = false;
    }
  }

  private errMessage(err: unknown): string {
    return err instanceof Error ? err.message : String(err);
  }

  private coworkerName(id: string | null | undefined): string | null {
    if (!id) return null;
    const cw = this.coworkers.find((c) => c.id === id);
    return cw ? cw.name : id.slice(0, 8);
  }

  private checkMeta(id: string): SafetyCheck | null {
    return this.checks.find((c) => c.id === id) ?? null;
  }

  private isPlatform(r: SafetyRule): boolean {
    return r.source === 'platform';
  }

  // ---- dialog flows ----

  private openCreate = (): void => {
    this.editTarget = null;
    this.duplicateSource = null;
    this.dialogOpen = true;
  };

  private openEdit(r: SafetyRule): void {
    if (this.isPlatform(r)) return;
    this.editTarget = r;
    this.duplicateSource = null;
    this.dialogOpen = true;
  }

  private openDuplicate(r: SafetyRule): void {
    this.editTarget = null;
    this.duplicateSource = r;
    this.dialogOpen = true;
  }

  private closeDialog = (): void => {
    this.dialogOpen = false;
    this.editTarget = null;
    this.duplicateSource = null;
  };

  // Edit-dialog "duplicate this rule" link (§6.11.3 — the path to move scope).
  private onDuplicateFromEdit = (e: CustomEvent<{ id: string }>): void => {
    const src = this.rules.find((r) => r.id === e.detail.id) ?? null;
    this.dialogOpen = false;
    this.editTarget = null;
    // Reopen as duplicate on the next frame so the dialog re-seeds cleanly.
    void this.updateComplete.then(() => {
      this.duplicateSource = src;
      this.dialogOpen = true;
    });
  };

  private async onSaved(id: string): Promise<void> {
    await this.refresh();
    this.pulse(id);
  }

  private pulse(id: string): void {
    this.highlightId = id;
    if (this.highlightTimer) clearTimeout(this.highlightTimer);
    void this.updateComplete.then(() => {
      this.querySelector(`[data-rule-id="${id}"]`)?.scrollIntoView({
        block: 'center',
        behavior: 'smooth',
      });
    });
    this.highlightTimer = window.setTimeout(() => {
      this.highlightId = null;
    }, 1800);
  }

  // ---- toggle (optimistic) ----

  private async toggleEnabled(r: SafetyRule): Promise<void> {
    if (this.isPlatform(r) || this.togglingIds.has(r.id)) return;
    const next = !r.enabled;
    this.rules = this.rules.map((x) =>
      x.id === r.id ? { ...x, enabled: next } : x,
    );
    this.togglingIds = new Set(this.togglingIds).add(r.id);
    try {
      await updateRule(r.id, { enabled: next });
    } catch {
      this.rules = this.rules.map((x) =>
        x.id === r.id ? { ...x, enabled: r.enabled } : x,
      );
      this.showToast('Couldn’t update — try again');
    } finally {
      const ids = new Set(this.togglingIds);
      ids.delete(r.id);
      this.togglingIds = ids;
    }
  }

  // ---- delete ----

  private askDelete(r: SafetyRule): void {
    if (this.isPlatform(r)) return;
    this.deleteTarget = r;
  }

  private cancelDelete = (): void => {
    if (this.deleteInFlight) return;
    this.deleteTarget = null;
  };

  private async performDelete(): Promise<void> {
    const r = this.deleteTarget;
    if (!r || this.deleteInFlight) return;
    this.deleteInFlight = true;
    try {
      await deleteRule(r.id);
      this.rules = this.rules.filter((x) => x.id !== r.id);
      this.deleteTarget = null;
    } catch (err) {
      this.deleteTarget = null;
      this.showToast(this.errMessage(err));
    } finally {
      this.deleteInFlight = false;
    }
  }

  // ---- audit drawer ----

  private async openAudit(r: SafetyRule): Promise<void> {
    this.auditTarget = r;
    this.auditEntries = [];
    this.auditLoading = true;
    try {
      const entries = await this.api.listSafetyRuleAudit(r.id);
      // Reverse-chronological (§6.9).
      this.auditEntries = [...entries].sort(
        (a, b) => Date.parse(b.created_at) - Date.parse(a.created_at),
      );
    } catch (err) {
      this.showToast(this.errMessage(err));
    } finally {
      this.auditLoading = false;
    }
  }

  private closeAudit = (): void => {
    this.auditTarget = null;
    this.auditEntries = [];
  };

  // ---- toast ----

  private showToast(msg: string): void {
    this.toast = msg;
    if (this.toastTimer) clearTimeout(this.toastTimer);
    this.toastTimer = window.setTimeout(() => {
      this.toast = null;
    }, 3200);
  }

  // ---- render ----

  override render(): TemplateResult {
    const sorted = sortRules(this.rules);
    const platform = sorted.filter((r) => this.isPlatform(r));
    const org = sorted.filter((r) => !this.isPlatform(r));
    return html`
      <div class="rm-spane">
        <div class="rm-ch">
          <h2>Safety rules</h2>
          <button type="button" class="rm-add" @click=${this.openCreate}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M12 5v14M5 12h14" />
            </svg>
            New rule
          </button>
        </div>
        <p class="rm-sub">
          Automatic guardrails that scan for personal data, prompt injection,
          secrets, or untrusted domains. Unlike approval policies these run with
          no human in the loop — except when a rule's action is set to Approve,
          which routes to the same approval inbox. See past triggers in the
          <a href="#/manage/safety-log" class="rm-saf-link"
            style="color: var(--rm-accent)">Safety log →</a>.
        </p>

        ${this.loading
          ? html`<div class="rm-banner-loading">Loading…</div>`
          : this.listError
            ? html`<div class="rm-banner-err">${this.listError}</div>`
            : this.rules.length === 0
              ? this.renderEmpty()
              : html`
                  ${platform.length ? this.renderPlatformBanner() : nothing}
                  ${platform.map((r) => this.renderCard(r))}
                  ${platform.length && org.length
                    ? html`<div class="rm-saf-section-label">
                        Your organization's rules
                      </div>`
                    : nothing}
                  ${org.map((r) => this.renderCard(r))}
                  ${this.renderHint()}
                `}

        <rm-safety-rule-dialog
          ?open=${this.dialogOpen}
          .editing=${this.editTarget}
          .duplicating=${this.duplicateSource}
          .checks=${this.checks}
          .coworkers=${this.coworkers}
          .rules=${this.rules}
          @close=${this.closeDialog}
          @safety-rule-saved=${(e: CustomEvent<{ id: string }>) =>
            void this.onSaved(e.detail.id)}
          @safety-rule-duplicate-from-edit=${this.onDuplicateFromEdit}
        ></rm-safety-rule-dialog>

        ${this.renderDeleteDialog()}
        ${this.renderAuditDrawer()}

        ${this.toast
          ? html`<div class="rm-toast" role="status" data-testid="saf-toast">
              ${this.toast}
            </div>`
          : nothing}
      </div>
    `;
  }

  private renderPlatformBanner(): TemplateResult {
    return html`
      <div class="rm-saf-platform-banner" data-testid="saf-platform-banner">
        ${iconShieldCheck()}
        <span>
          <b>Platform defaults</b> — these rules apply to every organization and
          can't be edited or disabled here. Contact the platform admin to
          change.
        </span>
      </div>
    `;
  }

  private renderHint(): TemplateResult {
    return html`
      <p class="rm-pol-hint" data-testid="saf-hint">
        Higher-priority rules run first; ties go to the newest. Changes apply to
        new agent tasks — already-running tasks finish with the current rules.
      </p>
    `;
  }

  private renderEmpty(): TemplateResult {
    return html`
      <div class="rm-pol-empty" data-testid="saf-empty">
        <div class="rm-pol-empty-icon" style="color: var(--rm-accent)">
          ${iconShieldCheck(22)}
        </div>
        <p>No safety rules yet.</p>
        <p class="rm-pol-empty-sub">
          Coworkers run with no automatic guardrails. Create your first rule to
          scan for personal data, prompt injection, or untrusted domains.
        </p>
        <button type="button" @click=${this.openCreate}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" stroke-width="2.2" aria-hidden="true">
            <path d="M12 5v14M5 12h14" />
          </svg>
          Create your first rule
        </button>
      </div>
    `;
  }

  private renderCard(r: SafetyRule): TemplateResult {
    const platform = this.isPlatform(r);
    const meta = this.checkMeta(r.check_id);
    const cwName = this.coworkerName(r.coworker_id);
    const dim = r.enabled ? '' : ' rm-card--dim';
    const hi = this.highlightId === r.id ? ' rm-card--highlight' : '';
    const tier = platform ? ' rm-card--platform' : '';
    const action = effectiveAction(
      { check_id: r.check_id, stage: r.stage, config: (r.config ?? {}) as Record<string, unknown> },
      meta,
    );
    const slow = meta?.cost_class === 'slow';
    const toggling = this.togglingIds.has(r.id);
    return html`
      <div
        class="rm-card${dim}${hi}${tier}"
        data-rule-id=${r.id}
        data-testid="saf-row"
      >
        <span
          class="rm-pol-pri ${priorityBadgeClass(r.priority)}"
          data-testid="saf-priority"
        >priority ${r.priority}</span>
        <span class="rm-mn">
          <b>${checkLabel(r.check_id)}</b>
          <span class="rm-pol-sent" data-testid="saf-sentence">
            ${unsafeHTML(
              safSentence(
                { check_id: r.check_id, stage: r.stage, config: (r.config ?? {}) as Record<string, unknown> },
                meta,
                cwName,
              ),
            )}
          </span>
        </span>
        ${slow ? html`<span class="rm-saf-slowchip">slow</span>` : nothing}
        ${cwName ? html`<span class="rm-saf-scope">${cwName}</span>` : nothing}
        ${action
          ? html`<span class="rm-pill ${safActionPillClass(action)}"
              data-testid="saf-action-pill">${action}</span>`
          : nothing}
        ${this.renderToggle(r, platform, toggling)}
        ${this.renderRowActs(r, platform)}
      </div>
    `;
  }

  private renderToggle(
    r: SafetyRule,
    platform: boolean,
    toggling: boolean,
  ): TemplateResult {
    if (platform) {
      // Fixed-on, click is a no-op (§6.2.1).
      return html`<span
        class="rm-pol-toggle rm-pol-toggle--on"
        style="cursor: default; opacity: 0.7"
        title="Platform-tier rules are always enabled"
        data-testid="saf-toggle"
      ><span>Enabled</span><span class="rm-switch"></span></span>`;
    }
    return html`<button
      type="button"
      class="rm-pol-toggle ${r.enabled ? 'rm-pol-toggle--on' : ''}"
      title=${r.enabled ? 'Click to disable' : 'Click to enable'}
      data-testid="saf-toggle"
      ?disabled=${toggling}
      @click=${(e: Event) => {
        e.stopPropagation();
        void this.toggleEnabled(r);
      }}
    ><span>${r.enabled ? 'Enabled' : 'Disabled'}</span><span class="rm-switch"></span></button>`;
  }

  private renderRowActs(r: SafetyRule, platform: boolean): TemplateResult {
    const auditBtn = html`<button
      type="button"
      class="rm-iconbtn"
      title="Change history"
      data-testid="saf-audit"
      @click=${(e: Event) => {
        e.stopPropagation();
        void this.openAudit(r);
      }}
    >${iconClock(15)}</button>`;
    if (platform) {
      // Platform rules: audit only (no edit / duplicate / delete) — §6.2.2.
      return html`<span class="rm-row-acts" style="opacity: 1">${auditBtn}</span>`;
    }
    return html`
      <span class="rm-row-acts">
        <button type="button" class="rm-iconbtn" title="Edit rule"
          data-testid="saf-edit"
          @click=${(e: Event) => {
            e.stopPropagation();
            this.openEdit(r);
          }}
        >${iconPencil(15)}</button>
        <button type="button" class="rm-iconbtn"
          title="Duplicate — useful for moving scope or branching config"
          data-testid="saf-duplicate"
          @click=${(e: Event) => {
            e.stopPropagation();
            this.openDuplicate(r);
          }}
        >${iconCopy(15)}</button>
        ${auditBtn}
        <button type="button" class="rm-iconbtn rm-iconbtn--danger" title="Delete rule"
          data-testid="saf-delete"
          @click=${(e: Event) => {
            e.stopPropagation();
            this.askDelete(r);
          }}
        >${iconTrash(15)}</button>
      </span>
    `;
  }

  private renderDeleteDialog(): TemplateResult {
    const r = this.deleteTarget;
    const meta = r ? this.checkMeta(r.check_id) : null;
    const sentence = r
      ? safSentence(
          { check_id: r.check_id, stage: r.stage, config: (r.config ?? {}) as Record<string, unknown> },
          meta,
          this.coworkerName(r.coworker_id),
        )
      : '';
    return html`
      <rm-confirm-dialog
        ?open=${r !== null}
        title="Delete safety rule?"
        tone="danger"
        confirm-label="Delete rule"
        busy-label="Deleting…"
        ?busy=${this.deleteInFlight}
        @confirm=${() => void this.performDelete()}
        @cancel=${this.cancelDelete}
        @close=${this.cancelDelete}
      >
        ${r
          ? html`<p>
                You're about to delete the rule
                <b>${checkLabel(r.check_id)}</b> —
                ${unsafeHTML(sentence)}
              </p>
              <p>
                After deletion, this check stops running on new agent tasks.
                Tasks already in progress keep using this rule until they
                finish, then move on.
              </p>
              <p>
                Past safety log entries are kept. The change history for this
                rule is also kept — you can review it later if there's ever an
                audit.
              </p>`
          : nothing}
      </rm-confirm-dialog>
    `;
  }

  private renderAuditDrawer(): TemplateResult {
    const r = this.auditTarget;
    return html`
      <rm-dialog
        ?open=${r !== null}
        title="Rule history"
        width="520px"
        @close=${this.closeAudit}
      >
        ${r
          ? html`<p class="text-[12.5px] text-ink-3 dark:text-d-ink-3 mb-3">
              ${checkLabel(r.check_id)}
            </p>`
          : nothing}
        ${this.auditLoading
          ? html`<div class="rm-banner-loading">Loading…</div>`
          : this.auditEntries.length === 0
            ? html`<div class="rm-banner-loading" data-testid="saf-audit-empty">
                No change history yet.
              </div>`
            : html`<div class="rm-saf-audit" data-testid="saf-audit-list">
                ${this.auditEntries.map((e) => this.renderAuditRow(e))}
              </div>`}
        <div slot="footer" style="display: flex; justify-content: flex-end">
          <button type="button" class="rm-btn rm-btn--secondary" @click=${this.closeAudit}>
            Close
          </button>
        </div>
      </rm-dialog>
    `;
  }

  private renderAuditRow(e: SafetyRuleAuditEntry): TemplateResult {
    const verb = e.action.toUpperCase();
    return html`
      <div class="rm-saf-audit-row">
        <div class="rm-saf-ahead">
          <span class="rm-saf-averb rm-saf-verb-${e.action}">${verb}</span>
          <span class="rm-saf-actor"
            >by ${e.actor_user_id ?? 'the system'}</span
          >
          <span class="rm-saf-ats">${new Date(e.created_at).toLocaleString()}</span>
        </div>
        <div class="rm-saf-achange">${auditSummary(e)}</div>
      </div>
    `;
  }
}

// History/clock icon — no iconClock in icons.ts, so inline (matches the
// stroke style of the shared icon set).
function iconClock(size = 15): TemplateResult {
  return html`<svg width=${size} height=${size} viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="1.8" aria-hidden="true">
    <circle cx="12" cy="12" r="10" />
    <path d="M12 6v6l4 2" />
  </svg>`;
}

// Shield-with-check — platform banner + empty-state glyph.
function iconShieldCheck(size = 14): TemplateResult {
  return html`<svg width=${size} height=${size} viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="1.8" aria-hidden="true">
    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    <path d="M9 12l2 2 4-4" />
  </svg>`;
}
