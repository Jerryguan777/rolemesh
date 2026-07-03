// ApprovalPoliciesPage — HITL tool-approval policy CRUD (spec Part H;
// behavioral reference web/src/components/approval-policies-page.ts).
// The list ranks rules in the same order the server evaluates them
// (priority desc, then created_at desc), shows a priority badge + an
// always-visible enable switch per row, and reveals Edit / Duplicate /
// Delete on hover. Create/edit/duplicate share one dialog; delete goes
// through a confirmation that restates the rule via the SAME
// conditionSentence renderer.
//
// The enable toggle is OPTIMISTIC (spec §5.3): flip the cache
// immediately, PATCH behind, revert + toast on failure. No confirm —
// fully reversible, and the high-frequency op stays out of hover.
//
// The timeout copy says 5 minutes — the Lit page documents the spec's
// "20 minutes" as stale (APPROVAL_TIMEOUT = 300_000ms).

import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Plus } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import {
  ApiError,
  getApiClient,
  type ApprovalPolicy,
} from '../../../api/client';
import { useApprovalPolicies } from '../../../api/queries';
import { BrandMark } from '../../../components/brand-mark';
import { ConfirmDialog } from '../../../components/confirm-dialog';
import { conditionSentence } from './condition-form';
import { PolicyCard } from './policy-card';
import { PolicyDialog } from './policy-dialog';
import './approval-policies.css';

/** List order = server evaluation order (spec §5.5): priority desc, then
 *  the newest rule first on ties. created_at is ISO-8601 from the API. */
export function sortPolicies(rows: ApprovalPolicy[]): ApprovalPolicy[] {
  return [...rows].sort(
    (a, b) =>
      b.priority - a.priority ||
      Date.parse(b.created_at) - Date.parse(a.created_at),
  );
}

function errText(err: unknown): string {
  if (err instanceof ApiError) return err.body?.message ?? `HTTP ${err.status}`;
  return (err as Error).message;
}

interface DialogState {
  open: boolean;
  editing: ApprovalPolicy | null;
  duplicating: ApprovalPolicy | null;
}

export function ApprovalPoliciesPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const policiesQ = useApprovalPolicies();

  const [dialog, setDialog] = useState<DialogState>({
    open: false,
    editing: null,
    duplicating: null,
  });
  const [deleteTarget, setDeleteTarget] = useState<ApprovalPolicy | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  /** Ids mid-PATCH on their toggle — a double-click can't queue two
   *  conflicting writes. */
  const [togglingIds, setTogglingIds] = useState<Set<string>>(new Set());
  /** Id to pulse after a create/duplicate/edit save (spec §5.7). */
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

  const setRows = (fn: (rows: ApprovalPolicy[]) => ApprovalPolicy[]) =>
    queryClient.setQueryData<ApprovalPolicy[]>(['approval-policies'], (cur) =>
      fn(cur ?? []),
    );

  /** Optimistic enable/disable (spec §5.3): flip immediately, PATCH in
   *  the background, revert + toast on failure. */
  async function toggleEnabled(row: ApprovalPolicy) {
    if (togglingIds.has(row.id)) return;
    const next = !row.enabled;
    setRows((rows) =>
      rows.map((r) => (r.id === row.id ? { ...r, enabled: next } : r)),
    );
    setTogglingIds((ids) => new Set(ids).add(row.id));
    try {
      await getApiClient().updateApprovalPolicy(row.id, { enabled: next });
    } catch {
      // Revert to the value we flipped away from.
      setRows((rows) =>
        rows.map((r) => (r.id === row.id ? { ...r, enabled: row.enabled } : r)),
      );
      showToast('Couldn’t update — try again');
    } finally {
      setTogglingIds((ids) => {
        const out = new Set(ids);
        out.delete(row.id);
        return out;
      });
    }
  }

  /** Splice the saved policy into the cache (no full re-fetch) so we can
   *  pulse the exact card. Create/duplicate append; edit replaces. */
  function onSaved(policy: ApprovalPolicy, toastMsg: string) {
    setRows((rows) =>
      rows.some((r) => r.id === policy.id)
        ? rows.map((r) => (r.id === policy.id ? policy : r))
        : [...rows, policy],
    );
    showToast(toastMsg);
    setFlashId(policy.id);
    if (flashTimer.current) clearTimeout(flashTimer.current);
    flashTimer.current = setTimeout(() => setFlashId(null), 1800);
    // Scroll after the list re-renders (sorted position may be anywhere).
    requestAnimationFrame(() => {
      document
        .querySelector(`[data-policy-id="${policy.id}"]`)
        ?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
  }

  async function performDelete() {
    const row = deleteTarget;
    if (!row || deleteBusy) return;
    setDeleteBusy(true);
    try {
      await getApiClient().deleteApprovalPolicy(row.id);
      setRows((rows) => rows.filter((r) => r.id !== row.id));
      showToast('Policy deleted');
    } catch (err) {
      showToast(errText(err));
    } finally {
      setDeleteBusy(false);
      setDeleteTarget(null);
    }
  }

  const openCreate = () =>
    setDialog({ open: true, editing: null, duplicating: null });

  const sorted = sortPolicies(policiesQ.data ?? []);
  const deleteToolDisp =
    deleteTarget?.tool_name === '*' ? 'any tool' : deleteTarget?.tool_name;

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
          <h1 className="page-title">Approval policies</h1>
          <div className="page-sub" style={{ maxWidth: 640 }}>
            Which actions your coworkers should pause and confirm with you before
            running. Confirmations appear in the chat; for scheduled tasks they go
            to whoever set up the task.
          </div>
        </div>
        <button className="btn-primary" onClick={openCreate}>
          <Plus />
          New policy
        </button>
      </div>

      <div className="grid-scroll" style={{ paddingTop: 12 }}>
        {policiesQ.isLoading ? (
          <div className="page-sub">Loading…</div>
        ) : policiesQ.isError ? (
          <div className="row-error">
            Failed to load approval policies — retry from the sidebar.
          </div>
        ) : sorted.length === 0 ? (
          <div className="grid-empty">
            <div style={{ margin: 'auto', textAlign: 'center' }}>
              <BrandMark size={128} />
              <div style={{ marginTop: '0.75rem', fontSize: '1rem' }}>
                No approval policies yet.
              </div>
              <div
                style={{
                  marginTop: 4,
                  fontSize: '0.875rem',
                  color: 'var(--rm-text-muted)',
                  maxWidth: 380,
                }}
              >
                Every tool call runs without asking. Create your first policy to
                gate consequential actions.
              </div>
              <button
                className="btn-primary"
                style={{ marginTop: '1rem' }}
                onClick={openCreate}
              >
                <Plus />
                Create your first policy
              </button>
            </div>
          </div>
        ) : (
          <>
            {sorted.map((p) => (
              <PolicyCard
                key={p.id}
                policy={p}
                toggling={togglingIds.has(p.id)}
                flash={flashId === p.id}
                onToggle={() => void toggleEnabled(p)}
                onEdit={() =>
                  setDialog({ open: true, editing: p, duplicating: null })
                }
                onDuplicate={() =>
                  setDialog({ open: true, editing: null, duplicating: p })
                }
                onDelete={() => setDeleteTarget(p)}
              />
            ))}
            {/* Evaluation-order hint — meaningless with no rules, so it only
                renders under a non-empty list. Timeout copy: 5 minutes (the
                Lit page corrects the design's stale 20-minute text). */}
            <div className="page-hint">
              Anything not matching above runs without asking. When multiple rules
              match the same call, the highest priority wins; ties go to the
              newest. Approvals time out after 5 minutes and auto-reject — the
              coworker can re-request next turn.
            </div>
          </>
        )}
      </div>

      {dialog.open ? (
        <PolicyDialog
          editing={dialog.editing}
          duplicating={dialog.duplicating}
          onClose={() => setDialog({ open: false, editing: null, duplicating: null })}
          onSaved={onSaved}
        />
      ) : null}

      {deleteTarget ? (
        <ConfirmDialog
          title="Delete approval policy?"
          confirmLabel="Delete policy"
          busyLabel="Deleting…"
          busy={deleteBusy}
          onConfirm={() => void performDelete()}
          onCancel={() => {
            if (!deleteBusy) setDeleteTarget(null);
          }}
        >
          <p style={{ margin: '0 0 10px' }}>
            You’re about to delete the policy that pauses for{' '}
            <b>
              {deleteTarget.mcp_server_name} · {deleteToolDisp}
            </b>{' '}
            <span
              dangerouslySetInnerHTML={{
                __html: conditionSentence(deleteTarget.condition_expr),
              }}
            />
            .
          </p>
          <p
            style={{
              margin: 0,
              fontSize: '12.5px',
              color: 'var(--rm-text-muted)',
              lineHeight: 1.55,
            }}
          >
            After deletion, matching calls will run without asking. Pending
            approvals already raised under this policy stay live until decided or
            expired; only future calls change.
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
