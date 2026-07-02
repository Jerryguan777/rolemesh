// Relative-time labels for the chat timestamp rail and the recall
// panel (spec §6.3). Semantics follow web/'s approval-card
// `relativeTime` plus the `N seconds ago` band the design reference
// shows. The message list refreshes these on a single 30s interval.

const MINUTE = 60_000;
const HOUR = 3_600_000;
const DAY = 86_400_000;

/** Format `iso` relative to `now` (defaults to Date.now()). Bands:
 *  just now · N seconds ago · Nm ago · Nh ago · yesterday ·
 *  N days ago · last week · locale date beyond 14 days. */
export function relativeTime(iso: string, now: number = Date.now()): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return '';
  const d = Math.max(0, now - then);
  if (d < 10_000) return 'just now';
  if (d < MINUTE) return `${Math.floor(d / 1000)} seconds ago`;
  if (d < HOUR) return `${Math.floor(d / MINUTE)}m ago`;
  if (d < DAY) return `${Math.floor(d / HOUR)}h ago`;
  if (d < 2 * DAY) return 'yesterday';
  if (d < 7 * DAY) return `${Math.floor(d / DAY)} days ago`;
  if (d < 14 * DAY) return 'last week';
  return new Date(then).toLocaleDateString();
}
