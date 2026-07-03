// SafetyRulesPage — detector-driven guardrail CRUD (spec Part I;
// behavioral reference web/src/components/safety-rules-page.ts). Two
// tiers, never interleaved: platform defaults first (banner + read-only
// cards, audit-only), then YOUR ORGANIZATION'S RULES. Each tier sorts
// in server evaluation order (priority desc, created_at desc — the
// shared lib/rule-ordering sort). Optimistic enable toggle (§H
// semantics); create/edit/duplicate share one dialog; delete restates
// the rule and explains snapshot semantics in plain words.

import { useEffect, useMemo, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Plus, ShieldCheck } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { getApiClient, type SafetyRule } from '../../../api/client';
import {
  useCoworkers,
  useSafetyChecks,
  useSafetyRules,
} from '../../../api/queries';
import { ConfirmDialog } from '../../../components/confirm-dialog';
import { sortByEvaluationOrder } from '../../../lib/rule-ordering';
import { AuditDrawer } from './audit-drawer';
import { RuleCard } from './rule-card';
import { RuleDialog } from './rule-dialog';
import { checkLabel, safSentence } from './safety-catalog';
import './safety-rules.css';

interface DialogState {
  open: boolean;
  editing: SafetyRule | null;
  duplicating: SafetyRule | null;
}

export function SafetyRulesPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const rulesQ = useSafetyRules();
  const checksQ = useSafetyChecks();
  const coworkersQ = useCoworkers();

  const [dialog, setDialog] = useState<DialogState>({
    open: false,
    editing: null,
    duplicating: null,
  });
  const [deleteTarget, setDeleteTarget] = useState<SafetyRule | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [auditTarget, setAuditTarget] = useState<SafetyRule | null>(null);
  const [togglingIds, setTogglingIds] = useState<Set<string>>(new Set());
  const [flashId, setFlashId] = useState<string | null>(null);
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  function showToast(msg: string) {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast(msg);
    toastTimer.current = setTimeout(() => setToast(null), 3200);
  }
  useEffect(
    () => () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
      if (flashTimer.current) clearTimeout(flashTimer.current);
    },
    [],
  );

  const rules = useMemo(() => rulesQ.data ?? [], [rulesQ.data]);
  const checks = checksQ.data ?? [];
  const coworkers = coworkersQ.data ?? [];

  const checkMeta = (id: string) => checks.find((c) => c.id === id) ?? null;
  const coworkerName = (id: string | null | undefined): string | null => {
    if (!id) return null;
    const cw = coworkers.find((c) => c.id === id);
    return cw ? cw.name : id.slice(0, 8);
  };

  const { platform, org } = useMemo(() => {
    const sorted = sortByEvaluationOrder(rules);
    return {
      platform: sorted.filter((r) => r.source === 'platform'),
      org: sorted.filter((r) => r.source !== 'platform'),
    };
  }, [rules]);

  const setRows = (fn: (rows: SafetyRule[]) => SafetyRule[]) =>
    queryClient.setQueryData<SafetyRule[]>(['safety-rules'], (cur) =>
      fn(cur ?? []),
    );

  /** Optimistic enable/disable (§H semantics): flip immediately, PATCH
   *  behind, revert + toast on failure. Platform rules never reach here
   *  (the card renders a fixed-on no-op). */
  async function toggleEnabled(rule: SafetyRule) {
    if (rule.source === 'platform' || togglingIds.has(rule.id)) return;
    const next = !rule.enabled;
    setRows((rows) =>
      rows.map((r) => (r.id === rule.id ? { ...r, enabled: next } : r)),
    );
    setTogglingIds((ids) => new Set(ids).add(rule.id));
    try {
      await getApiClient().updateSafetyRule(rule.id, { enabled: next });
    } catch {
      setRows((rows) =>
        rows.map((r) => (r.id === rule.id ? { ...r, enabled: rule.enabled } : r)),
      );
      showToast('Couldn’t update — try again');
    } finally {
      setTogglingIds((ids) => {
        const out = new Set(ids);
        out.delete(rule.id);
        return out;
      });
    }
  }

  /** Splice the saved rule into the cache + pulse the card. */
  function onSaved(rule: SafetyRule, toastMsg: string) {
    setRows((rows) =>
      rows.some((r) => r.id === rule.id)
        ? rows.map((r) => (r.id === rule.id ? rule : r))
        : [...rows, rule],
    );
    showToast(toastMsg);
    setFlashId(rule.id);
    if (flashTimer.current) clearTimeout(flashTimer.current);
    flashTimer.current = setTimeout(() => setFlashId(null), 1800);
    requestAnimationFrame(() => {
      document
        .querySelector(`[data-rule-id="${rule.id}"]`)
        ?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
  }

  /** Edit-dialog "duplicate this rule" link (§6.11.3 — the sanctioned
   *  scope-change path): close the edit, reopen as duplicate. */
  function onDuplicateFromEdit(source: SafetyRule) {
    setDialog({ open: false, editing: null, duplicating: null });
    requestAnimationFrame(() =>
      setDialog({ open: true, editing: null, duplicating: source }),
    );
  }

  async function performDelete() {
    const r = deleteTarget;
    if (!r || deleteBusy) return;
    setDeleteBusy(true);
    try {
      await getApiClient().deleteSafetyRule(r.id);
      setRows((rows) => rows.filter((x) => x.id !== r.id));
      showToast('Rule deleted');
    } catch (err) {
      showToast((err as Error).message ?? 'delete failed');
    } finally {
      setDeleteBusy(false);
      setDeleteTarget(null);
    }
  }

  const openCreate = () =>
    setDialog({ open: true, editing: null, duplicating: null });

  const renderCard = (r: SafetyRule) => (
    <RuleCard
      key={r.id}
      rule={r}
      check={checkMeta(r.check_id)}
      coworkerName={coworkerName(r.coworker_id)}
      toggling={togglingIds.has(r.id)}
      flash={flashId === r.id}
      onToggle={() => void toggleEnabled(r)}
      onEdit={() => setDialog({ open: true, editing: r, duplicating: null })}
      onDuplicate={() => setDialog({ open: true, editing: null, duplicating: r })}
      onAudit={() => setAuditTarget(r)}
      onDelete={() => setDeleteTarget(r)}
    />
  );

  const deleteMeta = deleteTarget ? checkMeta(deleteTarget.check_id) : null;

  return (
    <div className="page">
      <div>
        <button className="back-link" onClick={() => navigate('/')}>
          <ArrowLeft />
          Back to chat
        </button>
      </div>
      <div className="page-head">
        <div>
          <h1 className="page-title">Safety rules</h1>
          <div className="page-sub" style={{ maxWidth: 640 }}>
            Automatic guardrails that scan for personal data, prompt injection,
            secrets, or untrusted domains. Unlike approval policies these run with
            no human in the loop — except when a rule's action is set to Approve,
            which routes to the same approval surface.
          </div>
        </div>
        <button className="btn-primary" onClick={openCreate}>
          <Plus />
          New rule
        </button>
      </div>

      <div className="grid-scroll" style={{ paddingTop: 12 }}>
        {rulesQ.isLoading || checksQ.isLoading ? (
          <div className="page-sub">Loading…</div>
        ) : rulesQ.isError ? (
          <div className="row-error">
            Failed to load safety rules — retry from the sidebar.
          </div>
        ) : rules.length === 0 ? (
          <div className="grid-empty">
            <div style={{ margin: 'auto', textAlign: 'center' }}>
              <span style={{ color: 'var(--rm-action)' }}>
                <ShieldCheck size={64} strokeWidth={1.2} />
              </span>
              <div style={{ marginTop: '0.75rem', fontSize: '1rem' }}>
                No safety rules yet.
              </div>
              <div
                style={{
                  marginTop: 4,
                  fontSize: '0.875rem',
                  color: 'var(--rm-text-muted)',
                  maxWidth: 400,
                }}
              >
                Coworkers run with no automatic guardrails. Create your first rule
                to scan for personal data, prompt injection, or untrusted domains.
              </div>
              <button
                className="btn-primary"
                style={{ marginTop: '1rem' }}
                onClick={openCreate}
              >
                <Plus />
                Create your first rule
              </button>
            </div>
          </div>
        ) : (
          <>
            {platform.length ? (
              <div className="plat-banner">
                <ShieldCheck size={16} style={{ flexShrink: 0, marginTop: 1 }} />
                <span>
                  <b>Platform defaults</b> — these rules apply to every
                  organization and can't be edited or disabled at this level.
                  Contact the platform admin to change.
                </span>
              </div>
            ) : null}
            {platform.map(renderCard)}
            {platform.length && org.length ? (
              <div className="saf-section">Your organization's rules</div>
            ) : null}
            {org.map(renderCard)}
            {/* Evaluation-order + snapshot semantics in plain words —
                hidden when the list is empty. */}
            <div className="page-hint">
              Higher-priority rules run first; ties go to the newest. Changes apply
              to new agent tasks — tasks already in progress keep the rules they
              started with until they finish.
            </div>
          </>
        )}
      </div>

      {dialog.open ? (
        <RuleDialog
          editing={dialog.editing}
          duplicating={dialog.duplicating}
          checks={checks}
          coworkers={coworkers}
          rules={rules}
          onClose={() => setDialog({ open: false, editing: null, duplicating: null })}
          onSaved={onSaved}
          onDuplicateFromEdit={onDuplicateFromEdit}
        />
      ) : null}

      {auditTarget ? (
        <AuditDrawer rule={auditTarget} onClose={() => setAuditTarget(null)} />
      ) : null}

      {deleteTarget ? (
        <ConfirmDialog
          title="Delete safety rule?"
          confirmLabel="Delete rule"
          busyLabel="Deleting…"
          busy={deleteBusy}
          onConfirm={() => void performDelete()}
          onCancel={() => {
            if (!deleteBusy) setDeleteTarget(null);
          }}
        >
          <p style={{ margin: '0 0 10px' }}>
            You're about to delete the rule <b>{checkLabel(deleteTarget.check_id)}</b>{' '}
            —{' '}
            <span
              dangerouslySetInnerHTML={{
                __html: safSentence(
                  {
                    check_id: deleteTarget.check_id,
                    stage: deleteTarget.stage,
                    config: (deleteTarget.config ?? {}) as Record<string, unknown>,
                  },
                  deleteMeta,
                  coworkerName(deleteTarget.coworker_id),
                ),
              }}
            />
          </p>
          <p style={{ margin: '0 0 10px' }}>
            After deletion, this check stops running on new agent tasks. Tasks
            already in progress keep using this rule until they finish, then move
            on.
          </p>
          <p style={{ margin: 0, fontSize: '12.5px', color: 'var(--rm-text-muted)' }}>
            Past safety log entries are kept. The change history for this rule is
            also kept — you can review it later if there's ever an audit.
          </p>
        </ConfirmDialog>
      ) : null}

      {toast ? (
        <div className="toast" role="status">
          {toast}
        </div>
      ) : null}
    </div>
  );
}
