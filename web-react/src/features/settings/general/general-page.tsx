// GeneralPage — owner-only tenant settings (spec Part K). No Lit
// behavioral reference exists (its General entry is a coming-soon
// stub); D-K1 (user-approved) ships this ahead of the Lit SPA, bound
// STRICTLY to the shipped wire (GET/PATCH /api/v1/tenant) — no invented
// fields, no invented flows. Reconcile per the behavior-parity rule
// when the Lit page lands.
//
// A form, not a collection: one 760px column (D-UI2 — the v12
// prototype said 560; forms/panels joined the D-UI1 row cap so every
// settings surface shares one width), dirty-tracked Save
// (disabled when clean, invalid, or busy) + Revert (visible only when
// dirty); one PATCH carries both writable fields; field errors replace
// the hint line in danger color (the dialogs' pattern).
//
// max_concurrent_containers closes a loop from Part C: the coworker
// wizard's locked decision #10 kept it out of the wizard ("backend
// default applies") — this tenant-level control is that decision's
// intended home. Client-side gate is integer >= 1 only; the wire's
// max (100) stays server-authoritative.

import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { ArrowLeft } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { ApiError, getApiClient } from '../../../api/client';
import { useTenant } from '../../../api/queries';
import { BrandMark } from '../../../components/brand-mark';

interface FormState {
  name: string;
  /** NaN while the input holds a non-integer — blocks Save. */
  mcc: number;
}

export function GeneralPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const tenantQ = useTenant();
  const tenant = tenantQ.data ?? null;

  // Seeded from the loaded tenant; null until then (or after Revert).
  const [form, setForm] = useState<FormState | null>(null);
  const [busy, setBusy] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);

  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  function showToast(msg: string) {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast(msg);
    toastTimer.current = setTimeout(() => setToast(null), 3000);
  }
  useEffect(
    () => () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    },
    [],
  );

  const f: FormState | null =
    form ?? (tenant ? { name: tenant.name, mcc: tenant.max_concurrent_containers } : null);

  const dirty =
    !!tenant && !!f && (f.name !== tenant.name || f.mcc !== tenant.max_concurrent_containers);
  const nameErr = f && !f.name.trim() ? 'Workspace name is required.' : '';
  const mccErr =
    f && !(Number.isInteger(f.mcc) && f.mcc >= 1)
      ? 'Must be a whole number of at least 1.'
      : '';

  async function save() {
    if (!f || !dirty || nameErr || mccErr || busy) return;
    setBusy(true);
    setSaveErr(null);
    try {
      const saved = await getApiClient().updateTenant({
        name: f.name.trim(),
        max_concurrent_containers: f.mcc,
      });
      queryClient.setQueryData(['tenant'], saved);
      setForm(null); // re-render clean from the fresh server row
      showToast('Workspace settings saved');
    } catch (e) {
      setSaveErr(
        e instanceof ApiError ? (e.body?.message ?? `HTTP ${e.status}`) : (e as Error).message,
      );
    } finally {
      setBusy(false);
    }
  }

  // The GET's 403 is the trigger for the friendly owner-only notice
  // (deep links by non-owners must not dead-end on a raw error).
  const forbidden =
    tenantQ.isError && tenantQ.error instanceof ApiError && tenantQ.error.status === 403;

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
          <h1 className="page-title">General</h1>
          <div className="page-sub">
            Workspace settings. Only the workspace owner can view or change these.
          </div>
        </div>
      </div>

      <div className="grid-scroll" style={{ paddingTop: 12 }}>
        {tenantQ.isLoading ? (
          <div className="page-sub">Loading…</div>
        ) : forbidden ? (
          <div className="grid-empty" data-testid="gen-forbidden">
            <div style={{ margin: 'auto', textAlign: 'center' }}>
              <BrandMark size={128} />
              <div style={{ marginTop: '0.75rem', fontSize: '1rem' }}>
                Only the workspace owner can view general settings.
              </div>
            </div>
          </div>
        ) : tenantQ.isError ? (
          <div className="row-error">
            Failed to load workspace settings — retry from the sidebar.
          </div>
        ) : tenant && f ? (
          <div style={{ maxWidth: 760 }}>
            <div className="field">
              <label htmlFor="gen-name">Workspace name</label>
              <input
                id="gen-name"
                type="text"
                maxLength={120}
                value={f.name}
                disabled={busy}
                onChange={(e) => setForm({ ...f, name: e.target.value })}
              />
              <div className="hint" style={nameErr ? { color: 'var(--rm-danger)' } : undefined}>
                {nameErr || 'Shown across the app and in invitations.'}
              </div>
            </div>
            <div className="field">
              <label htmlFor="gen-slug">Workspace slug</label>
              <input
                id="gen-slug"
                type="text"
                disabled
                value={tenant.slug ?? '—'}
                style={{ fontFamily: 'ui-monospace, Menlo, monospace', fontSize: 13 }}
              />
              <div className="hint">The workspace identifier — fixed at creation.</div>
            </div>
            <div style={{ display: 'flex', gap: 20 }}>
              <div className="field" style={{ flex: 1 }}>
                <label>Plan</label>
                <div style={{ padding: '6px 0' }}>
                  {tenant.plan ? (
                    <span className="pill pill-active" style={{ textTransform: 'capitalize' }}>
                      {tenant.plan}
                    </span>
                  ) : (
                    <span className="hint">—</span>
                  )}
                </div>
                <div className="hint">Plan changes are handled by the platform operator.</div>
              </div>
              <div className="field" style={{ flex: 1 }}>
                <label>Created</label>
                <div style={{ padding: '9px 0', fontSize: '0.875rem' }}>
                  {new Date(tenant.created_at).toLocaleDateString(undefined, {
                    month: 'short',
                    day: 'numeric',
                    year: 'numeric',
                  })}
                </div>
              </div>
            </div>
            <div className="field">
              <label htmlFor="gen-mcc" style={{ whiteSpace: 'nowrap' }}>
                Concurrent coworker tasks
              </label>
              <input
                id="gen-mcc"
                type="text"
                inputMode="numeric"
                style={{ width: 96, display: 'block' }}
                value={Number.isNaN(f.mcc) ? '' : String(f.mcc)}
                disabled={busy}
                onChange={(e) => {
                  const t = e.target.value.trim();
                  setForm({ ...f, mcc: /^\d+$/.test(t) ? parseInt(t, 10) : NaN });
                }}
              />
              <div className="hint" style={mccErr ? { color: 'var(--rm-danger)' } : undefined}>
                {mccErr ||
                  'How many coworker containers may run at the same time. Higher values use more compute; queued tasks wait for a free slot.'}
              </div>
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', paddingTop: 4 }}>
              <button
                className="btn-primary"
                data-testid="gen-save"
                disabled={!dirty || !!nameErr || !!mccErr || busy}
                onClick={() => void save()}
              >
                {busy ? 'Saving…' : 'Save changes'}
              </button>
              {dirty && !busy ? (
                <button className="btn-ghost" data-testid="gen-revert" onClick={() => setForm(null)}>
                  Revert
                </button>
              ) : null}
            </div>
            {saveErr ? (
              <div className="row-error" role="alert" style={{ marginTop: 8 }}>
                {saveErr}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>

      {toast ? (
        <div className="toast" role="status">
          {toast}
        </div>
      ) : null}
    </div>
  );
}
