// Approval display helpers — lifted from the exported pure functions of
// web/src/components/approvals-inbox.ts (spec O.4 / parent Appendix C.2)
// so the React inbox and card share one countdown/urgency vocabulary.
// Framework-free; ported with the Lit unit tests.

/** Countdown turns urgent (deep red badge / red countdown) under this
 *  many ms remaining (§4.1/§4.3) — one threshold for card AND inbox. */
export const URGENT_MS = 5 * 60 * 1000;

/** Join up to the first 4 param entries as `k: v · k: v · …`, each value
 *  stringified and truncated to 30 chars (§4.4). A missing or non-object
 *  `params` (or an empty object) collapses to '' so the row omits the
 *  line. The inbox's "decide whether to open" hint — the CARD never
 *  truncates a primitive (a truncated amount is dangerous). */
export function paramsInline(params: unknown): string {
  if (!params || typeof params !== 'object' || Array.isArray(params)) return '';
  const entries = Object.entries(params as Record<string, unknown>);
  if (entries.length === 0) return '';
  return entries
    .slice(0, 4)
    .map(([k, v]) => {
      let val = typeof v === 'string' ? v : (JSON.stringify(v) ?? String(v));
      if (val.length > 30) val = val.slice(0, 30) + '…';
      return `${k}: ${val}`;
    })
    .join(' · ');
}

/** Countdown text from an ISO `expires_at` against `now` (epoch ms):
 *  `Xm left` / `Xs left` / `expired`. An unparseable / missing timestamp
 *  yields '' so the row drops it. */
export function formatCountdown(expiresAt: string | null, now: number): string {
  if (!expiresAt) return '';
  const exp = Date.parse(expiresAt);
  if (Number.isNaN(exp)) return '';
  const ms = exp - now;
  if (ms <= 0) return 'expired';
  const totalSec = Math.floor(ms / 1000);
  if (totalSec < 60) return `${totalSec}s left`;
  return `${Math.floor(totalSec / 60)}m left`;
}

/** Is this request within the urgent window (or already past expiry)?
 *  Strict `<` at the boundary (the Lit test pins it). */
export function isUrgent(expiresAt: string | null, now: number): boolean {
  if (!expiresAt) return false;
  const exp = Date.parse(expiresAt);
  if (Number.isNaN(exp)) return false;
  return exp - now < URGENT_MS;
}
