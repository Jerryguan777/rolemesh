// DecisionRow — one log entry (spec J.2). 7-column grid, whole row
// clickable: HH:MM:SS mono timestamp · verdict pill · mono stage ·
// mono finding codes · truncating summary · coworker name · chevron.
//
// The 4th column: the wire SafetyDecision carries NO check_id — the
// finding codes are the wire's signal of what the decision caught
// (Lit renderRow documents this; the v11 prototype's mock check_id
// column is a prototype liberty, corrected to source).

import type { SafetyDecision } from '../../../api/client';
import { SAF_ACTION_LABEL } from '../../../lib/safety-catalog';

export function findingCodes(d: SafetyDecision): string {
  return (d.findings ?? []).map((f) => f.code).join(', ') || '—';
}

export function DecisionRow({
  decision,
  coworkerName,
  onOpen,
}: {
  decision: SafetyDecision;
  /** Resolved name; `organization-wide` when the decision is unscoped. */
  coworkerName: string;
  onOpen: () => void;
}) {
  const d = decision;
  return (
    <div
      className="log-row"
      role="button"
      tabIndex={0}
      data-decision-id={d.id}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') onOpen();
      }}
    >
      <span className="ts">{d.created_at.slice(11, 19)}</span>
      <span
        className={`saf-act saf-act--${d.verdict_action}`}
        style={{ textAlign: 'center' }}
      >
        {SAF_ACTION_LABEL[d.verdict_action] ?? d.verdict_action}
      </span>
      <span className="tech">{d.stage}</span>
      <span className="tech" title={findingCodes(d)}>
        {findingCodes(d)}
      </span>
      <span className="sum" title={d.context_summary}>
        {d.context_summary}
      </span>
      <span className="cw">{coworkerName}</span>
      <span className="arrow">›</span>
    </div>
  );
}
