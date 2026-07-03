// CoworkersPage — the manage surface (spec Part C §C.1): list + search
// + card grid + wizard + delete confirm. Replaces the stub route per
// the §1.1 settings growth rule. Behavioral reference:
// web/src/components/coworkers-page.ts.
//
// Mutations invalidate the ['coworkers'] query — the SAME key the chat
// picker reads, so a created coworker appears in the picker with no
// extra wiring (spec §C.6).

import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowLeft, Plus, Search } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  getApiClient,
  type Coworker,
} from '../../../api/client';
import { useCoworkers, useModels } from '../../../api/queries';
import { BrandMark } from '../../../components/brand-mark';
import { ConfirmDialog } from '../../../components/confirm-dialog';
import { hasCapability } from '../../../lib/capabilities';
import { modelsByIdMap } from '../../../lib/coworker-label';
import { CoworkerCard } from './coworker-card';
import { CoworkerWizard } from './coworker-wizard';
import './coworkers.css';

function errText(err: unknown): string {
  return err instanceof ApiError
    ? `${err.status} — ${err.body?.message ?? err.message}`
    : ((err as Error).message ?? 'request failed');
}

export function CoworkersPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const coworkersQ = useCoworkers();
  const modelsQ = useModels();
  const modelsById = useMemo(
    () => modelsByIdMap(modelsQ.data ?? []),
    [modelsQ.data],
  );

  const [query, setQuery] = useState('');
  const [wizard, setWizard] = useState<{ open: boolean; editing: Coworker | null }>(
    { open: false, editing: null },
  );
  const [deleteTarget, setDeleteTarget] = useState<Coworker | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [shareBusy, setShareBusy] = useState<ReadonlySet<string>>(new Set());
  // Per-row error line for delete/share failures — errors stay on the
  // row, never a toast-only surface (spec §C.1).
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
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

  const canCreate = hasCapability('coworker.create');
  const rows = coworkersQ.data ?? [];
  const q = query.trim().toLowerCase();
  const visible = rows.filter(
    (c) => !q || `${c.name} ${c.system_prompt ?? ''}`.toLowerCase().includes(q),
  );

  function openChat(c: Coworker) {
    // chat reads ?agent_id from the real location.search — same URL
    // contract (and same full-navigation pattern) as the Lit page.
    location.href = `${location.pathname}?agent_id=${encodeURIComponent(c.id)}#/`;
  }

  async function toggleShare(c: Coworker) {
    if (shareBusy.has(c.id)) return;
    setShareBusy((s) => new Set(s).add(c.id));
    setRowErrors((e) => ({ ...e, [c.id]: '' }));
    try {
      const api = getApiClient();
      const updated =
        c.visibility === 'shared'
          ? await api.unshareCoworker(c.id)
          : await api.shareCoworker(c.id);
      // Patch the row in place so the pill + tooltip flip without a
      // full refetch (Lit's optimistic-from-response pattern).
      queryClient.setQueryData<Coworker[]>(['coworkers'], (cur) =>
        (cur ?? []).map((r) => (r.id === updated.id ? updated : r)),
      );
      showToast(
        updated.visibility === 'shared'
          ? `${updated.name} is now shared with the workspace`
          : `${updated.name} is now private`,
      );
    } catch (err) {
      setRowErrors((e) => ({ ...e, [c.id]: errText(err) }));
    } finally {
      setShareBusy((s) => {
        const next = new Set(s);
        next.delete(c.id);
        return next;
      });
    }
  }

  async function performDelete() {
    const c = deleteTarget;
    if (!c || deleteBusy) return;
    setDeleteBusy(true);
    setRowErrors((e) => ({ ...e, [c.id]: '' }));
    try {
      await getApiClient().deleteCoworker(c.id);
      await queryClient.invalidateQueries({ queryKey: ['coworkers'] });
      showToast(`Deleted ${c.name}`);
    } catch (err) {
      // Close on error too — the per-row line surfaces the message;
      // leaving the modal up would trap the user under it.
      setRowErrors((e) => ({ ...e, [c.id]: errText(err) }));
    } finally {
      setDeleteBusy(false);
      setDeleteTarget(null);
    }
  }

  function onWizardSaved(toastMsg: string | null) {
    void queryClient.invalidateQueries({ queryKey: ['coworkers'] });
    if (toastMsg) showToast(toastMsg);
  }

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
          <h1 className="page-title">Coworkers</h1>
          <div className="page-sub">
            Create and manage the agents your workspace can chat with.
          </div>
        </div>
        {canCreate ? (
          <button
            className="btn-primary"
            onClick={() => setWizard({ open: true, editing: null })}
          >
            <Plus />
            New coworker
          </button>
        ) : null}
      </div>
      <div className="page-search">
        <div className="search-field">
          <input
            type="text"
            placeholder="Search coworkers"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <span className="search-ic">
            <Search />
          </span>
        </div>
      </div>
      <div className="grid-scroll">
        {coworkersQ.isLoading ? (
          <div className="page-sub">Loading…</div>
        ) : coworkersQ.isError ? (
          <div className="row-error">Failed to load coworkers — retry from the sidebar.</div>
        ) : rows.length === 0 ? (
          <div className="grid-empty">
            <div style={{ margin: 'auto', textAlign: 'center' }}>
              <BrandMark size={128} />
              <div style={{ marginTop: '0.75rem', fontSize: '1rem' }}>
                No coworkers yet.
              </div>
              {canCreate ? (
                <button
                  className="btn-primary"
                  style={{ marginTop: '1rem' }}
                  onClick={() => setWizard({ open: true, editing: null })}
                >
                  New coworker
                </button>
              ) : null}
            </div>
          </div>
        ) : visible.length === 0 ? (
          <div className="page-sub">No coworkers match your search.</div>
        ) : (
          <div className="masonry">
            {visible.map((c) => (
              <CoworkerCard
                key={c.id}
                coworker={c}
                modelsById={modelsById}
                shareBusy={shareBusy.has(c.id)}
                rowError={rowErrors[c.id] || null}
                onOpenChat={() => openChat(c)}
                onToggleShare={() => void toggleShare(c)}
                onEdit={() => setWizard({ open: true, editing: c })}
                onDelete={() => setDeleteTarget(c)}
              />
            ))}
          </div>
        )}
      </div>

      {wizard.open ? (
        <CoworkerWizard
          editing={wizard.editing}
          onClose={() => setWizard({ open: false, editing: null })}
          onSaved={onWizardSaved}
        />
      ) : null}

      {deleteTarget ? (
        <ConfirmDialog
          title={`Delete coworker “${deleteTarget.name}”?`}
          confirmLabel="Delete"
          busyLabel="Deleting…"
          busy={deleteBusy}
          onConfirm={() => void performDelete()}
          onCancel={() => {
            if (!deleteBusy) setDeleteTarget(null);
          }}
        >
          This permanently removes <b>{deleteTarget.name}</b> and unbinds its MCP
          servers and skills. This can't be undone.
        </ConfirmDialog>
      ) : null}

      {toast ? <div className="toast">{toast}</div> : null}
    </div>
  );
}
