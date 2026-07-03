// SkillsPage — tenant-wide skill catalog (spec Part E). Chip-filtered
// (not searched — Lit parity); create/edit via one dialog; share;
// binding-aware delete. Behavioral reference web/src/components/
// skills-page.ts.
//
// Mutations invalidate ['skills'] — the same key the coworker wizard's
// step-5 catalogue reads, so a created skill appears there for binding
// with no extra wiring (spec E.4).

import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowLeft, Plus } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { ApiError, getApiClient, type SkillSummary } from '../../../api/client';
import { useSkillRegistry } from '../../../api/queries';
import { ConfirmDialog } from '../../../components/confirm-dialog';
import { hasCapability } from '../../../lib/capabilities';
import { SkillCard } from './skill-card';
import { SkillDialog } from './skill-dialog';
import {
  chipEmptyCopy,
  chipMatches,
  SKILL_CHIPS,
  type SkillChip,
} from './skill-chips';
import './skills.css';

function errText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 409 && err.body?.details) {
      const ids = (err.body.details as Record<string, unknown>).coworker_ids;
      if (Array.isArray(ids)) {
        return `In use by ${ids.length} coworker${ids.length === 1 ? '' : 's'} — unbind it from each before deleting.`;
      }
    }
    return err.body?.message ?? `${err.status}`;
  }
  return (err as Error).message ?? 'request failed';
}

export function SkillsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const registryQ = useSkillRegistry();

  const [chip, setChip] = useState<SkillChip>('all');
  const [dialog, setDialog] = useState<{ open: boolean; editing: SkillSummary | null }>(
    { open: false, editing: null },
  );
  const [deleteTarget, setDeleteTarget] = useState<SkillSummary | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [shareBusy, setShareBusy] = useState<ReadonlySet<string>>(new Set());
  const [deleteErrors, setDeleteErrors] = useState<Record<string, string>>({});
  const [shareErrors, setShareErrors] = useState<Record<string, string>>({});
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

  const canCreate = hasCapability('skill.create');
  const rows = registryQ.data ?? [];
  const visible = useMemo(() => rows.filter((s) => chipMatches(chip, s)), [rows, chip]);

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['skills'] });
  }

  async function toggleShare(s: SkillSummary) {
    if (shareBusy.has(s.id)) return;
    setShareBusy((b) => new Set(b).add(s.id));
    setShareErrors((e) => ({ ...e, [s.id]: '' }));
    try {
      const api = getApiClient();
      const updated =
        s.visibility === 'shared' ? await api.unshareSkill(s.id) : await api.shareSkill(s.id);
      refresh();
      showToast(
        updated.visibility === 'shared'
          ? `${updated.name} is now shared with the workspace`
          : `${updated.name} is now private`,
      );
    } catch (err) {
      setShareErrors((e) => ({ ...e, [s.id]: errText(err) }));
    } finally {
      setShareBusy((b) => {
        const next = new Set(b);
        next.delete(s.id);
        return next;
      });
    }
  }

  async function performDelete() {
    const s = deleteTarget;
    if (!s || deleteBusy) return;
    setDeleteBusy(true);
    setDeleteErrors((e) => ({ ...e, [s.id]: '' }));
    try {
      await getApiClient().deleteSkill(s.id);
      refresh();
      showToast(`Deleted ${s.name}`);
    } catch (err) {
      setDeleteErrors((e) => ({ ...e, [s.id]: errText(err) }));
    } finally {
      setDeleteBusy(false);
      setDeleteTarget(null);
    }
  }

  const deleteBlockCount = deleteTarget?.bound_coworker_count ?? 0;
  const deleteBlocked = deleteBlockCount > 0;

  function newSkillBtn(key: string) {
    return (
      <button
        key={key}
        className="btn-primary"
        onClick={() => setDialog({ open: true, editing: null })}
      >
        <Plus />
        New skill
      </button>
    );
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
          <h1 className="page-title">Skills</h1>
          <div className="page-sub">
            Tenant-wide catalog. Bind a skill to a coworker in the coworker wizard.
          </div>
        </div>
        {canCreate ? newSkillBtn('header') : null}
      </div>

      {rows.length > 0 ? (
        <div className="chips">
          {SKILL_CHIPS.map((c) => (
            <button
              key={c.id}
              className={`chip${chip === c.id ? ' on' : ''}`}
              onClick={() => setChip(c.id)}
            >
              {c.label}
            </button>
          ))}
        </div>
      ) : null}

      <div className="grid-scroll">
        {registryQ.isLoading ? (
          <div className="page-sub">Loading…</div>
        ) : registryQ.isError ? (
          <div className="row-error">Failed to load skills — retry from the sidebar.</div>
        ) : visible.length === 0 ? (
          <div className="grid-empty">
            <div style={{ margin: 'auto', textAlign: 'center' }}>
              <div style={{ fontSize: '1rem', fontWeight: 700 }}>
                {chipEmptyCopy(chip, rows.length > 0)}
              </div>
              {/* Offer create only where creating makes sense (not on the
                  "shared by others" chip). */}
              {canCreate && chip !== 'shared' ? (
                <div style={{ marginTop: '1rem' }}>{newSkillBtn('empty')}</div>
              ) : null}
            </div>
          </div>
        ) : (
          <div className="masonry">
            {visible.map((s) => (
              <SkillCard
                key={s.id}
                skill={s}
                shareBusy={shareBusy.has(s.id)}
                deleteError={deleteErrors[s.id] || null}
                shareError={shareErrors[s.id] || null}
                onOpen={() => setDialog({ open: true, editing: s })}
                onToggleShare={() => void toggleShare(s)}
                onEdit={() => setDialog({ open: true, editing: s })}
                onDelete={() => setDeleteTarget(s)}
              />
            ))}
          </div>
        )}
      </div>

      {dialog.open ? (
        <SkillDialog
          editing={dialog.editing}
          onClose={() => setDialog({ open: false, editing: null })}
          onSaved={(msg) => {
            refresh();
            showToast(msg);
          }}
        />
      ) : null}

      {deleteTarget ? (
        <ConfirmDialog
          title={`Delete skill “${deleteTarget.name}”?`}
          confirmLabel="Delete"
          busyLabel="Deleting…"
          busy={deleteBusy}
          disableConfirm={deleteBlocked}
          onConfirm={() => void performDelete()}
          onCancel={() => {
            if (!deleteBusy) setDeleteTarget(null);
          }}
        >
          {deleteBlocked ? (
            <>
              This skill is bound to <b>{deleteBlockCount}</b> coworker
              {deleteBlockCount === 1 ? '' : 's'}. Unbind it from{' '}
              {deleteBlockCount === 1 ? 'that coworker' : 'each one'} before deleting.
            </>
          ) : (
            <>This cannot be undone.</>
          )}
        </ConfirmDialog>
      ) : null}

      {toast ? <div className="toast">{toast}</div> : null}
    </div>
  );
}
