// ProviderGroup — one provider's models (spec F.2). Read-only. The
// group header shows credential state (credential set / no credential +
// Connect); each model row shows a status pill and dims when inactive
// OR uncredentialed. Status logic mirrors the Lit renderModelCard:
//   dim  = !is_active || !hasCredential
//   pill = inactive (warn) | ready (success) | needs credential (gray)

import type { Model } from '../../../api/client';
import type { ProviderGroup as ProviderGroupData } from '../../../lib/models-grouping';

function ModelRow({ model, hasCredential }: { model: Model; hasCredential: boolean }) {
  const dim = !model.is_active || !hasCredential;
  return (
    <div className={`model-row${dim ? ' dim' : ''}`}>
      <span>
        <div className="m-name">{model.display_name}</div>
        <div className="m-sub">
          {model.model_id} · {model.model_family}
        </div>
      </span>
      <span className="m-fill" />
      {!model.is_active ? (
        <span className="pill pill-paused">inactive</span>
      ) : hasCredential ? (
        <span className="pill pill-active">ready</span>
      ) : (
        <span className="pill pill-disabled">needs credential</span>
      )}
    </div>
  );
}

export function ProviderGroup({
  group,
  onConnect,
}: {
  group: ProviderGroupData;
  onConnect: (provider: string) => void;
}) {
  return (
    <div className="prov-group">
      <div className="prov-head">
        <b>{group.provider}</b>
        {group.hasCredential ? (
          <span className="pill pill-active">credential set</span>
        ) : (
          <>
            <span className="pill pill-disabled">no credential</span>
            <button className="btn-ghost" onClick={() => onConnect(group.provider)}>
              Connect
            </button>
          </>
        )}
      </div>
      {group.models.map((m) => (
        <ModelRow key={m.id} model={m} hasCredential={group.hasCredential} />
      ))}
    </div>
  );
}
