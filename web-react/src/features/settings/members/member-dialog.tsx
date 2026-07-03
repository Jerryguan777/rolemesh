// MemberDialog — add + edit share this one 480px dialog (spec L.3).
//
// Role rules — mirror, don't own: the page gate is the `user.manage`
// capability, but "only owners grant the owner role" has NO capability
// on the wire (it's a handler business rule keyed to the caller's
// role). We read Me.role solely to MIRROR that server invariant for
// display — owner option disabled + "(owners only)" + hint — never as
// a client-side authorization decision; the POST/PATCH 403 stays
// authoritative and surfaces in the footer if the mirror is ever
// stale. Editing yourself to a lower role is server-allowed, so it
// gets an advisory, not a block.
//
// Self-contained mutation (the credential-dialog pattern): owns the
// POST/PATCH, invalidates ['users'], and hands the toast copy to the
// page via onSaved.

import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { X } from 'lucide-react';
import {
  ApiError,
  getApiClient,
  type UserResponse,
} from '../../../api/client';
import { currentMe } from '../../../lib/capabilities';
import { BrandMark } from '../../../components/brand-mark';

type Role = 'member' | 'admin' | 'owner';
const ROLES: Role[] = ['member', 'admin', 'owner'];

export function MemberDialog({
  target,
  onClose,
  onSaved,
}: {
  /** Row being edited, or null for Add member. */
  target: UserResponse | null;
  onClose: () => void;
  /** Toast hook — fires after the write lands and the list refreshes. */
  onSaved: (message: string) => void;
}) {
  const queryClient = useQueryClient();
  const me = currentMe();
  const editing = !!target;
  const isOwnerCaller = me?.role === 'owner';
  const editingSelf = editing && target.id === me?.user_id;

  const [name, setName] = useState(target?.name ?? '');
  const [email, setEmail] = useState(target?.email ?? '');
  const [role, setRole] = useState<Role>((target?.role as Role) ?? 'member');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      if (!busy) onClose();
    }
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [busy, onClose]);

  const roleHint = !isOwnerCaller
    ? 'Only owners can grant the owner role — the server enforces this.'
    : editingSelf && role !== 'owner'
      ? 'Lowering your own role may remove your access to this page.'
      : 'Owners and admins can manage members and workspace resources.';

  async function save() {
    if (busy || !name.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const api = getApiClient();
      if (editing) {
        // PATCH sends email verbatim — "" is the wire's clear-it signal
        // (omit/null would leave the stored value unchanged).
        await api.updateUser(target.id, {
          name: name.trim(),
          email: email.trim(),
          role,
        });
      } else {
        await api.createUser({
          name: name.trim(),
          email: email.trim() || null,
          role,
        });
      }
      await queryClient.invalidateQueries({ queryKey: ['users'] });
      onSaved(editing ? 'Member updated' : `Added ${name.trim()}`);
      onClose();
    } catch (e) {
      setErr(
        e instanceof ApiError ? (e.body?.message ?? `HTTP ${e.status}`) : (e as Error).message,
      );
      setBusy(false);
    }
  }

  return (
    <div
      className="scrim"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div
        className="dlg"
        style={{ width: 480 }}
        role="dialog"
        aria-modal="true"
        aria-label="Member"
      >
        <div className="dlg-header">
          <div className="hleft">
            <div className="dlg-brand-icon">
              <BrandMark size={16} />
            </div>
            <h2 className="dlg-title">{editing ? `Edit ${target.name}` : 'Add member'}</h2>
          </div>
          <button className="icon-btn" aria-label="Close" disabled={busy} onClick={onClose}>
            <X />
          </button>
        </div>
        <div className="wiz-body" style={{ minHeight: 0 }}>
          <div className="field">
            <label htmlFor="mem-name">Name</label>
            <input
              id="mem-name"
              type="text"
              maxLength={200}
              value={name}
              disabled={busy}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="mem-email">
              Email{' '}
              <span style={{ fontWeight: 400, color: 'var(--rm-text-muted)' }}>optional</span>
            </label>
            <input
              id="mem-email"
              type="text"
              placeholder="name@company.com"
              value={email}
              disabled={busy}
              onChange={(e) => setEmail(e.target.value)}
            />
            {editing ? (
              <div className="hint">Clearing this field removes the stored email.</div>
            ) : null}
          </div>
          <div className="field">
            <label htmlFor="mem-role">Role</label>
            <select
              id="mem-role"
              value={role}
              disabled={busy}
              onChange={(e) => setRole(e.target.value as Role)}
            >
              {ROLES.map((r) => (
                <option key={r} value={r} disabled={r === 'owner' && !isOwnerCaller}>
                  {r === 'owner' && !isOwnerCaller ? 'owner (owners only)' : r}
                </option>
              ))}
            </select>
            <div className="hint">{roleHint}</div>
          </div>
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
              data-testid="mem-save"
              disabled={busy || !name.trim()}
              onClick={() => void save()}
            >
              {busy ? 'Saving…' : editing ? 'Save changes' : 'Add member'}
            </button>
          </span>
        </div>
      </div>
    </div>
  );
}
