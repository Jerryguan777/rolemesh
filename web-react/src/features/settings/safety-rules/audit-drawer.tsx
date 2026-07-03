// AuditDrawer — per-rule change history (spec I.4; behavioral reference
// web/src/components/safety-rules-page.ts renderAuditDrawer). Available
// on platform cards too — orgs can see when defaults changed. Entries
// reverse-chronological: [VERB] by {actor} {date} + a client-diffed
// summary (audit-summary.ts — the wire ships state snapshots, no server
// summary string). Actor is the wire's actor_user_id (or "the system");
// the prototype's friendly names are mock sugar we don't invent.

import { useEffect, useState } from 'react';
import { X } from 'lucide-react';
import {
  getApiClient,
  type SafetyRule,
  type SafetyRuleAuditEntry,
} from '../../../api/client';
import { BrandMark } from '../../../components/brand-mark';
import { auditSummary } from './audit-summary';
import { checkLabel } from '../../../lib/safety-catalog';

export function AuditDrawer({
  rule,
  onClose,
}: {
  rule: SafetyRule;
  onClose: () => void;
}) {
  const [entries, setEntries] = useState<SafetyRuleAuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setErr(null);
    getApiClient()
      .listSafetyRuleAudit(rule.id)
      .then((rows) => {
        if (!alive) return;
        // Reverse-chronological (§6.9) — belt-and-braces re-sort.
        setEntries(
          [...rows].sort(
            (a, b) => Date.parse(b.created_at) - Date.parse(a.created_at),
          ),
        );
      })
      .catch((e) => {
        if (alive) setErr((e as Error).message ?? 'failed to load history');
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [rule.id]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      onClose();
    }
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [onClose]);

  return (
    <div
      className="scrim"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="dlg"
        style={{ width: 520 }}
        role="dialog"
        aria-modal="true"
        aria-label="Rule history"
      >
        <div className="dlg-header">
          <div className="hleft">
            <div className="dlg-brand-icon">
              <BrandMark size={16} />
            </div>
            <h2 className="dlg-title">Change history — {checkLabel(rule.check_id)}</h2>
          </div>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            <X />
          </button>
        </div>
        <div className="wiz-body" style={{ minHeight: 0 }}>
          {loading ? (
            <div className="hint">Loading…</div>
          ) : err ? (
            <div className="row-error">{err}</div>
          ) : entries.length === 0 ? (
            <div className="hint">No change history yet.</div>
          ) : (
            entries.map((e) => (
              <div className="audit-row" key={e.id}>
                <span className={`audit-verb audit-verb--${e.action}`}>
                  {e.action.toUpperCase()}
                </span>
                <span style={{ minWidth: 0 }}>
                  <span className="who">by {e.actor_user_id ?? 'the system'}</span>
                  <div className="diff">{auditSummary(e)}</div>
                </span>
                <span className="when">{new Date(e.created_at).toLocaleString()}</span>
              </div>
            ))
          )}
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
