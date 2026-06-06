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

import Ajv, { type ValidateFunction } from 'ajv';
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

// G4: module-level Ajv instance + compiled-validator cache (§6.12.3).
// Compiled once per config_schema, reused across dialog opens.
const _ajv = new Ajv({ allErrors: true, strict: false });
const _schemaValidators = new Map<string, ValidateFunction>();

/** Compile (or return cached) an Ajv validator for a check's config_schema.
 *  Returns null when config_schema is absent or null (defensive §6, hard
 *  constraint #6). */
function _getSchemaValidator(check: SafetyCheck): ValidateFunction | null {
  const schema = check.config_schema as Record<string, unknown> | null | undefined;
  if (!schema || typeof schema !== 'object') return null;
  let v = _schemaValidators.get(check.id);
  if (!v) {
    v = _ajv.compile(schema);
    _schemaValidators.set(check.id, v);
  }
  return v;
}

/** Translate a FastAPI 4xx detail array entry into a user-friendly message
 *  (§6.18 Layer 2). The `loc` path is flattened to a dot-string for display. */
function _translateFastApiError(err: {
  type?: string;
  loc?: unknown[];
  msg?: string;
}): string {
  const loc = Array.isArray(err.loc)
    ? err.loc.filter((s) => s !== 'body').join('.')
    : '';
  const field = loc ? `(field: ${loc})` : '';
  switch (err.type) {
    case 'extra_forbidden':
      return `Unknown field${loc ? ` '${loc}'` : ''} — this check doesn't accept that setting.`;
    case 'missing':
      return `Required field${loc ? ` '${loc}'` : ''} is missing.`;
    case 'int_parsing':
    case 'float_parsing':
      return `Field${loc ? ` '${loc}'` : ''} must be a number.`;
    case 'enum':
      return `Field${loc ? ` '${loc}'` : ''} has an invalid value. ${err.msg ?? ''} ${field}`.trim();
    default:
      return err.msg
        ? `${err.msg} ${field}`.trim()
        : `The server rejected this field ${field}.`.trim();
  }
}

// G7 — schema-driven enum rendering (spec §6.12.5).
// Human-readable labels for known enum values. Unknown values fall through to
// raw-value display (reverse-drift property: backend adds new enum → frontend
// renders it immediately with the raw code as fallback label).
const ENUM_LABELS: Record<string, string> = {
  // pii.regex pattern keys (uppercase)
  SSN: 'US Social Security numbers',
  CREDIT_CARD: 'Credit card numbers',
  EMAIL: 'Email addresses',
  PHONE_US: 'Phone numbers (US)',
  IP_ADDRESS: 'IP addresses',
  // presidio.pii entity codes (PII.* prefix not used in current backend enum)
  EMAIL_ADDRESS: 'Email addresses',
  PHONE_NUMBER: 'Phone numbers',
  US_SSN: 'US Social Security numbers',
  PERSON: "People's names",
  LOCATION: 'Locations',
  DATE_TIME: 'Dates and times',
  // openai_moderation categories (lowercase codes from backend enum)
  sexual: 'Sexual content',
  hate: 'Hate speech',
  harassment: 'Harassment',
  'self-harm': 'Self-harm',
  violence: 'Violence',
};

export function enumLabel(v: string): string {
  return ENUM_LABELS[v] ?? v;
}

/** Read enum values from a check's config_schema field.
 *  `kind` is 'items' for array-item enums (presidio codes, moderation
 *  categories) or 'propertyNames' for dict-key enums (pii.regex patterns).
 *  Returns [] when schema or the field is absent — caller falls back to the
 *  hardcoded ENUM_LABELS keys (defensive §6, hard constraint #6). */
export function getSchemaEnum(
  check: SafetyCheck | null,
  fieldPath: string,
  kind: 'items' | 'propertyNames',
): string[] {
  const schema = check?.config_schema as Record<string, unknown> | null | undefined;
  if (!schema || typeof schema !== 'object') return [];
  const props = (schema['properties'] as Record<string, unknown> | undefined) ?? {};
  const field = props[fieldPath] as Record<string, unknown> | undefined;
  if (!field) return [];
  const node = (field[kind] as Record<string, unknown> | undefined) ?? {};
  return Array.isArray(node['enum']) ? (node['enum'] as string[]) : [];
}

// Fallback entity lists for when config_schema is absent (pre-PR-#58 deploys
// or null schema). Keeps the form functional even without schema data.
const PII_REGEX_FALLBACK: string[] = ['SSN', 'CREDIT_CARD', 'EMAIL', 'PHONE_US', 'IP_ADDRESS'];
const PRESIDIO_FALLBACK: string[] = [
  'EMAIL_ADDRESS', 'PHONE_NUMBER', 'US_SSN', 'CREDIT_CARD',
  'PERSON', 'LOCATION', 'IP_ADDRESS', 'DATE_TIME',
];
const MODERATION_FALLBACK: string[] = ['sexual', 'hate', 'harassment', 'self-harm', 'violence'];

@customElement('rm-safety-rule-dialog')
export class SafetyRuleDialog extends LitElement {
  @property({ type: Boolean }) open = false;
  @property({ attribute: false }) editing: SafetyRule | null = null;
  @property({ attribute: false }) duplicating: SafetyRule | null = null;
  /** Check catalog + coworker list — fetched once by the page, passed down. */
  @property({ attribute: false }) checks: SafetyCheck[] = [];
  @property({ attribute: false }) coworkers: CoworkerSummary[] = [];
  /** All existing rules — used for duplicate-detection (G3, spec §6.10a). */
  @property({ attribute: false }) rules: SafetyRule[] = [];

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
  // --- G3: duplicate-detection state (spec §6.10a) ---
  /** Org-tier rule matching the current (check, scope, stage) triple when in
   *  create/duplicate mode. When set, the form auto-flips to edit that rule. */
  @state() private _dupTarget: SafetyRule | null = null;
  /** True after the user clicked "Create a separate rule anyway" — bypasses
   *  auto-flip and forces a POST. Resets when dialog closes. */
  @state() private _forceCreate = false;
  /** Platform-tier rule covering the same triple; shows the gray FYI banner
   *  but does NOT flip to edit (user can't edit platform rules). */
  @state() private _platformOverlap: SafetyRule | null = null;
  // --- G4: client-side validation errors (§6.12.3 / §6.18) ---
  @state() private _saveErrors: { fieldId?: string; message: string }[] = [];

  protected override createRenderRoot() {
    return this;
  }

  override willUpdate(changed: Map<string, unknown>) {
    if (changed.has('open') && this.open) {
      this._dupTarget = null;
      this._forceCreate = false;
      this._platformOverlap = null;
      this._saveErrors = [];
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
    // G3: run detection after all form fields are set (skips in edit mode).
    this._checkDuplicateRule();
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
    this._dupTarget = null; // triple changed — reset before re-detecting
    this._platformOverlap = null;
    this._checkDuplicateRule();
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
    this._dupTarget = null;
    this._platformOverlap = null;
    this._checkDuplicateRule();
  }

  private onScopeChange(v: string): void {
    this.coworkerId = v === '' ? null : v;
    this._dupTarget = null;
    this._platformOverlap = null;
    this._checkDuplicateRule();
  }

  // ---- G3: duplicate-rule detection (spec §6.10a) ----

  // Detect whether the current (check_id, coworker_id, stage) triple matches an
  // existing rule. Skipped in edit mode, when _forceCreate is already true, or
  // when check/stage are not yet set. On a match the form auto-flips to edit
  // the existing rule (or shows a gray FYI for platform-tier overlaps).
  private _checkDuplicateRule(): void {
    if (this.isEdit) return;         // real edit — user picked this rule explicitly
    if (this._forceCreate) return;   // user opted out of auto-flip
    if (!this.checkId || !this.stage) return;

    const matchesTriple = (r: SafetyRule) =>
      r.check_id === this.checkId &&
      r.stage === this.stage &&
      (r.coworker_id ?? null) === this.coworkerId;

    // Org-tier collision: an editable rule with the same triple (excluding the
    // duplicating source so we don't detect a rule against itself).
    const orgMatch = this.rules.find(
      (r) =>
        r.source === 'tenant' &&
        matchesTriple(r) &&
        r.id !== this.duplicating?.id,
    );

    if (orgMatch) {
      this._dupTarget = orgMatch;
      this._platformOverlap = null;
      // Pre-load existing rule's config/priority/enabled — user is editing it.
      const cfg = { ...(orgMatch.config ?? {}) } as Record<string, unknown>;
      const override = cfg['action_override'];
      this.pickedAction =
        typeof override === 'string' ? (override as SafetyVerdictAction) : null;
      delete cfg['action_override'];
      this._normalizeConfigFromBackend(orgMatch.check_id, cfg);
      this.config = cfg;
      this.priority = orgMatch.priority;
      this.enabled = orgMatch.enabled;
      this.coworkerId = orgMatch.coworker_id ?? null;
      return;
    }

    this._dupTarget = null;

    // Platform-tier overlap: show FYI banner, do NOT flip to edit.
    const platformMatch = this.rules.find(
      (r) => r.source === 'platform' && matchesTriple(r),
    );
    this._platformOverlap = platformMatch ?? null;
  }

  // "Create a separate rule anyway" — lock in forced-create mode.
  private _onForceCreate(): void {
    this._forceCreate = true;
    this._dupTarget = null;
    this._platformOverlap = null;
    // Reset to defaults; keep check/stage the user selected.
    this.config = {};
    this.pickedAction = null;
    this.priority = 100;
    this.enabled = true;
    this.coworkerId = null; // unlock scope
  }

  // "Switch back to editing the existing one" — re-run detection.
  private _onSwitchBackToEdit(): void {
    this._forceCreate = false;
    this._checkDuplicateRule();
  }

  // Banner rendered above the check field when dup detection fires (§6.10a).
  private _renderDupBanner(): TemplateResult | typeof nothing {
    if (this._dupTarget && !this._forceCreate) {
      const label = this._dupTarget.check_id;
      const stageLbl = SAF_STAGE_LABEL[this._dupTarget.stage as SafetyStage] ?? this._dupTarget.stage;
      const scope = this._dupTarget.coworker_id
        ? (this.coworkers.find((c) => c.id === this._dupTarget!.coworker_id)?.name ?? this._dupTarget.coworker_id)
        : 'all coworkers';
      return html`
        <div class="rm-dup-banner rm-dup-banner--info" data-testid="saf-dup-banner-info">
          <span>ℹ</span>
          <span>
            You already have a <b>${label}</b> rule for <b>${stageLbl}</b> on
            <b>${scope}</b>. You're editing that rule now (not creating a new one).
            <button
              type="button"
              class="rm-dup-link"
              data-testid="saf-dup-force-create"
              @click=${() => this._onForceCreate()}
            >Create a separate rule anyway</button>
          </span>
        </div>
      `;
    }
    if (this._forceCreate) {
      return html`
        <div class="rm-dup-banner rm-dup-banner--warn" data-testid="saf-dup-banner-warn">
          <span>⚠</span>
          <span>
            Creating a second rule for the same surface — they may conflict.
            <button
              type="button"
              class="rm-dup-link"
              data-testid="saf-dup-switch-back"
              @click=${() => this._onSwitchBackToEdit()}
            >Switch back to editing the existing one</button>
          </span>
        </div>
      `;
    }
    if (this._platformOverlap) {
      const label = this._platformOverlap.check_id;
      return html`
        <div class="rm-dup-banner rm-dup-banner--fyi" data-testid="saf-dup-banner-fyi">
          <span>🛡</span>
          <span>
            A platform default <b>${label}</b> already covers this surface — your
            org rule will run alongside it.
          </span>
        </div>
      `;
    }
    return nothing;
  }

  // ---- G4: client-side schema validation (§6.12.3 / §6.18) ----

  // Layer 1a — Ajv JSON Schema validation against config_schema.
  // Returns array of {fieldId?, message} errors; empty = valid.
  private _validateWithSchema(config: Record<string, unknown>): { fieldId?: string; message: string }[] {
    const check = this.currentCheck();
    if (!check) return [];
    const validate = _getSchemaValidator(check);
    if (!validate) return [];
    if (validate(config)) return [];
    return (validate.errors ?? []).map((e) => {
      const path = e.instancePath?.replace(/^\//, '') ?? '';
      return {
        fieldId: path || undefined,
        message: e.message ?? 'Invalid value',
      };
    });
  }

  // Layer 1b — hand-coded sanity checks for constraints JSON Schema can't express.
  // Only runs when config_schema is present: without a schema the backend hasn't
  // declared expected config shape, so we can't know whether {} is intentional.
  private _sanityCheck(config: Record<string, unknown>): { fieldId?: string; message: string }[] {
    const check = this.currentCheck();
    if (!check?.config_schema) return []; // no schema → skip (Ajv already skipped too)
    const errs: { fieldId?: string; message: string }[] = [];
    if (this.checkId === 'pii.regex') {
      const patterns = config['patterns'] as Record<string, boolean> | undefined;
      if (!patterns || Object.keys(patterns).length === 0) {
        errs.push({ fieldId: 'saf-config', message: 'Pick at least one type of personal data to look for.' });
      }
    } else if (this.checkId === 'presidio.pii') {
      const bc = (config['block_codes'] as string[]) ?? [];
      const rc = (config['redact_codes'] as string[]) ?? [];
      if (bc.length === 0 && rc.length === 0) {
        errs.push({ fieldId: 'saf-config', message: 'Set an action for at least one entity type.' });
      }
    } else if (this.checkId === 'openai_moderation') {
      const bl = (config['block_categories'] as string[]) ?? [];
      const wl = (config['warn_categories'] as string[]) ?? [];
      if (bl.length === 0 && wl.length === 0) {
        errs.push({ fieldId: 'saf-config', message: 'Set an action for at least one category.' });
      }
    } else if (this.checkId === 'domain_allowlist') {
      const hosts = (config['allowed_hosts'] as string[]) ?? [];
      if (hosts.length === 0) {
        errs.push({ fieldId: 'saf-hosts', message: 'Add at least one host.' });
      }
    } else if (this.checkId === 'egress.domain_rule') {
      const patterns = (config['domain_patterns'] as string[]) ?? [];
      if (patterns.length === 0) {
        errs.push({ fieldId: 'saf-hosts', message: 'Add at least one domain pattern.' });
      }
    }
    return errs;
  }

  // Combined pre-save validation (Layer 1). Returns errors array.
  private _validateBeforeSave(config: Record<string, unknown>): { fieldId?: string; message: string }[] {
    return [...this._validateWithSchema(config), ...this._sanityCheck(config)];
  }

  // Parse a FastAPI 4xx { detail: [...] } body and return friendly messages
  // (§6.18 Layer 2). Falls back gracefully if the shape is unexpected.
  static _parseBackend400(body: unknown): { fieldId?: string; message: string }[] {
    if (!body || typeof body !== 'object') return [];
    const detail = (body as Record<string, unknown>)['detail'];
    if (!Array.isArray(detail)) return [];
    return detail
      .filter((e): e is Record<string, unknown> => e && typeof e === 'object')
      .map((e) => ({
        fieldId: undefined,
        message: _translateFastApiError({
          type: e['type'] as string | undefined,
          loc: e['loc'] as unknown[] | undefined,
          msg: e['msg'] as string | undefined,
        }),
      }));
  }

  // Render inline error message next to an input (fieldId matches testid).
  private _fieldError(fieldId: string): TemplateResult | typeof nothing {
    const err = this._saveErrors.find((e) => e.fieldId === fieldId);
    if (!err) return nothing;
    return html`<p class="rm-field-error" data-testid="saf-err-${fieldId}">${err.message}</p>`;
  }

  // Render summary error banner at dialog bottom (§6.18).
  private _renderErrorBanner(): TemplateResult | typeof nothing {
    if (this._saveErrors.length === 0) return nothing;
    const count = this._saveErrors.length;
    return html`
      <div class="rm-save-error-banner" data-testid="saf-error-banner">
        <b>${count === 1 ? 'Fix this before saving' : 'Fix these before saving'}</b>
        <ul>${this._saveErrors.map((e) => html`<li>${e.message}</li>`)}</ul>
      </div>
    `;
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
    // G4: Layer 1 — client-side validation before any network call.
    const config = this.buildConfig();
    const errs = this._validateBeforeSave(config);
    if (errs.length > 0) {
      this._saveErrors = errs;
      return;
    }
    this._saveErrors = [];
    this.busy = true;
    this.err = null;
    try {
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
      } else if (this._dupTarget && !this._forceCreate) {
        // G3: auto-flipped to edit existing rule — PATCH that rule, not POST.
        // Scope omitted (immutable on edit, same as real edit mode).
        const saved = await updateRule(this._dupTarget.id, {
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
      // G4: Layer 2 — try to parse FastAPI { detail: [...] } from the error.
      const raw = err instanceof Error ? err.message : String(err);
      let parsed: unknown;
      try { parsed = JSON.parse(raw); } catch { /* not JSON */ }
      if (parsed) {
        const backend400 = SafetyRuleDialog._parseBackend400(parsed);
        if (backend400.length > 0) {
          this._saveErrors = backend400;
          return;
        }
      }
      this.err = raw;
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
    if (this._dupTarget && !this._forceCreate) return 'Edit existing rule';
    if (this._forceCreate) return 'Create separate rule';
    if (this.duplicating) return 'Duplicate safety rule';
    return 'New safety rule';
  }

  private saveBtnLabel(): string {
    if (this.isEdit || (this._dupTarget && !this._forceCreate)) return 'Save changes';
    if (this._forceCreate) return 'Create separate rule';
    return 'Create rule';
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

        ${this._renderDupBanner()}
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
        ${this._renderErrorBanner()}

        <div slot="footer" style="display: flex; gap: 8px; justify-content: flex-end;">
          <button type="button" class="rm-btn rm-btn--secondary"
            ?disabled=${this.busy} @click=${this.close}>Cancel</button>
          <button type="button" class="rm-btn rm-btn--primary"
            data-testid="saf-submit" ?disabled=${this.busy}
            @click=${() => void this.submit()}
          >${this.busy ? 'Saving…' : this.saveBtnLabel()}</button>
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
        return this.renderPiiEntityGrid(check);
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
        return this.renderRouting(check, 'type', true);
      case 'moderation-routing':
        // Stored internally as { routing: {cat→action} } and converted to
        // { block_categories, warn_categories } on save. Only block/warn
        // are expressible per-category (no require_approval_categories field).
        return this.renderModerationRouting(check);
      case 'jailbreak-phrases':
        // Backend: { phrases: list[str], case_sensitive: bool }
        // Different from llm_guard.prompt_injection / toxicity (threshold).
        return this.renderJailbreakPhrases();
      default:
        return html``;
    }
  }

  private renderPiiEntityGrid(check: SafetyCheck): TemplateResult {
    // G7: read enum values from config_schema; fall back to static list.
    const keys =
      getSchemaEnum(check, 'patterns', 'propertyNames').length > 0
        ? getSchemaEnum(check, 'patterns', 'propertyNames')
        : PII_REGEX_FALLBACK;
    // _piiKeys holds the Set of selected backend keys (SSN, CREDIT_CARD, …).
    const selected = new Set((this.config['_piiKeys'] as string[]) ?? []);
    return html`
      <label class="block text-[12.5px] font-medium mb-1">What to look for</label>
      <div class="rm-saf-cfg-checks">
        ${keys.map(
          (backendKey) => html`<label>
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
            ${enumLabel(backendKey)}
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

  private renderJailbreakPhrases(): TemplateResult {
    // Backend: { phrases: list[str], case_sensitive: bool }
    // phrases defaults to the built-in set on the server; leave empty to use it.
    const phrases = ((this.config['phrases'] as string[]) ?? []).join('\n');
    const caseSensitive = (this.config['case_sensitive'] as boolean) ?? false;
    return html`
      <label class="block text-[12.5px] font-medium mb-1">
        Custom detection phrases
        <span class="rm-saf-lblnote">one per line · leave blank to use the built-in list</span>
      </label>
      <textarea
        class="${INPUT_CLASS} font-mono text-[12.5px]"
        style="min-height:72px"
        data-testid="saf-jailbreak-phrases"
        placeholder="ignore all previous instructions&#10;pretend you have no restrictions"
        .value=${phrases}
        @input=${(e: Event) => {
          const lines = (e.target as HTMLTextAreaElement).value
            .split('\n')
            .map((s) => s.trim())
            .filter(Boolean);
          this.config = { ...this.config, phrases: lines };
        }}
      ></textarea>
      <label class="flex items-center gap-2 mt-2 text-[12.5px]">
        <input
          type="checkbox"
          data-testid="saf-case-sensitive"
          ?checked=${caseSensitive}
          @change=${(e: Event) => {
            this.config = {
              ...this.config,
              case_sensitive: (e.target as HTMLInputElement).checked,
            };
          }}
        />
        Case-sensitive matching
      </label>
      <p class="text-[11.5px] text-ink-3 dark:text-d-ink-3 mt-2 leading-snug">
        Leave the phrases box empty to use the built-in set. Add custom phrases
        to catch tenant-specific jailbreak patterns.
      </p>
    `;
  }

  // Moderation routing: only block/warn per category — the schema has no
  // require_approval_categories field, so we filter it from the options.
  private renderModerationRouting(check: SafetyCheck): TemplateResult {
    // G7: read categories from config_schema; fall back to static list.
    const categoryKeys =
      getSchemaEnum(check, 'block_categories', 'items').length > 0
        ? getSchemaEnum(check, 'block_categories', 'items')
        : MODERATION_FALLBACK;
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
        ${categoryKeys.map(
          (code) => html`<div class="rm-saf-routing-row">
            <div>
              <div class="rm-saf-rcode">${enumLabel(code)}</div>
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

  // Postel's-Law normalizer: strip scheme + trailing path, lowercase.
  // Truly-invalid lines (illegal chars) are left as-is so schema validation
  // can surface a precise error rather than silently mangling them.
  private _normalizeDomainLine(raw: string): string {
    let s = raw.trim().toLowerCase();
    s = s.replace(/^https?:\/\//, '');
    const slash = s.indexOf('/');
    if (slash !== -1) s = s.slice(0, slash);
    return s;
  }

  // onBlur: normalize every non-empty line in the textarea and rewrite it.
  private _onHostTextareaBlur(e: Event, configKey: string): void {
    const ta = e.target as HTMLTextAreaElement;
    const lines = ta.value
      .split('\n')
      .map((l) => this._normalizeDomainLine(l))
      .filter(Boolean);
    ta.value = lines.join('\n');
    this.config = { ...this.config, [configKey]: lines };
  }

  private renderHostList(): TemplateResult {
    const isEgress = this.checkId === 'egress.domain_rule';
    // egress.domain_rule → domain_patterns + optional ports (EgressDomainRuleConfig).
    // domain_allowlist  → allowed_hosts (DomainAllowlistConfig).
    const configKey = isEgress ? 'domain_patterns' : 'allowed_hosts';
    const hosts = ((this.config[configKey] as string[]) ?? []).join('\n');

    if (isEgress) {
      const portsRaw = ((this.config['ports'] as number[]) ?? []).join(', ');
      return html`
        <label class="block text-[12.5px] font-medium mb-1">
          Allowed domains
          <span class="rm-saf-lblnote">one per line · wildcards like *.stripe.com · we'll clean URLs on save</span>
        </label>
        <textarea
          class="${INPUT_CLASS} font-mono text-[12.5px]"
          style="min-height:80px"
          data-testid="saf-hosts"
          placeholder="api.stripe.com&#10;*.internal.acme.com"
          .value=${hosts}
          @input=${(e: Event) => {
            const lines = (e.target as HTMLTextAreaElement).value
              .split('\n')
              .map((s) => s.trim())
              .filter(Boolean);
            this.config = { ...this.config, domain_patterns: lines };
          }}
          @blur=${(e: Event) => this._onHostTextareaBlur(e, 'domain_patterns')}
        ></textarea>
        <label class="block text-[12.5px] font-medium mt-2 mb-1">
          Ports
          <span class="rm-saf-lblnote">optional · comma-separated · leave blank for any port</span>
        </label>
        <input
          type="text"
          class="${INPUT_CLASS}"
          data-testid="saf-egress-ports"
          placeholder="443, 8443"
          .value=${portsRaw}
          @input=${(e: Event) => {
            const raw = (e.target as HTMLInputElement).value;
            const parsed = raw
              .split(',')
              .map((s) => parseInt(s.trim(), 10))
              .filter((n) => !Number.isNaN(n) && n > 0 && n <= 65535);
            const next = { ...this.config };
            if (parsed.length) next['ports'] = parsed;
            else delete next['ports'];
            this.config = next;
          }}
        />
        <p class="text-[11.5px] text-ink-3 dark:text-d-ink-3 mt-2 leading-snug">
          The coworker can only reach these domains. Any other outbound request
          is blocked. <code>*.acme.com</code> matches subdomains but not
          <code>acme.com</code> itself.
        </p>
      `;
    }

    // domain_allowlist
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
            .split('\n')
            .map((s) => s.trim())
            .filter(Boolean);
          this.config = { ...this.config, allowed_hosts: lines };
        }}
        @blur=${(e: Event) => this._onHostTextareaBlur(e, 'allowed_hosts')}
      ></textarea>
      <p class="text-[11.5px] text-ink-3 dark:text-d-ink-3 mt-2 leading-snug">
        The coworker can only reach these hosts. Any other outbound request is
        blocked.
      </p>
    `;
  }

  // Per-finding routing table (Experience 2). For presidio.pii only.
  // Codes are read from config_schema items.enum; fall back to static list.
  private renderRouting(
    check: SafetyCheck,
    noun: 'type' | 'category',
    withThreshold: boolean,
  ): TemplateResult {
    // G7: derive entity codes from config_schema (block_codes items.enum).
    const codes =
      getSchemaEnum(check, 'block_codes', 'items').length > 0
        ? getSchemaEnum(check, 'block_codes', 'items')
        : PRESIDIO_FALLBACK;
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
        ${codes.map(
          (code) => html`<div class="rm-saf-routing-row">
            <div>
              <div class="rm-saf-rcode">${enumLabel(code)}</div>
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
    // Scope is locked in real edit mode AND when auto-flipped to edit an
    // existing rule via dup-detection (same audit-consistency reason).
    const locked = this.isEdit || (this._dupTarget !== null && !this._forceCreate);
    return html`
      <div class="mb-2">
        <label class="block text-[12.5px] font-medium mb-1">Applies to</label>
        <select
          class=${INPUT_CLASS}
          data-testid="saf-scope"
          ?disabled=${locked || this.busy}
          style=${locked ? 'opacity:0.55;cursor:not-allowed' : ''}
          @change=${(e: Event) => this.onScopeChange((e.target as HTMLSelectElement).value)}
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
