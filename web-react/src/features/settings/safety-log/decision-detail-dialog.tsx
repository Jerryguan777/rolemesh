// DecisionDetailDialog — read-only decision inspection (spec J.3;
// behavioral reference web/src/components/safety-decision-detail-dialog.ts,
// visuals per the v11 prototype). Metadata grid + platform-rule chip
// (wire `source` surfaced) + triggered rules (ids referencing deleted
// rules render "deleted rule" — the log outlives its rules) + findings
// blocks + the data-minimization note anchored on the decision's own
// timestamp. Renders entirely from the list row — the wire page items
// already carry full findings; GET /decisions/{id} has no consumer.

import { useEffect } from 'react';
import { X } from 'lucide-react';
import type { SafetyDecision, SafetyRule } from '../../../api/client';
import { BrandMark } from '../../../components/brand-mark';
import { SAF_ACTION_LABEL, checkLabel } from '../../../lib/safety-catalog';

export function DecisionDetailDialog({
  decision,
  rules,
  coworkerName,
  onClose,
}: {
  decision: SafetyDecision;
  /** Part I's rules query — resolves triggered_rule_ids to check labels. */
  rules: SafetyRule[];
  coworkerName: string;
  onClose: () => void;
}) {
  const d = decision;

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      onClose();
    }
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [onClose]);

  const when = new Date(d.created_at);
  const hhmmss = d.created_at.slice(11, 19);
  const triggered = d.triggered_rule_ids ?? [];

  return (
    <div
      className="scrim"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="dlg"
        style={{ width: 600 }}
        role="dialog"
        aria-modal="true"
        aria-label="Safety decision"
      >
        <div className="dlg-header">
          <div className="hleft">
            <div className="dlg-brand-icon">
              <BrandMark size={16} />
            </div>
            <h2 className="dlg-title">Decision {d.id}</h2>
          </div>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            <X />
          </button>
        </div>
        <div className="wiz-body" style={{ minHeight: 0 }}>
          <dl className="meta-grid">
            <dt>When</dt>
            <dd>
              {when.toLocaleDateString(undefined, {
                month: 'short',
                day: 'numeric',
                year: 'numeric',
              })}{' '}
              {hhmmss}
            </dd>
            <dt>Verdict</dt>
            <dd>
              <span className={`saf-act saf-act--${d.verdict_action}`}>
                {SAF_ACTION_LABEL[d.verdict_action] ?? d.verdict_action}
              </span>
              {d.source === 'platform' ? (
                <span className="saf-chip" style={{ marginLeft: 6 }}>
                  platform rule
                </span>
              ) : null}
            </dd>
            <dt>Stage</dt>
            <dd className="mono" style={{ fontSize: '12.5px' }}>
              {d.stage}
            </dd>
            <dt>Coworker</dt>
            <dd>{coworkerName}</dd>
            <dt>Triggered rule</dt>
            <dd>
              {triggered.length ? (
                triggered.map((id, i) => {
                  const rule = rules.find((r) => r.id === id);
                  return (
                    <span key={id}>
                      {i > 0 ? ', ' : ''}
                      {rule ? checkLabel(rule.check_id) : 'deleted rule'}{' '}
                      <span style={{ color: 'var(--rm-text-muted)' }}>({id})</span>
                    </span>
                  );
                })
              ) : (
                <span style={{ color: 'var(--rm-text-muted)' }}>—</span>
              )}
            </dd>
            <dt>Context digest</dt>
            <dd
              className="mono"
              style={{ fontSize: 12, color: 'var(--rm-text-muted)' }}
            >
              {d.context_digest}
            </dd>
            <dt>Summary</dt>
            <dd>{d.context_summary}</dd>
          </dl>

          <div className="findings-title">Findings</div>
          {(d.findings ?? []).length ? (
            (d.findings ?? []).map((f, i) => (
              <div className="finding" key={i}>
                <div className="f-head">
                  <span className="f-code">{f.code}</span>
                  <span className={`sev sev--${f.severity}`}>{f.severity}</span>
                </div>
                <div style={{ fontSize: '0.875rem' }}>{f.message}</div>
                {f.metadata ? (
                  <div className="f-meta">{JSON.stringify(f.metadata)}</div>
                ) : null}
              </div>
            ))
          ) : (
            <div className="hint" style={{ marginBottom: 12 }}>
              No findings — check ran and verdict was <b>allow</b>.
            </div>
          )}

          <div className="dm-note" style={{ marginTop: 14 }}>
            <b>Data minimization</b> — the raw payload is not stored; only the
            SHA-256 digest above and the short summary. To investigate root cause,
            open the conversation around {hhmmss}.
          </div>
        </div>
        <div className="wiz-foot">
          <span />
          <button className="btn-ghost" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
