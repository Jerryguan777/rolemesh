// MCPServersPage — tenant-scoped MCP registry (spec Part D). Reuses
// Part C's page chrome, manage-card family, and shared confirm dialog.
// No visibility/ownership on this resource — the whole page is gated
// by the `mcp.configure` nav capability (Part A §5.1), so no per-row
// capability split.
//
// Usage counts are client-derived (D.1, Lit parity) via the shared
// query — see useMCPUsageCounts. The delete-block is advisory; the
// backend's 409 RESOURCE_IN_USE is the authoritative gate.

import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowLeft, Plus, Search } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { getApiClient, type MCPServer } from '../../../api/client';
import { useMCPServerRegistry, useMCPUsageCounts } from '../../../api/queries';
import { ConfirmDialog } from '../../../components/confirm-dialog';
import { deleteErrText } from './delete-error';
import { MCPServerCard } from './mcp-server-card';
import { MCPServerDialog } from './mcp-server-dialog';
import './mcp-servers.css';

export function MCPServersPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const registryQ = useMCPServerRegistry();
  const usageQ = useMCPUsageCounts(true);
  const counts = usageQ.data ?? new Map<string, number>();

  const [query, setQuery] = useState('');
  const [dialog, setDialog] = useState<{ open: boolean; editing: MCPServer | null }>(
    { open: false, editing: null },
  );
  const [deleteTarget, setDeleteTarget] = useState<MCPServer | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
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

  const rows = registryQ.data ?? [];
  const q = query.trim().toLowerCase();
  const visible = useMemo(
    () =>
      rows.filter(
        (s) =>
          !q ||
          `${s.name} ${s.url} ${s.description ?? ''}`.toLowerCase().includes(q),
      ),
    [rows, q],
  );

  function refreshAfterMutation() {
    void queryClient.invalidateQueries({ queryKey: ['mcp-servers'] });
    // Bindings didn't change on create/edit, but a fresh count is cheap
    // and keeps the page authoritative after a delete elsewhere.
    void queryClient.invalidateQueries({ queryKey: ['mcp-usage-counts'] });
  }

  async function performDelete() {
    const s = deleteTarget;
    if (!s || deleteBusy) return;
    setDeleteBusy(true);
    setRowErrors((e) => ({ ...e, [s.id]: '' }));
    try {
      await getApiClient().deleteMCPServer(s.id);
      refreshAfterMutation();
      showToast(`Deleted ${s.name}`);
    } catch (err) {
      // Close on error too — the per-row line surfaces the message
      // (incl. the 409 in-use case if the advisory block was stale).
      setRowErrors((e) => ({ ...e, [s.id]: deleteErrText(err) }));
    } finally {
      setDeleteBusy(false);
      setDeleteTarget(null);
    }
  }

  const deleteBlockCount = deleteTarget ? (counts.get(deleteTarget.id) ?? 0) : 0;
  const deleteBlocked = deleteBlockCount > 0;

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
          <h1 className="page-title">MCP servers</h1>
          <div className="page-sub">
            Tenant-scoped registry — changes hot-reload to the egress gateway.
          </div>
        </div>
        <button
          className="btn-primary"
          onClick={() => setDialog({ open: true, editing: null })}
        >
          <Plus />
          New MCP server
        </button>
      </div>
      <div className="page-search">
        <div className="search-field">
          <input
            type="text"
            placeholder="Search MCP servers"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <span className="search-ic">
            <Search />
          </span>
        </div>
      </div>
      <div className="grid-scroll">
        {registryQ.isLoading ? (
          <div className="page-sub">Loading…</div>
        ) : registryQ.isError ? (
          <div className="row-error">Failed to load MCP servers — retry from the sidebar.</div>
        ) : rows.length === 0 ? (
          <div className="grid-empty">
            <div style={{ margin: 'auto', textAlign: 'center' }}>
              <div style={{ fontSize: '1rem', fontWeight: 700 }}>No MCP servers yet.</div>
              <div className="page-sub" style={{ marginTop: 4 }}>
                Register one to make its tools available to coworkers.
              </div>
              <button
                className="btn-primary"
                style={{ marginTop: '1rem' }}
                onClick={() => setDialog({ open: true, editing: null })}
              >
                New MCP server
              </button>
            </div>
          </div>
        ) : visible.length === 0 ? (
          <div className="page-sub">No MCP servers match your search.</div>
        ) : (
          <div className="masonry">
            {visible.map((s) => (
              <MCPServerCard
                key={s.id}
                server={s}
                usageCount={counts.get(s.id) ?? 0}
                rowError={rowErrors[s.id] || null}
                onEdit={() => setDialog({ open: true, editing: s })}
                onDelete={() => setDeleteTarget(s)}
              />
            ))}
          </div>
        )}
      </div>

      {dialog.open ? (
        <MCPServerDialog
          editing={dialog.editing}
          onClose={() => setDialog({ open: false, editing: null })}
          onSaved={(msg) => {
            refreshAfterMutation();
            showToast(msg);
          }}
        />
      ) : null}

      {deleteTarget ? (
        <ConfirmDialog
          title={`Delete MCP server “${deleteTarget.name}”?`}
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
              This MCP server is bound to <b>{deleteBlockCount}</b> coworker
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
