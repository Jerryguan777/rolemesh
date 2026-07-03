// MCPServerDialog — create AND edit in one component (spec D.2;
// behavioral reference web/src/components/mcp-server-dialog.ts). No
// wizard — a single 560px form. Fields: name, transport (inline
// option-cards http/sse), url (monospace), auth mode (select + dynamic
// hint), description (optional).
//
// extra_headers / tool_reversibility are API-only (D-M1) — the Lit
// dialog defers them too. Submit gates on name + url non-empty;
// type/auth_mode always carry defaults (http/service) so the looser
// gate still satisfies the wire-required set.

import { useEffect, useState } from 'react';
import { X } from 'lucide-react';
import {
  ApiError,
  getApiClient,
  type MCPServer,
  type MCPServerCreate,
  type MCPType,
} from '../../../api/client';
import { BrandMark } from '../../../components/brand-mark';
import { AUTH_MODES, authModeDescription } from './auth-modes';

interface DialogForm {
  name: string;
  type: MCPType;
  url: string;
  auth_mode: MCPServerCreate['auth_mode'];
  description: string;
}

function formFromServer(s: MCPServer): DialogForm {
  return {
    name: s.name,
    type: s.type,
    url: s.url,
    auth_mode: s.auth_mode,
    description: s.description ?? '',
  };
}

const EMPTY: DialogForm = {
  name: '',
  type: 'http',
  url: '',
  auth_mode: 'service',
  description: '',
};

const TRANSPORTS: MCPType[] = ['http', 'sse'];

export function MCPServerDialog({
  editing,
  onClose,
  onSaved,
}: {
  editing: MCPServer | null;
  onClose: () => void;
  /** Fired on success with a toast line; parent refreshes the list. */
  onSaved: (toast: string) => void;
}) {
  const isEdit = editing !== null;
  const [form, setForm] = useState<DialogForm>(() =>
    editing ? formFromServer(editing) : EMPTY,
  );
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

  const canSubmit = form.name.trim() !== '' && form.url.trim() !== '' && !busy;

  async function submit() {
    if (!canSubmit) return;
    setBusy(true);
    setErr(null);
    const body = {
      name: form.name.trim(),
      type: form.type,
      url: form.url.trim(),
      auth_mode: form.auth_mode,
      description: form.description.trim() || null,
    };
    try {
      const api = getApiClient();
      const saved = isEdit
        ? await api.updateMCPServer(editing.id, body)
        : await api.createMCPServer(body);
      onSaved(
        isEdit
          ? `Saved changes to ${saved.name}`
          : `Registered ${saved.name} — egress gateway hot-reloaded`,
      );
      onClose();
    } catch (e) {
      setErr(
        e instanceof ApiError ? (e.body?.message ?? `${e.status}`) : (e as Error).message,
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
      <div className="dlg mcp" role="dialog" aria-modal="true" aria-label="MCP server">
        <div className="dlg-header">
          <div className="hleft">
            <div className="dlg-brand-icon">
              <BrandMark size={16} />
            </div>
            <h2 className="dlg-title">
              {isEdit ? `Edit ${editing.name}` : 'New MCP server'}
            </h2>
          </div>
          <button className="icon-btn" aria-label="Close" disabled={busy} onClick={onClose}>
            <X />
          </button>
        </div>
        <div className="wiz-body">
          <div className="field">
            <label htmlFor="mcp-name">Name</label>
            <input
              id="mcp-name"
              type="text"
              maxLength={200}
              placeholder="e.g. records-mcp"
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            />
          </div>
          <div className="field">
            <label>Transport</label>
            <div className="transport-row">
              {TRANSPORTS.map((t) => (
                <button
                  key={t}
                  className={`opt-card${form.type === t ? ' selected' : ''}`}
                  onClick={() => setForm((f) => ({ ...f, type: t }))}
                >
                  <div className="t">{t}</div>
                </button>
              ))}
            </div>
          </div>
          <div className="field">
            <label htmlFor="mcp-url">URL</label>
            <input
              id="mcp-url"
              type="text"
              className="mono"
              placeholder="http://records-mcp.rolemesh-system.svc:8080/mcp"
              value={form.url}
              onChange={(e) => setForm((f) => ({ ...f, url: e.target.value }))}
            />
            <div className="hint">
              Every call is routed through the egress gateway and credential proxy —
              the URL must be reachable from the cluster, not from your browser.
            </div>
          </div>
          <div className="field">
            <label htmlFor="mcp-auth">Auth mode</label>
            <select
              id="mcp-auth"
              value={form.auth_mode}
              onChange={(e) =>
                setForm((f) => ({
                  ...f,
                  auth_mode: e.target.value as DialogForm['auth_mode'],
                }))
              }
            >
              {AUTH_MODES.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </select>
            <div className="hint">{authModeDescription(form.auth_mode)}</div>
          </div>
          <div className="field">
            <label htmlFor="mcp-desc">
              Description{' '}
              <span style={{ fontWeight: 400, color: 'var(--rm-text-muted)' }}>
                (optional)
              </span>
            </label>
            <textarea
              id="mcp-desc"
              style={{ minHeight: 64 }}
              value={form.description}
              onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
            />
          </div>
        </div>
        <div className="wiz-foot">
          {err ? <span className="wiz-err" role="alert">{err}</span> : <span />}
          <span className="actions">
            <button className="btn-ghost" disabled={busy} onClick={onClose}>
              Cancel
            </button>
            <button className="btn-primary" disabled={!canSubmit} onClick={() => void submit()}>
              {busy ? (isEdit ? 'Saving…' : 'Creating…') : isEdit ? 'Save changes' : 'Create'}
            </button>
          </span>
        </div>
      </div>
    </div>
  );
}
