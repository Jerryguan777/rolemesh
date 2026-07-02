// AgentCard — one selectable coworker card (spec §7.3 field mapping):
// provider line = coworkerSubtitle uppercased; profile = model label;
// desc = routing_description (the wire Coworker has no `description`
// field). Skill badges are omitted in v1 (D-11: N+1 sub-resource).
// Radio semantics behind the checkbox visual (spec §7.1).

import type { Coworker, Model } from '../../../api/client';
import { coworkerSubtitle } from '../../../lib/coworker-label';

export function AgentCard({
  coworker,
  modelsById,
  selected,
  onSelect,
}: {
  coworker: Coworker;
  modelsById: Map<string, Model>;
  selected: boolean;
  onSelect: () => void;
}) {
  const model = coworker.model_id ? modelsById.get(coworker.model_id) : undefined;
  const modelLabel = model?.display_name ?? coworker.model_id ?? null;
  return (
    <div
      className={`card${selected ? ' selected' : ''}`}
      role="radio"
      aria-checked={selected}
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onSelect();
        }
      }}
    >
      <span className="cb" />
      <div className="card-content">
        <div>
          <div className="provider">{coworkerSubtitle(coworker, modelsById)}</div>
          <div className="name">{coworker.name}</div>
        </div>
        {modelLabel ? <div className="profile">Model: {modelLabel}</div> : null}
        {coworker.routing_description ? (
          <div className="desc">{coworker.routing_description}</div>
        ) : null}
      </div>
    </div>
  );
}
