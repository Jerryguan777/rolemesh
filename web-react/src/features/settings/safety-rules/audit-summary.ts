// Ported from web/src/components/safety-rules-page.ts auditSummary @
// feat/webui-react. Human one-line summary of an audit entry (§6.9).
// The wire ships before_state / after_state snapshots — there is NO
// server-provided summary field (the v10 spec's I.4 claim is inverted;
// the Lit source documents the actual contract) — so we diff the fields
// an operator cares about. Kept deliberately small + deterministic.

import type { SafetyRuleAuditEntry } from '../../../api/client';
import { checkLabel } from '../../../lib/safety-catalog';

export function auditSummary(entry: SafetyRuleAuditEntry): string {
  if (entry.action === 'created') {
    const a = entry.after_state ?? {};
    const check = checkLabel(String(a['check_id'] ?? ''));
    return `Created — ${check}${a['stage'] ? `, ${String(a['stage'])}` : ''}`;
  }
  if (entry.action === 'deleted') return 'Deleted';
  const before = entry.before_state ?? {};
  const after = entry.after_state ?? {};
  const fields = ['priority', 'enabled', 'stage'];
  const parts: string[] = [];
  for (const f of fields) {
    if (JSON.stringify(before[f]) !== JSON.stringify(after[f])) {
      parts.push(`${f}: ${fmtVal(before[f])} → ${fmtVal(after[f])}`);
    }
  }
  // action_override lives inside config; surface it specifically.
  const bOv = (before['config'] as Record<string, unknown> | undefined)?.['action_override'];
  const aOv = (after['config'] as Record<string, unknown> | undefined)?.['action_override'];
  if (JSON.stringify(bOv) !== JSON.stringify(aOv)) {
    parts.push(`action: ${fmtVal(bOv ?? 'default')} → ${fmtVal(aOv ?? 'default')}`);
  }
  return parts.length ? parts.join('; ') : 'Configuration updated';
}

function fmtVal(v: unknown): string {
  if (v === true) return 'on';
  if (v === false) return 'off';
  return String(v);
}
