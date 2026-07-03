// ModelsPage — read-only platform-catalog view (spec Part F). Model is
// platform-managed (writes are behind platform-only `model.manage`);
// tenant-side this page is pure visibility — no CRUD, no dialogs. Its
// job is to show what exists, what's usable, and why not.
//
// Grouping is the copied lib/models-grouping (the same helper the
// coworker wizard's model step consumes — F.4). Called WITHOUT a
// backend so every provider/model shows; inactive models are kept and
// surfaced dimmed. Behavioral reference web/src/components/models-page.ts.

import { useMemo, useState } from 'react';
import { ArrowLeft, Plus } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import type { ModelProvider } from '../../../api/client';
import { useCredentials, useModels } from '../../../api/queries';
import { groupModelsByProvider } from '../../../lib/models-grouping';
import { CredentialDialog } from '../../../components/credential-dialog';
import { ProviderGroup } from './provider-group';
import './models.css';

export function ModelsPage() {
  const navigate = useNavigate();
  const modelsQ = useModels();
  // Presence-only; a 403 for a member degrades to "no credential
  // everywhere" (the useCredentials hook already catches to []).
  const credentialsQ = useCredentials(true);

  const groups = useMemo(
    () => groupModelsByProvider(modelsQ.data ?? [], credentialsQ.data ?? []),
    [modelsQ.data, credentialsQ.data],
  );

  // D-MO1 resolved (v8): Add credential / Connect open the real
  // credential dialog in place. Header Add → provider select (null);
  // a group's Connect → pre-filled with that provider. The dialog
  // invalidates ['models'] + ['credentials'] on save, so pills flip
  // live with no extra wiring. `undefined` = closed.
  const [dialogProvider, setDialogProvider] = useState<
    ModelProvider | null | undefined
  >(undefined);

  const hasModels = (modelsQ.data?.length ?? 0) > 0;

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
          <h1 className="page-title">Models</h1>
          <div className="page-sub">
            Models are grouped by provider. A provider's models become usable once
            its credential is set.
          </div>
        </div>
        <button className="btn-primary" onClick={() => setDialogProvider(null)}>
          <Plus />
          Add credential
        </button>
      </div>
      <div className="grid-scroll" style={{ paddingTop: 12 }}>
        {modelsQ.isLoading ? (
          <div className="page-sub">Loading…</div>
        ) : modelsQ.isError ? (
          <div className="row-error">Failed to load models — retry from the sidebar.</div>
        ) : !hasModels ? (
          <div className="grid-empty">
            <div style={{ margin: 'auto', textAlign: 'center' }}>
              <div style={{ fontSize: '1rem' }}>
                No models available — the platform catalog appears empty.
              </div>
            </div>
          </div>
        ) : (
          groups.map((g) => (
            <ProviderGroup
              key={g.provider}
              group={g}
              onConnect={(provider) => setDialogProvider(provider as ModelProvider)}
            />
          ))
        )}
      </div>

      {dialogProvider !== undefined ? (
        <CredentialDialog
          provider={dialogProvider}
          onClose={() => setDialogProvider(undefined)}
        />
      ) : null}
    </div>
  );
}
