// CredentialsPage — tenant LLM credentials (spec Part G; behavioral
// reference web/src/components/credentials-page.ts). FOUR fixed
// provider rows (the PROVIDERS constant — the page's job is showing
// providers that are NOT configured yet), each with set/missing state,
// a rotate/add pencil, and a delete (only when set).
//
// The whole page is gated by the `credential.byok.manage` nav
// capability (app/nav.ts) — like the Lit page there is no per-row
// ownership split. The secret is structurally absent from the wire, so
// nothing is ever displayed back.
//
// Delete has NO client-side pre-block (nothing on the wire counts
// consumers); the backend's 409 RESOURCE_IN_USE (details.coworker_ids)
// is authoritative and surfaces on the row — spec G.4 corrected.

import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Pencil, Plus, Trash2 } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import type { ModelProvider } from '../../../api/client';
import { getApiClient } from '../../../api/client';
import { useCredentials } from '../../../api/queries';
import { CredentialDialog } from '../../../components/credential-dialog';
import { ConfirmDialog } from '../../../components/confirm-dialog';
import { PROVIDERS, schemaFor } from '../../../components/provider-schemas';
import { credDeleteErrText } from './delete-error';
import './credentials.css';

function fmtDate(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleDateString();
  } catch {
    return iso;
  }
}

export function CredentialsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const credentialsQ = useCredentials(true);

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

  // `null` provider = the header Add flow (dialog shows its select);
  // a provider string = row pencil (locked, no select). `undefined` =
  // closed. Distinguished from null so we cannot collapse the two.
  const [dialogProvider, setDialogProvider] = useState<
    ModelProvider | null | undefined
  >(undefined);
  const [deleteTarget, setDeleteTarget] = useState<ModelProvider | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});

  const rows = credentialsQ.data ?? [];
  const credFor = (p: ModelProvider) => rows.find((c) => c.provider === p) ?? null;

  async function performDelete() {
    const p = deleteTarget;
    if (!p || deleteBusy) return;
    setDeleteBusy(true);
    setRowErrors((e) => ({ ...e, [p]: '' }));
    try {
      await getApiClient().deleteCredential(p);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['credentials'] }),
        queryClient.invalidateQueries({ queryKey: ['models'] }),
      ]);
      showToast(`${schemaFor(p).label} credential deleted`);
      setDeleteTarget(null);
    } catch (err) {
      // Close on error too — the per-row line carries the message,
      // including the authoritative 409-in-use case.
      setRowErrors((e) => ({ ...e, [p]: credDeleteErrText(err) }));
      setDeleteTarget(null);
    } finally {
      setDeleteBusy(false);
    }
  }

  const deleteLabel = deleteTarget ? schemaFor(deleteTarget).label : '';

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
          <h1 className="page-title">Credentials</h1>
          <div className="page-sub">
            One credential per provider. Keys are envelope-encrypted server-side and
            never displayed back.
          </div>
        </div>
        <button className="btn-primary" onClick={() => setDialogProvider(null)}>
          <Plus />
          Add credential
        </button>
      </div>

      <div className="grid-scroll" style={{ paddingTop: 12 }}>
        {credentialsQ.isLoading ? (
          <div className="page-sub">Loading…</div>
        ) : credentialsQ.isError ? (
          <div className="row-error">
            Failed to load credentials — retry from the sidebar.
          </div>
        ) : (
          PROVIDERS.map((p) => {
            const ex = credFor(p);
            const err = rowErrors[p];
            return (
              <div key={p}>
                <div className="model-row cred-row">
                  <span>
                    <div className="m-name" style={{ textTransform: 'capitalize' }}>
                      {schemaFor(p).label}
                    </div>
                    <div className="m-sub cred-sub">
                      {ex
                        ? `set ${fmtDate(ex.updated_at)}`
                        : 'not configured — coworkers using this provider cannot run'}
                    </div>
                  </span>
                  <span className="m-fill" />
                  {ex ? (
                    <span className="pill pill-active">set</span>
                  ) : (
                    <span className="pill pill-paused">missing</span>
                  )}
                  <span className="icon-acts">
                    <button
                      className="icon-btn"
                      title={ex ? 'Rotate credential' : 'Add credential'}
                      aria-label={ex ? 'Rotate credential' : 'Add credential'}
                      onClick={() => setDialogProvider(p)}
                    >
                      <Pencil />
                    </button>
                    {ex ? (
                      <button
                        className="icon-btn danger"
                        title="Delete credential"
                        aria-label="Delete credential"
                        onClick={() => setDeleteTarget(p)}
                      >
                        <Trash2 />
                      </button>
                    ) : null}
                  </span>
                </div>
                {err ? <div className="row-error cred-err">{err}</div> : null}
              </div>
            );
          })
        )}
      </div>

      {dialogProvider !== undefined ? (
        <CredentialDialog
          provider={dialogProvider}
          onClose={() => setDialogProvider(undefined)}
          onSaved={(prov) => showToast(`${schemaFor(prov).label} credential saved`)}
        />
      ) : null}

      {deleteTarget ? (
        <ConfirmDialog
          title={`Delete ${deleteLabel} credential?`}
          confirmLabel="Delete"
          busyLabel="Deleting…"
          busy={deleteBusy}
          onConfirm={() => void performDelete()}
          onCancel={() => {
            if (!deleteBusy) setDeleteTarget(null);
          }}
        >
          Coworkers using <b>{deleteLabel}</b> models will stop running until a new
          credential is set. This can’t be undone.
        </ConfirmDialog>
      ) : null}

      {toast ? <div className="toast">{toast}</div> : null}
    </div>
  );
}
