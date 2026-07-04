// MembersPage — tenant user management (spec Part L). No Lit
// behavioral reference exists (its Members entry is a coming-soon
// stub); D-L1 (user-approved, same footing as D-K1) ships this ahead
// of the Lit SPA, bound STRICTLY to the shipped wire (/api/v1/users).
// Reconcile per the behavior-parity rule when the Lit page lands.
//
// Access is the `user.manage` capability (owner + admin), enforced by
// the route-level <Gated>. Two handler business rules shape the rows:
// you cannot delete yourself (400) — mirrored by HIDING Remove on the
// caller's own row (a dead button loses to no button, the Part I §8.5
// principle) — and only owners grant the owner role (the dialog
// mirrors that one; see member-dialog.tsx).
//
// Row width joins the D-UI1 760px unification (the v13 prototype's
// inline 680px cap is superseded — one cap for every row-list page).

import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Pencil, Plus, Trash2 } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { ApiError, getApiClient, type UserResponse } from '../../../api/client';
import { useUsers } from '../../../api/queries';
import { currentMe } from '../../../lib/capabilities';
import { ConfirmDialog } from '../../../components/confirm-dialog';
import { MemberDialog } from './member-dialog';

const PAGE_SIZE = 20;

export function MembersPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const me = currentMe();

  const [page, setPage] = useState(0);
  const usersQ = useUsers(page * PAGE_SIZE, PAGE_SIZE);

  /** null = closed; { target: null } = Add member. */
  const [dialog, setDialog] = useState<{ target: UserResponse | null } | null>(null);
  const [removing, setRemoving] = useState<UserResponse | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);
  const [removeErr, setRemoveErr] = useState<string | null>(null);

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

  async function confirmRemove() {
    if (!removing || removeBusy) return;
    setRemoveBusy(true);
    setRemoveErr(null);
    try {
      await getApiClient().deleteUser(removing.id);
      // Removing the last row of a later page would land on an empty
      // page — step back before the refetch.
      if ((usersQ.data?.items.length ?? 0) === 1 && page > 0) setPage(page - 1);
      await queryClient.invalidateQueries({ queryKey: ['users'] });
      showToast(`Removed ${removing.name}`);
      setRemoving(null);
    } catch (e) {
      setRemoveErr(
        e instanceof ApiError ? (e.body?.message ?? `HTTP ${e.status}`) : (e as Error).message,
      );
    } finally {
      setRemoveBusy(false);
    }
  }

  const items = usersQ.data?.items ?? [];
  const total = usersQ.data?.total ?? 0;
  const start = total === 0 ? 0 : page * PAGE_SIZE + 1;
  const end = Math.min(total, page * PAGE_SIZE + items.length);

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
          <h1 className="page-title">Members</h1>
          <div className="page-sub" style={{ maxWidth: 640 }}>
            People in this workspace and their roles. Owners and admins can manage
            members; only owners can grant the owner role.
          </div>
        </div>
        <button
          className="btn-primary"
          data-testid="mem-add"
          onClick={() => setDialog({ target: null })}
        >
          <Plus />
          Add member
        </button>
      </div>

      <div className="grid-scroll" style={{ paddingTop: 12 }}>
        {usersQ.isLoading ? (
          <div className="page-sub">Loading…</div>
        ) : usersQ.isError ? (
          <div className="row-error">Failed to load members — retry from the sidebar.</div>
        ) : (
          <>
            {items.map((u) => (
              <div className="model-row" key={u.id} data-testid={`mem-row-${u.id}`}>
                <span style={{ minWidth: 0 }}>
                  <div
                    className="m-name"
                    style={{ display: 'flex', alignItems: 'center', gap: 6 }}
                  >
                    {u.name}
                    {u.id === me?.user_id ? <span className="you-tag">You</span> : null}
                    {Object.keys(u.channel_ids ?? {}).map((ch) => (
                      <span className="chan-tag" key={ch}>
                        {ch} linked
                      </span>
                    ))}
                  </div>
                  <div className="m-sub" style={{ fontFamily: 'var(--font-base)' }}>
                    {u.email || 'no email'}
                  </div>
                </span>
                <span className="m-fill" />
                <span className={`role-pill role-pill--${u.role}`}>{u.role}</span>
                <span className="icon-acts">
                  <button
                    className="icon-btn"
                    title="Edit member"
                    onClick={() => setDialog({ target: u })}
                  >
                    <Pencil />
                  </button>
                  {u.id === me?.user_id ? null : (
                    <button
                      className="icon-btn danger"
                      title="Remove from workspace"
                      onClick={() => {
                        setRemoveErr(null);
                        setRemoving(u);
                      }}
                    >
                      <Trash2 />
                    </button>
                  )}
                </span>
              </div>
            ))}
            {total > PAGE_SIZE ? (
              <div className="pager" style={{ maxWidth: 760 }} data-testid="mem-pager">
                <span>
                  Showing {start}–{end} of {total}
                </span>
                <span className="sp" />
                <button
                  className="btn-ghost"
                  disabled={page === 0}
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                >
                  ← Previous
                </button>
                <button
                  className="btn-ghost"
                  disabled={(page + 1) * PAGE_SIZE >= total}
                  onClick={() => setPage((p) => p + 1)}
                >
                  Next →
                </button>
              </div>
            ) : null}
          </>
        )}
      </div>

      {dialog ? (
        <MemberDialog
          target={dialog.target}
          onClose={() => setDialog(null)}
          onSaved={showToast}
        />
      ) : null}

      {removing ? (
        <ConfirmDialog
          title={`Remove ${removing.name} from the workspace?`}
          confirmLabel="Remove member"
          busyLabel="Removing…"
          busy={removeBusy}
          onConfirm={() => void confirmRemove()}
          onCancel={() => {
            if (!removeBusy) setRemoving(null);
          }}
        >
          <p>
            {removing.name} loses access immediately. Coworkers, skills, and other
            resources they created remain in the workspace.
          </p>
          {removing.role === 'owner' ? (
            <p>
              <b>{removing.name} is an owner.</b> Make sure another owner remains —
              the server does not block removing the last one.
            </p>
          ) : null}
          {removeErr ? (
            <p className="row-error" role="alert">
              {removeErr}
            </p>
          ) : null}
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
