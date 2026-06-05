// <rm-safety-rule-dialog> — create / edit / duplicate a safety rule (spec §6.11).
//
// One dialog backs all three flows (mirrors approval-policy-dialog):
//   - editing non-null     → edit  (PATCH; scope LOCKED — §6.11.3)
//   - duplicating non-null → create, pre-filled (POST; scope editable)
//   - both null            → create, defaults (POST)
//
// THREE EDITOR EXPERIENCES for the action (spec §6.11.1), chosen from the
// wire check's action_model + cfgKind — but the taxonomy name is never shown
// (§8.5). The user sees behaviour, not classification:
//   1. fixed (non host-list): a segmented control with the natural action
//      marked and unsupported / non-overridable actions disabled.
//   2. config_routed (presidio.pii / openai_moderation): NO action field —
//      the per-finding routing table in the config section IS the editor.
//   3. host-list (domain_allowlist / egress.domain_rule): NO action field —
//      the host list is the whole rule ("allow these, block the rest").
// A fully-inert field is removed, not greyed (§8.5 #4).
//
// Writes go through safety-admin-client (admin-privileged per design §3
// Phase 4). On edit we NEVER send coworker_id — scope is immutable for audit
// consistency (SafetyRuleUpdate has no such field; this is the belt-and-
// suspenders backstop).

import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';

import './dialog.js';
import type {
  SafetyCheck,
  SafetyRule,
  SafetyStage,
  SafetyVerdictAction,
} from '../api/client.js';
import {
  createRule,
  updateRule,
  type CoworkerSummary,
} from '../services/safety-admin-client.js';
import {
  SAFETY_CHECK_CATALOG,
  SAF_ACTION_LABEL,
  SAF_ACTION_ORDER,
  SAF_ACTION_SUB,
  SAF_CONTROL_STAGES,
  SAF_STAGE_LABEL,
  actionButtonState,
  naturalAction,
  safSentence,
  supportedActions,
} from './safety-catalog.js';

const INPUT_CLASS =
  'w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3 ' +
  'dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1 ' +
  'text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand';

// Presentation lists for the per-check config forms. Human label first, the
// technical code as a muted subtitle (§8.5: behaviour primary, code for
// debugging). These are UI-only; the backend keys are the codes.

// pii.regex: backend key is `patterns: { SSN: true, ... }` (uppercase, dict of bool).
// Each entry: [backendKey, humanLabel]. Frontend stores as Set<backendKey>.
const PII_REGEX_ENTITIES: [string, string][] = [
  ['SSN', 'US Social Security numbers'],
  ['CREDIT_CARD', 'Credit card numbers'],
  ['EMAIL', 'Email addresses'],
  ['PHONE_US', 'Phone numbers'],
  ['IP_ADDRESS', 'IP addresses'],
];
const PRESIDIO_ENTITIES: [string, string][] = [
  ['EMAIL_ADDRESS', 'Email addresses'],
  ['PHONE_NUMBER', 'Phone numbers'],
  ['US_SSN', 'US Social Security numbers'],
  ['CREDIT_CARD', 'Credit card numbers'],
  ['PERSON', "People's names"],
  ['LOCATION', 'Locations'],
  ['IP_ADDRESS', 'IP addresses'],
  ['DATE_TIME', 'Dates and times'],
];
const MODERATION_CATEGORIES: [string, string][] = [
  ['sexual', 'Sexual content'],
  ['hate', 'Hate speech'],
  ['harassment', 'Harassment'],
  ['self-harm', 'Self-harm'],
  ['violence', 'Violence'],
];
const SECRET_PLUGINS: [string, string][] = [
  ['aws', 'AWS keys'],
  ['github', 'GitHub tokens'],
  ['slack', 'Slack tokens'],
  ['stripe', 'Stripe keys'],
  ['jwt', 'Login tokens (JWT)'],
  ['basic_auth', 'Basic auth credentials'],
  ['private_key', 'Private keys'],
];

@customElement('rm-safety-rule-dialog')
export class SafetyRuleDialog extends LitElement {
  @property({ type: Boolean }) open = false;
  @property({ attribute: false }) editing: SafetyRule | null = null;
  @property({ attribute: false }) duplicating: SafetyRule | null = null;
  /** Check catalog + coworker list — fetched once by the page, passed down. */
  @property({ attribute: false }) checks: SafetyCheck[] = [];
  @property({ attribute: false }) coworkers: CoworkerSummary[] = [];

  @state() private checkId = '';
  @state() private stage: SafetyStage | '' = '';
  /** Override action for fixed checks; null ⇒ use the natural action. */
  @state() private pickedAction: SafetyVerdictAction | null = null;
  @state() private coworkerId: string | null = null;
  @state() private priority = 100;
  @state() private enabled = true;
  /** cfgKind-specific config (entities / routing / threshold / hosts / …). */
  @state() private config: Record<string, unknown> = {};
  /** Raw-JSON escape hatch; null ⇒ visual form is authoritative. */
  @state() private advancedJson: string | null = null;
  @state() private busy = false;
  @state() private err: string | null = null;

  protected override createRenderRoot() {
    return this;
  }

  override willUpdate(changed: Map<string, unknown>) {
    if (changed.has('open') && this.open) {
      this.seedForm();
      this.err = null;
    }
  }

  private get isEdit(): boolean {
    return this.editing !== null;
  }

  private seedSource(): SafetyRule | null {
    return this.editing ?? this.duplicating;
  }

  private currentCheck(): SafetyCheck | null {
    return this.checks.find((c) => c.id === this.checkId) ?? null;
  }

  private seedForm(): void {
    const src = this.seedSource();
    if (src) {
      this.checkId = src.check_id;
      this.stage = src.stage;
      this.coworkerId = src.coworker_id ?? null;
      this.priority = src.priority;
      this.enabled = src.enabled;
      const cfg = { ...(src.config ?? {}) } as Record<string, unknown>;
      const override = cfg['action_override'];
      this.pickedAction =
        typeof override === 'string' ? (override as SafetyVerdictAction) : null;
      delete cfg['action_override'];
      // Convert stored backend format → internal display format per check.
      this._normalizeConfigFromBackend(src.check_id, cfg);
      this.config = cfg;
      this.advancedJson = null;
    } else {
      const first = this.checks[0];
      this.checkId = first?.id ?? '';
      this.stage = (first?.stages?.[0] as SafetyStage) ?? '';
      this.coworkerId = null;
      this.priority = 100;
      this.enabled = true;
      this.pickedAction = null;
      this.config = {};
      this.advancedJson = null;
    }
  }

  private close = () => {
    this.dispatchEvent(new CustomEvent('close', { bubbles: true, composed: true }));
  };

  // ---- check / stage change resets dependent state ----

  private onCheckChange(id: string): void {
    this.checkId = id;
    const check = this.currentCheck();
    // Reset stage to the check's first supported stage, drop any override +
    // config from the previous (different-shaped) check.
    this.stage = (check?.stages?.[0] as SafetyStage) ?? '';
    this.pickedAction = null;
    this.config = {};
    this.advancedJson = null;
  }

  private onStageChange(stage: SafetyStage): void {
    this.stage = stage;
    // An override valid on the old stage may be unsupported on the new one;
    // drop it so we never submit an unsupported action.
    if (this.pickedAction) {
      const check = this.currentCheck();
      const nat = naturalAction(check, stage);
      const st = actionButtonState(check, stage, this.pickedAction, nat);
      if (!st.enabled) this.pickedAction = null;
    }
  }

  // ---- config format converters (backend ↔ internal display) ----

  // Convert backend-stored config → internal UI format on load.
  // Each check uses different field names from what the UI stores internally.
  private _normalizeConfigFromBackend(checkId: string, cfg: Record<string, unknown>): void {
    if (checkId === 'pii.regex') {
      // Backend: { patterns: { SSN: true, CREDIT_CARD: true, ... } }
      // Internal: Set<backendKey> stored as cfg['_piiKeys'] (a string[])
      const patterns = (cfg['patterns'] as Record<string, boolean> | undefined) ?? {};
      cfg['_piiKeys'] = Object.keys(patterns).filter((k) => patterns[k]);
      delete cfg['patterns'];
    } else if (checkId === 'presidio.pii') {
      // Backend: { block_codes: [...], redact_codes: [...], score_threshold: 0.4 }
      // Internal: { routing: { CODE: 'block'|'redact' }, threshold: 0.4 }
      const blockCodes = (cfg['block_codes'] as string[]) ?? [];
      const redactCodes = (cfg['redact_codes'] as string[]) ?? [];
      const routing: Record<string, string> = {};
      for (const c of blockCodes) routing[c] = 'block';
      for (const c of redactCodes) routing[c] = 'redact';
      cfg['routing'] = routing;
      cfg['threshold'] = cfg['score_threshold'] ?? 0.4;
      delete cfg['block_codes'];
      delete cfg['redact_codes'];
      delete cfg['score_threshold'];
      delete cfg['language'];
    } else if (checkId === 'openai_moderation') {
      // Backend: { block_categories: [...], warn_categories: [...] }
      // Internal: { routing: { category: 'block'|'warn' } }
      const blockCats = (cfg['block_categories'] as string[]) ?? [];
      const warnCats = (cfg['warn_categories'] as string[]) ?? [];
      const routing: Record<string, string> = {};
      for (const c of blockCats) routing[c] = 'block';
      for (const c of warnCats) routing[c] = 'warn';
      cfg['routing'] = routing;
      delete cfg['block_categories'];
      delete cfg['warn_categories'];
    }
    // secret_scanner: backend only has action_override (already stripped above).
    // No additional conversion needed.
  }

  // Convert internal UI format → backend config on save.
  private _buildBackendConfig(cfg: Record<string, unknown>): Record<string, unknown> {
    const out = { ...cfg };
    if (this.checkId === 'pii.regex') {
      const keys = (out['_piiKeys'] as string[]) ?? [];
      delete out['_piiKeys'];
      const patterns: Record<string, boolean> = {};
      for (const k of keys) patterns[k] = true;
      out['patterns'] = patterns;
    } else if (this.checkId === 'presidio.pii') {
      const routing = (out['routing'] as Record<string, string>) ?? {};
      delete out['routing'];
      const threshold = out['threshold'];
      delete out['threshold'];
      out['block_codes'] = Object.entries(routing).filter(([, a]) => a === 'block').map(([c]) => c);
      out['redact_codes'] = Object.entries(routing).filter(([, a]) => a === 'redact').map(([c]) => c);
      out['score_threshold'] = threshold ?? 0.4;
    } else if (this.checkId === 'openai_moderation') {
      const routing = (out['routing'] as Record<string, string>) ?? {};
      delete out['routing'];
      out['block_categories'] = Object.entries(routing).filter(([, a]) => a === 'block').map(([c]) => c);
      out['warn_categories'] = Object.entries(routing).filter(([, a]) => a === 'warn').map(([c]) => c);
    }
    // secret_scanner: action_override only — no extra fields.
    return out;
  }

  // ---- editor-experience selection (spec §6.11.1) ----

  private showActionPanel(): boolean {
    const check = this.currentCheck();
    if (!check) return false;
    const cfgKind = SAFETY_CHECK_CATALOG[check.id]?.cfgKind;
    if (cfgKind === 'host-list') return false; // Experience 3
    if (check.action_model === 'config_routed') return false; // Experience 2
    return check.action_model === 'fixed'; // Experience 1
  }

  // ---- config assembly ----

  private buildConfig(): Record<string, unknown> {
    // Advanced JSON wins when visible, non-empty, and parseable (§6.12).
    if (this.advancedJson !== null) {
      const trimmed = this.advancedJson.trim();
      if (trimmed) {
        try {
          const parsed = JSON.parse(trimmed);
          if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
            return parsed as Record<string, unknown>;
          }
        } catch {
          /* fall through to the form */
        }
      }
    }
    let cfg: Record<string, unknown> = { ...this.config };
    // action_override is only written for the fixed picker experience, only
    // when the user picked a non-natural, override-writable action.
    if (this.showActionPanel() && this.pickedAction) {
      const check = this.currentCheck();
      const nat = naturalAction(check, this.stage as SafetyStage);
      if (this.pickedAction !== nat) cfg['action_override'] = this.pickedAction;
    }
    // Convert internal UI format → backend field names before submitting.
    cfg = this._buildBackendConfig(cfg);
    return cfg;
  }

  private async submit(): Promise<void> {
    if (!this.checkId || !this.stage) {
      this.err = 'Pick a check and a stage.';
      return;
    }
    this.busy = true;
    this.err = null;
    try {
      const config = this.buildConfig();
      // createRule / updateRule return the admin-shaped row; the page holds
      // v1-client rows (with source/tier/editable), so we hand back only the
      // id and let the page re-fetch — the two-tier split must come from a
      // fresh authoritative read anyway.
      let savedId: string;
      if (this.isEdit && this.editing) {
        // Scope (coworker_id) intentionally omitted — immutable on edit.
        const saved = await updateRule(this.editing.id, {
          check_id: this.checkId,
          stage: this.stage,
          config,
          priority: this.priority,
          enabled: this.enabled,
        });
        savedId = saved.id;
      } else {
        const saved = await createRule({
          check_id: this.checkId,
          stage: this.stage,
          coworker_id: this.coworkerId,
          config,
          priority: this.priority,
          enabled: this.enabled,
        });
        savedId = saved.id;
      }
      this.dispatchEvent(
        new CustomEvent('safety-rule-saved', {
          detail: { id: savedId },
          bubbles: true,
          composed: true,
        }),
      );
      this.close();
    } catch (err) {
      this.err = err instanceof Error ? err.message : String(err);
    } finally {
      this.busy = false;
    }
  }

  /** Edit-mode scope hint → close edit, reopen as duplicate (the only path to
   *  change scope, §6.11.3). The page owns the flow. */
  private duplicateFromEdit(): void {
    const id = this.editing?.id;
    if (!id) return;
    this.dispatchEvent(
      new CustomEvent('safety-rule-duplicate-from-edit', {
        detail: { id },
        bubbles: true,
        composed: true,
      }),
    );
  }

  // ---- render ----

  private dialogTitle(): string {
    if (this.isEdit) return 'Edit safety rule';
    if (this.duplicating) return 'Duplicate safety rule';
    return 'New safety rule';
  }

  override render(): TemplateResult {
    const check = this.currentCheck();
    const pres = check ? SAFETY_CHECK_CATALOG[check.id] : undefined;
    return html`
      <rm-dialog
        title=${this.dialogTitle()}
        ?open=${this.open}
        ?close-on-backdrop=${!this.busy}
        ?close-on-esc=${!this.busy}
        width="600px"
        @close=${this.close}
      >
        <p class="text-[12.5px] text-ink-2 dark:text-d-ink-2 mb-3">
          Safety rules run automatically — no one in the loop — except when you
          set the action to <b>Approve</b>, which routes to the same approval
          inbox as business policies. Changes apply to new agent tasks;
          already-running tasks finish with the current rules.
        </p>

        ${this.renderCheckField(pres)}
        ${this.renderStageField(check)}
        ${this.showActionPanel() ? this.renderActionPanel(check) : nothing}
        ${this.renderConfigField(check, pres)}
        ${this.renderScopeField()}
        ${this.renderPriorityEnabled()}
        ${this.renderPreview(check)}

        ${this.err
          ? html`<div
              class="text-[12.5px] text-red-600 dark:text-red-300 mt-2"
              role="alert"
              data-testid="saf-form-error"
            >${this.err}</div>`
          : nothing}

        <div slot="footer" style="display: flex; gap: 8px; justify-content: flex-end;">
          <button type="button" class="rm-btn rm-btn--secondary"
            ?disabled=${this.busy} @click=${this.close}>Cancel</button>
          <button type="button" class="rm-btn rm-btn--primary"
            data-testid="saf-submit" ?disabled=${this.busy}
            @click=${() => void this.submit()}
          >${this.busy ? 'Saving…' : this.isEdit ? 'Save changes' : 'Create rule'}</button>
        </div>
      </rm-dialog>
    `;
  }

  private renderCheckField(
    pres: (typeof SAFETY_CHECK_CATALOG)[string] | undefined,
  ): TemplateResult {
    // Group the check <select> by category (Sensitive data / … / Network),
    // each option suffixed with its cost so the user sees the latency tradeoff.
    const byCategory = new Map<string, SafetyCheck[]>();
    for (const c of this.checks) {
      const cat = SAFETY_CHECK_CATALOG[c.id]?.category ?? 'Other';
      const list = byCategory.get(cat) ?? [];
      list.push(c);
      byCategory.set(cat, list);
    }
    const check = this.currentCheck();
    const slow = check?.cost_class === 'slow';
    return html`
      <div class="mb-2">
        <label class="block text-[12.5px] font-medium mb-1">Check</label>
        <select
          class=${INPUT_CLASS}
          data-testid="saf-check"
          ?disabled=${this.busy}
          @change=${(e: Event) =>
            this.onCheckChange((e.target as HTMLSelectElement).value)}
        >
          ${[...byCategory.entries()].map(
            ([cat, list]) => html`<optgroup label=${cat}>
              ${list.map(
                (c) => html`<option value=${c.id} ?selected=${c.id === this.checkId}>
                  ${SAFETY_CHECK_CATALOG[c.id]?.label ?? c.id}
                  (${c.cost_class})
                </option>`,
              )}
            </optgroup>`,
          )}
        </select>
        ${pres
          ? html`<p class="text-[11.5px] text-ink-3 dark:text-d-ink-3 mt-1 leading-snug">
              ${pres.desc}
            </p>`
          : nothing}
        ${slow
          ? html`<p class="text-[11.5px] text-amber-700 dark:text-amber-400 mt-1"
              data-testid="saf-slow-warn">
              This check adds noticeable latency — avoid putting it on a
              fast-path stage like “before tool calls”.
            </p>`
          : nothing}
      </div>
    `;
  }

  private renderStageField(check: SafetyCheck | null): TemplateResult {
    const stages = (check?.stages ?? []) as SafetyStage[];
    const control = this.stage && SAF_CONTROL_STAGES.has(this.stage);
    return html`
      <div class="mb-2">
        <label class="block text-[12.5px] font-medium mb-1">Where it runs</label>
        <select
          class=${INPUT_CLASS}
          data-testid="saf-stage"
          ?disabled=${this.busy}
          @change=${(e: Event) =>
            this.onStageChange((e.target as HTMLSelectElement).value as SafetyStage)}
        >
          ${stages.map(
            (s) => html`<option value=${s} ?selected=${s === this.stage}>
              ${SAF_STAGE_LABEL[s] ?? s}
            </option>`,
          )}
        </select>
        ${this.stage
          ? html`<p class="text-[11.5px] text-ink-3 dark:text-d-ink-3 mt-1 leading-snug">
              ${control
                ? 'If this check errors here, the call is blocked by default.'
                : 'If this check errors here, the call is let through.'}
            </p>`
          : nothing}
      </div>
    `;
  }

  private renderActionPanel(check: SafetyCheck | null): TemplateResult {
    const stage = this.stage as SafetyStage;
    const natural = naturalAction(check, stage);
    const current = this.pickedAction ?? natural;
    const naturalLabel = natural ? SAF_ACTION_LABEL[natural].toLowerCase() : 'act on';
    return html`
      <div class="mb-2" data-testid="saf-action-field">
        <label class="block text-[12.5px] font-medium mb-1">When it triggers, do this</label>
        <div class="rm-saf-action-panel">
          By default, this check
          <span class="rm-saf-natural rm-saf-act-${natural ?? 'block'}">${naturalLabel}s</span>
          anything it finds. You can change the action below.
        </div>
        <div class="rm-saf-seg" role="group" data-testid="saf-action-seg">
          ${SAF_ACTION_ORDER.map((a) => {
            const st = actionButtonState(check, stage, a, natural);
            const on = a === current && st.enabled;
            return html`<button
              type="button"
              class="${on ? `rm-saf-on rm-saf-act-${a}` : ''}"
              data-action=${a}
              ?disabled=${!st.enabled || this.busy}
              data-reason=${st.enabled ? nothing : st.reason}
              @click=${() => {
                this.pickedAction = a === natural ? null : a;
              }}
            >
              ${SAF_ACTION_LABEL[a]}
              <div class="rm-saf-sub">${SAF_ACTION_SUB[a]}</div>
              ${a === natural
                ? html`<span class="rm-saf-nat-dot" title="Default for this check"></span>`
                : nothing}
            </button>`;
          })}
        </div>
        <p class="rm-saf-seg-legend">
          <span class="rm-saf-nat-dot"></span> = the default. Pick something else
          to override.
        </p>
      </div>
    `;
  }

  // ---- config forms (one per cfgKind, spec §6.12) ----

  private renderConfigField(
    check: SafetyCheck | null,
    pres: (typeof SAFETY_CHECK_CATALOG)[string] | undefined,
  ): TemplateResult {
    if (!check || !pres?.cfgKind) return html``;
    const adv = this.advancedJson !== null;
    return html`
      <div class="mb-3" data-testid="saf-config">
        ${adv ? this.renderAdvancedJson() : this.renderCfgForm(check, pres.cfgKind)}
        <button
          type="button"
          class="text-[11.5px] text-accent mt-2"
          style="color: var(--rm-accent); background: none; border: none; cursor: pointer; padding: 0;"
          data-testid="saf-adv-toggle"
          @click=${() => this.toggleAdvanced()}
        >
          ${adv ? 'Use the visual form' : 'Advanced: edit as JSON'}
        </button>
      </div>
    `;
  }

  private toggleAdvanced(): void {
    if (this.advancedJson === null) {
      this.advancedJson = JSON.stringify(this.buildConfig(), null, 2);
    } else {
      this.advancedJson = null;
    }
  }

  private renderAdvancedJson(): TemplateResult {
    return html`
      <label class="block text-[12.5px] font-medium mb-1">Configuration (JSON)</label>
      <textarea
        class="${INPUT_CLASS} font-mono text-[12px]"
        rows="6"
        data-testid="saf-config-json"
        .value=${this.advancedJson ?? ''}
        @input=${(e: Event) => {
          this.advancedJson = (e.target as HTMLTextAreaElement).value;
        }}
      ></textarea>
    `;
  }

  private renderCfgForm(check: SafetyCheck, cfgKind: string): TemplateResult {
    switch (cfgKind) {
      case 'pii-entities':
        // Internally stored as _piiKeys (Set<backendKey>); converted to
        // { patterns: { SSN: true, ... } } on save (see _buildBackendConfig).
        return this.renderPiiEntityGrid();
      case 'secret-plugins':
        // Backend SecretScannerConfig has action_override only — no plugin
        // selection. Show an info note instead of a broken checkbox grid.
        return this.renderSecretScannerNote();
      case 'threshold':
        return this.renderThreshold('How sensitive should the check be?', 0.7);
      case 'host-list':
        return this.renderHostList();
      case 'presidio-routing':
        // Stored internally as { routing: {CODE→action}, threshold } and
        // converted to { block_codes, redact_codes, score_threshold } on save.
        return this.renderRouting(check, 'type', PRESIDIO_ENTITIES, true);
      case 'moderation-routing':
        // Stored internally as { routing: {cat→action} } and converted to
        // { block_categories, warn_categories } on save. Only block/warn
        // are expressible per-category (no require_approval_categories field).
        return this.renderModerationRouting(check);
      default:
        return html``;
    }
  }

  private renderPiiEntityGrid(): TemplateResult {
    // _piiKeys holds the Set of selected backend keys (SSN, CREDIT_CARD, …).
    const selected = new Set((this.config['_piiKeys'] as string[]) ?? []);
    return html`
      <label class="block text-[12.5px] font-medium mb-1">What to look for</label>
      <div class="rm-saf-cfg-checks">
        ${PII_REGEX_ENTITIES.map(
          ([backendKey, lbl]) => html`<label>
            <input
              type="checkbox"
              ?checked=${selected.has(backendKey)}
              @change=${(e: Event) => {
                const next = new Set(selected);
                if ((e.target as HTMLInputElement).checked) next.add(backendKey);
                else next.delete(backendKey);
                this.config = { ...this.config, _piiKeys: [...next] };
              }}
            />
            ${lbl}
          </label>`,
        )}
      </div>
    `;
  }

  private renderSecretScannerNote(): TemplateResult {
    // SecretScannerConfig only accepts action_override — there is no plugin
    // selection in the current backend schema. The check runs all built-in
    // detectors automatically.
    return html`
      <p class="text-[12.5px] text-ink-3 dark:text-d-ink-3 leading-snug">
        This check runs all built-in secret detectors automatically. No
        additional configuration is needed — use the action picker above to
        choose what happens when a secret is found.
      </p>
    `;
  }

  // Moderation routing: only block/warn per category — the schema has no
  // require_approval_categories field, so we filter it from the options.
  private renderModerationRouting(check: SafetyCheck): TemplateResult {
    const stage = this.stage as SafetyStage;
    const routing = (this.config['routing'] as Record<string, string>) ?? {};
    // Only block and warn are expressible per-category.
    const options = supportedActions(check, stage).filter(
      (a) => a !== 'allow' && a !== 'require_approval' && a !== 'redact',
    );
    const anyRouted = Object.values(routing).some(Boolean);
    return html`
      <label class="block text-[12.5px] font-medium mb-1">
        Choose an action for each category
        <span class="rm-saf-lblnote">leave any blank to let it through</span>
      </label>
      <div class="rm-saf-routing-table" data-testid="saf-routing">
        ${MODERATION_CATEGORIES.map(
          ([code, lbl]) => html`<div class="rm-saf-routing-row">
            <div>
              <div class="rm-saf-rcode">${lbl}</div>
              <div class="rm-saf-rdesc">${code}</div>
            </div>
            <select
              data-routing-code=${code}
              @change=${(e: Event) => {
                const v = (e.target as HTMLSelectElement).value;
                const next = { ...routing };
                if (v) next[code] = v;
                else delete next[code];
                this.config = { ...this.config, routing: next };
              }}
            >
              <option value="" ?selected=${!routing[code]}>— allow these —</option>
              ${options.map(
                (a) => html`<option value=${a} ?selected=${routing[code] === a}>
                  ${SAF_ACTION_LABEL[a]}
                </option>`,
              )}
            </select>
          </div>`,
        )}
      </div>
      ${!anyRouted
        ? html`<div class="rm-saf-routing-inert" data-testid="saf-routing-inert">
            <b>Nothing set yet.</b> The check runs on every input but won't do
            anything. Pick an action for at least one category above to make it
            active.
          </div>`
        : nothing}
    `;
  }

  private renderThreshold(label: string, fallback: number): TemplateResult {
    // Internally stored as `threshold`; presidio uses `score_threshold` on
    // the backend — converted in _buildBackendConfig.
    const value = (this.config['threshold'] as number) ?? fallback;
    return html`
      <label class="block text-[12.5px] font-medium mb-1">${label}</label>
      <div class="rm-saf-threshold">
        <span class="text-[12px] text-ink-3 dark:text-d-ink-3" style="min-width:80px">Sensitivity</span>
        <input
          type="range" min="0" max="1" step="0.05"
          data-testid="saf-threshold"
          .value=${String(value)}
          @input=${(e: Event) => {
            this.config = {
              ...this.config,
              threshold: parseFloat((e.target as HTMLInputElement).value),
            };
          }}
        />
        <span class="rm-saf-tval">${value}</span>
      </div>
      <div class="rm-saf-thint"><span>stricter (catches more)</span><span>looser (catches less)</span></div>
    `;
  }

  private renderHostList(): TemplateResult {
    // domain_allowlist backend config key is `allowed_hosts` (DomainAllowlistConfig),
    // not `hosts`. This was the 400 "extra_forbidden / missing allowed_hosts" bug.
    const hosts = ((this.config['allowed_hosts'] as string[]) ?? []).join('\n');
    return html`
      <label class="block text-[12.5px] font-medium mb-1">
        Allowed hosts
        <span class="rm-saf-lblnote">one per line · wildcards like *.stripe.com</span>
      </label>
      <textarea
        class="${INPUT_CLASS} font-mono text-[12.5px]"
        style="min-height:80px"
        data-testid="saf-hosts"
        placeholder="api.stripe.com&#10;*.internal.acme.com"
        .value=${hosts}
        @input=${(e: Event) => {
          const lines = (e.target as HTMLTextAreaElement).value
            .split(/\s+/)
            .map((s) => s.trim())
            .filter(Boolean);
          this.config = { ...this.config, allowed_hosts: lines };
        }}
      ></textarea>
      <p class="text-[11.5px] text-ink-3 dark:text-d-ink-3 mt-2 leading-snug">
        The coworker can only reach these hosts. Any other outbound request is
        blocked.
      </p>
    `;
  }

  // Per-finding routing table (Experience 2). Each row: human label + code +
  // a dropdown of supportable actions (server-driven, minus allow — the empty
  // "— allow these —" option is the allow case). presidio also has a threshold.
  private renderRouting(
    check: SafetyCheck,
    noun: 'type' | 'category',
    rows: [string, string][],
    withThreshold: boolean,
  ): TemplateResult {
    const stage = this.stage as SafetyStage;
    const routing = (this.config['routing'] as Record<string, string>) ?? {};
    const options = supportedActions(check, stage).filter((a) => a !== 'allow');
    const anyRouted = Object.values(routing).some(Boolean);
    return html`
      <label class="block text-[12.5px] font-medium mb-1">
        Choose an action for each ${noun}
        <span class="rm-saf-lblnote">leave any blank to let it through</span>
      </label>
      <div class="rm-saf-routing-table" data-testid="saf-routing">
        ${rows.map(
          ([code, lbl]) => html`<div class="rm-saf-routing-row">
            <div>
              <div class="rm-saf-rcode">${lbl}</div>
              <div class="rm-saf-rdesc">${code}</div>
            </div>
            <select
              data-routing-code=${code}
              @change=${(e: Event) => {
                const v = (e.target as HTMLSelectElement).value;
                const next = { ...routing };
                if (v) next[code] = v;
                else delete next[code];
                this.config = { ...this.config, routing: next };
              }}
            >
              <option value="" ?selected=${!routing[code]}>— allow these —</option>
              ${options.map(
                (a) => html`<option value=${a} ?selected=${routing[code] === a}>
                  ${SAF_ACTION_LABEL[a]}
                </option>`,
              )}
            </select>
          </div>`,
        )}
      </div>
      ${!anyRouted
        ? html`<div class="rm-saf-routing-inert" data-testid="saf-routing-inert">
            <b>Nothing set yet.</b> The check runs on every input but won't do
            anything. Pick an action for at least one ${noun} above to make it
            active.
          </div>`
        : nothing}
      ${withThreshold ? this.renderThreshold('Confidence', 0.6) : nothing}
    `;
  }

  private renderScopeField(): TemplateResult {
    const locked = this.isEdit;
    return html`
      <div class="mb-2">
        <label class="block text-[12.5px] font-medium mb-1">Applies to</label>
        <select
          class=${INPUT_CLASS}
          data-testid="saf-scope"
          ?disabled=${locked || this.busy}
          style=${locked ? 'opacity:0.55;cursor:not-allowed' : ''}
          @change=${(e: Event) => {
            const v = (e.target as HTMLSelectElement).value;
            this.coworkerId = v === '' ? null : v;
          }}
        >
          <option value="" ?selected=${!this.coworkerId}>All coworkers</option>
          ${this.coworkers.map(
            (c) => html`<option value=${c.id} ?selected=${c.id === this.coworkerId}>
              ${c.name}
            </option>`,
          )}
        </select>
        ${locked
          ? html`<div class="rm-saf-scope-locked" data-testid="saf-scope-locked">
              <span>🔒</span>
              <span>Scope is fixed after creation (for audit consistency). To
                move scope,
                <a @click=${() => this.duplicateFromEdit()}>duplicate this rule</a>
                with the new scope, then delete this one.</span>
            </div>`
          : nothing}
      </div>
    `;
  }

  private renderPriorityEnabled(): TemplateResult {
    return html`
      <div class="mb-2 flex items-end gap-4">
        <label class="text-[12.5px] font-medium">
          Priority
          <span class="text-ink-3 dark:text-d-ink-3 font-normal">higher wins on ties</span>
          <input
            type="number"
            class="${INPUT_CLASS} w-24 mt-1 block"
            data-testid="saf-priority"
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
            data-testid="saf-enabled"
            aria-pressed=${this.enabled}
            @click=${() => {
              this.enabled = !this.enabled;
            }}
            ?disabled=${this.busy}
          >
            <span>${this.enabled ? 'Enabled' : 'Disabled'}</span>
            <span class="rm-switch"></span>
          </button>
        </div>
      </div>
    `;
  }

  private renderPreview(check: SafetyCheck | null): TemplateResult {
    if (!this.checkId || !this.stage) return html``;
    const cwName = this.coworkerId
      ? (this.coworkers.find((c) => c.id === this.coworkerId)?.name ?? null)
      : null;
    const sentence = safSentence(
      { check_id: this.checkId, stage: this.stage, config: this.buildConfig() },
      check,
      cwName,
    );
    const disabledTail = this.enabled
      ? ''
      : ' <i>(disabled — won\'t run until re-enabled)</i>';
    return html`
      <div class="rm-pol-preview" data-testid="saf-preview">
        ${unsafeHTML(`${sentence} Priority <b>${this.priority}</b>.${disabledTail}`)}
      </div>
    `;
  }
}
