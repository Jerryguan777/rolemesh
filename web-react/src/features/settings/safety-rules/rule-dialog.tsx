// RuleDialog — create / edit / duplicate a safety rule (spec I.3;
// behavioral reference web/src/components/safety-rule-dialog.ts). One
// dialog backs all three flows:
//   - editing non-null     → edit  (PATCH; scope LOCKED — §6.11.3)
//   - duplicating non-null → create, pre-filled (POST; scope editable)
//   - both null            → create, defaults (POST)
//
// THREE EDITOR EXPERIENCES for the action, keyed on the wire check's
// action_model + cfgKind (the taxonomy is never shown): fixed →
// segmented control; config_routed → the routing table IS the editor
// (action field REMOVED); host-list → no action field (the list is the
// whole rule). A fully-inert field is removed, not greyed.
//
// G3 duplicate detection: (check_id, coworker_id, stage) collision in
// create/duplicate mode auto-flips the form to editing the existing
// rule (config pre-loaded, blue banner) with a "Create a separate rule
// anyway" escape (amber banner + return path). Platform overlaps get a
// subtle FYI only. Save dispatch: editing → PATCH; dupTarget && !force
// → PATCH the target; else POST.
//
// Scope is immutable after creation (SafetyRuleUpdate has no
// coworker_id) — edit mode locks the select; the hint's "duplicate this
// rule" link is the sanctioned scope-change path.

import { useEffect, useMemo, useRef, useState } from 'react';
import { X } from 'lucide-react';
import {
  ApiError,
  getApiClient,
  type Coworker,
  type SafetyCheck,
  type SafetyRule,
  type SafetyStage,
  type SafetyVerdictAction,
} from '../../../api/client';
import { BrandMark } from '../../../components/brand-mark';
import { Switch } from '../../../components/switch';
import { ActionPanel } from './action-panel';
import { ConfigForm } from './config-forms';
import {
  buildBackendConfig,
  normalizeConfigFromBackend,
  parseBackend400,
  validateBeforeSave,
  type SaveError,
} from './config-convert';
import { findDuplicate } from './duplicate-detection';
import {
  SAF_CONTROL_STAGES,
  SAF_STAGE_LABEL,
  SAFETY_CATEGORY_ORDER,
  SAFETY_CHECK_CATALOG,
  actionButtonState,
  naturalAction,
  safSentence,
} from './safety-catalog';

interface FormState {
  checkId: string;
  stage: SafetyStage | '';
  /** Override for fixed checks; null ⇒ natural action. */
  pickedAction: SafetyVerdictAction | null;
  coworkerId: string | null;
  priority: number;
  enabled: boolean;
  /** cfgKind-specific INTERNAL config (see config-convert). */
  config: Record<string, unknown>;
  /** Raw-JSON escape hatch; null ⇒ the visual form is authoritative. */
  advancedJson: string | null;
}

function seedFromRule(src: SafetyRule): FormState {
  const cfg = { ...(src.config ?? {}) } as Record<string, unknown>;
  const override = cfg['action_override'];
  delete cfg['action_override'];
  normalizeConfigFromBackend(src.check_id, cfg);
  return {
    checkId: src.check_id,
    stage: src.stage,
    pickedAction: typeof override === 'string' ? (override as SafetyVerdictAction) : null,
    coworkerId: src.coworker_id ?? null,
    priority: src.priority,
    enabled: src.enabled,
    config: cfg,
    advancedJson: null,
  };
}

export function RuleDialog({
  editing,
  duplicating,
  checks,
  coworkers,
  rules,
  onClose,
  onSaved,
  onDuplicateFromEdit,
}: {
  editing: SafetyRule | null;
  duplicating: SafetyRule | null;
  checks: SafetyCheck[];
  coworkers: Coworker[];
  /** All existing rules — G3 duplicate detection input. */
  rules: SafetyRule[];
  onClose: () => void;
  onSaved: (rule: SafetyRule, toast: string) => void;
  /** The scope-change path: close this edit, reopen as duplicate. */
  onDuplicateFromEdit: (source: SafetyRule) => void;
}) {
  const isEdit = editing !== null;
  const seedSource = editing ?? duplicating;

  const [form, setForm] = useState<FormState>(() => {
    if (seedSource) return seedFromRule(seedSource);
    const first = checks[0];
    return {
      checkId: first?.id ?? '',
      stage: (first?.stages?.[0] as SafetyStage) ?? '',
      pickedAction: null,
      coworkerId: null,
      priority: 100,
      enabled: true,
      config: {},
      advancedJson: null,
    };
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [saveErrors, setSaveErrors] = useState<SaveError[]>([]);
  // G3 state.
  const [dupTarget, setDupTarget] = useState<SafetyRule | null>(null);
  const [forceCreate, setForceCreate] = useState(false);
  const [platformOverlap, setPlatformOverlap] = useState<SafetyRule | null>(null);
  /** Last auto-loaded dup id — guards the preload against re-running. */
  const loadedDupId = useRef<string | null>(null);

  const check = useMemo(
    () => checks.find((c) => c.id === form.checkId) ?? null,
    [checks, form.checkId],
  );
  const pres = check ? SAFETY_CHECK_CATALOG[check.id] : undefined;
  const cfgKind = pres?.cfgKind;
  const showActionPanel =
    !!check && cfgKind !== 'host-list' && check.action_model === 'fixed';

  // G3: re-detect whenever the triple changes (skipped in real edit mode
  // and after the force-create opt-out).
  useEffect(() => {
    if (isEdit || forceCreate) {
      setDupTarget(null);
      setPlatformOverlap(null);
      return;
    }
    const hit = findDuplicate(
      rules,
      { checkId: form.checkId, stage: form.stage, coworkerId: form.coworkerId },
      duplicating?.id,
    );
    setDupTarget(hit.orgMatch);
    setPlatformOverlap(hit.platformMatch);
    if (hit.orgMatch && loadedDupId.current !== hit.orgMatch.id) {
      // Pre-load the existing rule — the user is editing it now.
      loadedDupId.current = hit.orgMatch.id;
      setForm(seedFromRule(hit.orgMatch));
    }
    if (!hit.orgMatch) loadedDupId.current = null;
  }, [
    isEdit,
    forceCreate,
    rules,
    form.checkId,
    form.stage,
    form.coworkerId,
    duplicating?.id,
  ]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      if (!busy) onClose();
    }
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [busy, onClose]);

  function onCheckChange(id: string) {
    const next = checks.find((c) => c.id === id);
    setForm((f) => ({
      ...f,
      checkId: id,
      stage: (next?.stages?.[0] as SafetyStage) ?? '',
      pickedAction: null,
      config: {},
      advancedJson: null,
    }));
  }

  function onStageChange(stage: SafetyStage) {
    setForm((f) => {
      // An override valid on the old stage may be unsupported on the new
      // one; drop it so we never submit an unsupported action.
      let picked = f.pickedAction;
      if (picked) {
        const nat = naturalAction(check, stage);
        if (!actionButtonState(check, stage, picked, nat).enabled) picked = null;
      }
      return { ...f, stage, pickedAction: picked };
    });
  }

  function onForceCreate() {
    setForceCreate(true);
    loadedDupId.current = null;
    // Reset to defaults; keep the check/stage the user selected.
    setForm((f) => ({
      ...f,
      pickedAction: null,
      config: {},
      priority: 100,
      enabled: true,
      coworkerId: null,
      advancedJson: null,
    }));
  }

  /** Backend-shaped config from the current form state. Advanced JSON
   *  wins when visible + parseable (§6.12); otherwise the visual form,
   *  with action_override written only for a non-natural fixed pick. */
  function buildConfig(): Record<string, unknown> {
    if (form.advancedJson !== null) {
      const trimmed = form.advancedJson.trim();
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
    const cfg: Record<string, unknown> = { ...form.config };
    if (showActionPanel && form.pickedAction) {
      const nat = naturalAction(check, form.stage as SafetyStage);
      if (form.pickedAction !== nat) cfg['action_override'] = form.pickedAction;
    }
    return buildBackendConfig(form.checkId, cfg);
  }

  async function submit() {
    if (busy) return;
    if (!form.checkId || !form.stage) {
      setErr('Pick a check and a stage.');
      return;
    }
    // G4 Layer 1 — client-side validation before any network call.
    const config = buildConfig();
    const errs = validateBeforeSave(check, config);
    if (errs.length > 0) {
      setSaveErrors(errs);
      return;
    }
    setSaveErrors([]);
    setBusy(true);
    setErr(null);
    try {
      const api = getApiClient();
      let saved: SafetyRule;
      let toast: string;
      if (isEdit) {
        // Scope (coworker_id) intentionally omitted — immutable on edit.
        saved = await api.updateSafetyRule(editing.id, {
          check_id: form.checkId,
          stage: form.stage,
          config,
          priority: form.priority,
          enabled: form.enabled,
        });
        toast = 'Rule updated';
      } else if (dupTarget && !forceCreate) {
        // G3 auto-flip: PATCH the existing rule, not POST.
        saved = await api.updateSafetyRule(dupTarget.id, {
          check_id: form.checkId,
          stage: form.stage,
          config,
          priority: form.priority,
          enabled: form.enabled,
        });
        toast = 'Rule updated';
      } else {
        saved = await api.createSafetyRule({
          check_id: form.checkId,
          stage: form.stage,
          coworker_id: form.coworkerId,
          config,
          priority: form.priority,
          enabled: form.enabled,
          // Wire default; the card renderer doesn't consume description
          // (I.6.4) and the dialog exposes no field for it.
          description: '',
        });
        toast = 'Rule created';
      }
      onSaved(saved, toast);
      onClose();
    } catch (e) {
      // G4 Layer 2 — translate a FastAPI 4xx detail array when present.
      if (e instanceof ApiError && e.body) {
        const backend400 = parseBackend400(e.body);
        if (backend400.length > 0) {
          setSaveErrors(backend400);
          setBusy(false);
          return;
        }
      }
      setErr(
        e instanceof ApiError ? (e.body?.message ?? `HTTP ${e.status}`) : (e as Error).message,
      );
      setBusy(false);
    }
  }

  const title = isEdit
    ? 'Edit safety rule'
    : dupTarget && !forceCreate
      ? 'Edit existing rule'
      : forceCreate
        ? 'Create separate rule'
        : duplicating
          ? 'Duplicate safety rule'
          : 'New safety rule';
  const saveLabel =
    isEdit || (dupTarget && !forceCreate)
      ? 'Save changes'
      : forceCreate
        ? 'Create separate rule'
        : 'Create rule';
  const scopeLocked = isEdit || (dupTarget !== null && !forceCreate);

  // Check select grouped by category, catalog order first.
  const byCategory = useMemo(() => {
    const map = new Map<string, SafetyCheck[]>();
    for (const c of checks) {
      const cat = SAFETY_CHECK_CATALOG[c.id]?.category ?? 'Other';
      map.set(cat, [...(map.get(cat) ?? []), c]);
    }
    return [...map.entries()].sort(
      (a, b) =>
        (SAFETY_CATEGORY_ORDER.indexOf(a[0]) + 99) -
        (SAFETY_CATEGORY_ORDER.indexOf(b[0]) + 99),
    );
  }, [checks]);

  const stages = (check?.stages ?? []) as SafetyStage[];
  const controlStage = form.stage && SAF_CONTROL_STAGES.has(form.stage);
  const scopeName = form.coworkerId
    ? (coworkers.find((c) => c.id === form.coworkerId)?.name ?? null)
    : null;
  const previewSentence = form.checkId && form.stage
    ? safSentence(
        { check_id: form.checkId, stage: form.stage as SafetyStage, config: buildConfig() },
        check,
        scopeName,
      )
    : '';

  const dupScopeName = dupTarget?.coworker_id
    ? (coworkers.find((c) => c.id === dupTarget.coworker_id)?.name ?? dupTarget.coworker_id)
    : 'all coworkers';

  return (
    <div
      className="scrim"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div
        className="dlg"
        style={{ width: 640 }}
        role="dialog"
        aria-modal="true"
        aria-label="Safety rule"
      >
        <div className="dlg-header">
          <div className="hleft">
            <div className="dlg-brand-icon">
              <BrandMark size={16} />
            </div>
            <h2 className="dlg-title">{title}</h2>
          </div>
          <button className="icon-btn" aria-label="Close" disabled={busy} onClick={onClose}>
            <X />
          </button>
        </div>
        <div className="wiz-body" style={{ minHeight: 0 }}>
          <div className="hint" style={{ marginBottom: 12 }}>
            Safety rules run automatically — no one in the loop — except when you
            set the action to <b>Approve</b>, which routes to the same approval
            surface as business policies. Changes apply to new agent tasks;
            already-running tasks finish with the current rules.
          </div>

          {/* G3 banners */}
          {dupTarget && !forceCreate ? (
            <div className="dup-banner info" data-testid="saf-dup-banner-info">
              <span>ℹ</span>
              <span>
                You already have a <b>{SAFETY_CHECK_CATALOG[dupTarget.check_id]?.label ?? dupTarget.check_id}</b>{' '}
                rule for <b>{SAF_STAGE_LABEL[dupTarget.stage] ?? dupTarget.stage}</b> on{' '}
                <b>{dupScopeName}</b>. You're editing that rule now (not creating a
                new one).{' '}
                <button type="button" onClick={onForceCreate}>
                  Create a separate rule anyway
                </button>
              </span>
            </div>
          ) : forceCreate ? (
            <div className="dup-banner warn" data-testid="saf-dup-banner-warn">
              <span>⚠</span>
              <span>
                Creating a second rule for the same surface — they may conflict.{' '}
                <button
                  type="button"
                  onClick={() => {
                    setForceCreate(false);
                    loadedDupId.current = null;
                  }}
                >
                  Switch back to editing the existing one
                </button>
              </span>
            </div>
          ) : platformOverlap ? (
            <div className="dup-banner subtle" data-testid="saf-dup-banner-fyi">
              <span>🛡</span>
              <span>
                A platform default{' '}
                <b>{SAFETY_CHECK_CATALOG[platformOverlap.check_id]?.label ?? platformOverlap.check_id}</b>{' '}
                already covers this surface — your org rule will run alongside it.
              </span>
            </div>
          ) : null}

          {/* Check */}
          <div className="field">
            <label htmlFor="saf-check">Check</label>
            <select
              id="saf-check"
              disabled={busy}
              value={form.checkId}
              onChange={(e) => onCheckChange(e.target.value)}
            >
              {byCategory.map(([cat, list]) => (
                <optgroup key={cat} label={cat}>
                  {list.map((c) => (
                    <option key={c.id} value={c.id}>
                      {SAFETY_CHECK_CATALOG[c.id]?.label ?? c.id} ({c.cost_class})
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            {pres ? <div className="hint">{pres.desc}</div> : null}
            {check?.cost_class === 'slow' ? (
              <div className="hint" style={{ color: 'var(--rm-warn-text)' }} data-testid="saf-slow-warn">
                This check adds noticeable latency — avoid putting it on a fast-path
                stage like "before tool calls".
              </div>
            ) : null}
          </div>

          {/* Stage */}
          <div className="field">
            <label htmlFor="saf-stage">Where it runs</label>
            <select
              id="saf-stage"
              disabled={busy}
              value={form.stage}
              onChange={(e) => onStageChange(e.target.value as SafetyStage)}
            >
              {stages.map((s) => (
                <option key={s} value={s}>
                  {SAF_STAGE_LABEL[s] ?? s}
                </option>
              ))}
            </select>
            {form.stage ? (
              <div className="hint">
                {controlStage
                  ? 'If this check errors here, the call is blocked by default.'
                  : 'If this check errors here, the call is let through.'}
              </div>
            ) : null}
          </div>

          {/* Action panel — experience 1 only */}
          {showActionPanel ? (
            <ActionPanel
              check={check}
              stage={form.stage as SafetyStage}
              pickedAction={form.pickedAction}
              busy={busy}
              onPick={(a) => setForm((f) => ({ ...f, pickedAction: a }))}
            />
          ) : null}

          {/* Config form + advanced JSON hatch */}
          {check && cfgKind ? (
            <div className="field" data-testid="saf-config">
              {form.advancedJson !== null ? (
                <>
                  <label htmlFor="saf-config-json">Configuration (JSON)</label>
                  <textarea
                    id="saf-config-json"
                    className="mono"
                    style={{ minHeight: 120 }}
                    data-testid="saf-config-json"
                    disabled={busy}
                    value={form.advancedJson}
                    onChange={(e) =>
                      setForm((f) => ({ ...f, advancedJson: e.target.value }))
                    }
                  />
                </>
              ) : (
                <ConfigForm
                  check={check}
                  cfgKind={cfgKind}
                  stage={form.stage as SafetyStage}
                  config={form.config}
                  busy={busy}
                  onChange={(next) => setForm((f) => ({ ...f, config: next }))}
                />
              )}
              <button
                type="button"
                className="btn-ghost"
                style={{ padding: 0, fontSize: '11.5px', marginTop: 8 }}
                data-testid="saf-adv-toggle"
                disabled={busy}
                onClick={() =>
                  setForm((f) =>
                    f.advancedJson === null
                      ? { ...f, advancedJson: JSON.stringify(buildConfig(), null, 2) }
                      : { ...f, advancedJson: null },
                  )
                }
              >
                {form.advancedJson !== null ? 'Use the visual form' : 'Advanced: edit as JSON'}
              </button>
            </div>
          ) : null}

          {/* Scope */}
          <div className="field">
            <label htmlFor="saf-scope">Applies to</label>
            <select
              id="saf-scope"
              disabled={scopeLocked || busy}
              style={scopeLocked ? { opacity: 0.55, cursor: 'not-allowed' } : undefined}
              value={form.coworkerId ?? ''}
              onChange={(e) =>
                setForm((f) => ({ ...f, coworkerId: e.target.value || null }))
              }
            >
              <option value="">All coworkers</option>
              {coworkers.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
            {scopeLocked ? (
              <div className="hint" data-testid="saf-scope-locked">
                🔒 Scope is fixed after creation (for audit consistency). To move
                scope,{' '}
                <button
                  type="button"
                  className="btn-ghost"
                  style={{ padding: 0, fontSize: 'inherit', textDecoration: 'underline' }}
                  onClick={() => {
                    const src = editing ?? dupTarget;
                    if (src) onDuplicateFromEdit(src);
                  }}
                >
                  duplicate this rule
                </button>{' '}
                with the new scope, then delete this one.
              </div>
            ) : null}
          </div>

          {/* Priority + Status */}
          <div
            style={{ display: 'flex', gap: 20, alignItems: 'flex-end', marginBottom: 14 }}
          >
            <div className="field" style={{ marginBottom: 0 }}>
              <label htmlFor="saf-priority" style={{ whiteSpace: 'nowrap' }}>
                Priority{' '}
                <span style={{ fontWeight: 400, color: 'var(--rm-text-muted)' }}>
                  higher wins on ties
                </span>
              </label>
              <input
                id="saf-priority"
                type="text"
                inputMode="numeric"
                style={{ width: 96, display: 'block' }}
                disabled={busy}
                value={String(form.priority)}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    priority: parseInt(e.target.value, 10) || 0,
                  }))
                }
              />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Status</label>
              <Switch
                on={form.enabled}
                disabled={busy}
                onToggle={() => setForm((f) => ({ ...f, enabled: !f.enabled }))}
              />
            </div>
          </div>

          {/* Live preview — the SAME safSentence the cards render */}
          {previewSentence ? (
            <div className="preview" data-testid="saf-preview">
              <div className="pv-label">This rule</div>
              <span
                dangerouslySetInnerHTML={{
                  __html: `${previewSentence} Priority <b>${form.priority}</b>.${
                    form.enabled ? '' : " <i>(disabled — won't run until re-enabled)</i>"
                  }`,
                }}
              />
            </div>
          ) : null}

          {saveErrors.length > 0 ? (
            <div className="row-error" style={{ marginTop: 10 }} data-testid="saf-error-banner">
              <b>{saveErrors.length === 1 ? 'Fix this before saving' : 'Fix these before saving'}</b>
              <ul style={{ margin: '4px 0 0 18px' }}>
                {saveErrors.map((e, i) => (
                  <li key={i}>{e.message}</li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
        <div className="wiz-foot">
          {err ? (
            <span className="wiz-err" role="alert">
              {err}
            </span>
          ) : (
            <span />
          )}
          <span style={{ display: 'inline-flex', gap: 8 }}>
            <button className="btn-ghost" disabled={busy} onClick={onClose}>
              Cancel
            </button>
            <button
              className="btn-primary"
              data-testid="saf-submit"
              disabled={busy}
              onClick={() => void submit()}
            >
              {busy ? 'Saving…' : saveLabel}
            </button>
          </span>
        </div>
      </div>
    </div>
  );
}
