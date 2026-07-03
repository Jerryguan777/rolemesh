// FilterBar — four dropdowns + the programmatic rule_id chip + the
// D-J1 time-range quick-select (spec J.2). The dropdowns are the source
// of truth for their own state (no duplicate chips); rule_id is NEVER a
// dropdown (rule lists unbounded) — deep-link only, rendered as a
// removable chip whose × clears only rule_id. `Clear filters` resets
// everything, chip included. Every change resets to page 0 (the page
// owns that — every callback here funnels through onChange).
//
// D-J1 (user-approved): time range is a quick-select (24h/7d/30d) that
// writes from_ts; `Custom` reveals the datetime-local pair (the shipped
// Lit control) for arbitrary ranges. Same wire semantics either way.

import type {
  SafetyCheck,
  SafetyStage,
  SafetyVerdictAction,
} from '../../../api/client';
import {
  SAF_ACTION_LABEL,
  SAF_ACTION_ORDER,
  SAF_STAGE_SHORT,
  checkLabel,
} from '../../../lib/safety-catalog';

export type QuickRange = '' | '24h' | '7d' | '30d' | 'custom';

export interface LogFilters {
  verdict: SafetyVerdictAction | '';
  stage: SafetyStage | '';
  coworkerId: string;
  checkId: string;
  /** Deep-link-only chip filter. */
  ruleId: string;
  range: QuickRange;
  /** ISO bounds — derived from `range` presets or the custom inputs. */
  fromTs: string;
  toTs: string;
}

export const EMPTY_FILTERS: LogFilters = {
  verdict: '',
  stage: '',
  coworkerId: '',
  checkId: '',
  ruleId: '',
  range: '',
  fromTs: '',
  toTs: '',
};

const RANGE_MS: Record<string, number> = {
  '24h': 24 * 3600_000,
  '7d': 7 * 24 * 3600_000,
  '30d': 30 * 24 * 3600_000,
};

export function FilterBar({
  filters,
  checks,
  coworkers,
  ruleChipLabel,
  onChange,
}: {
  filters: LogFilters;
  checks: SafetyCheck[];
  coworkers: { id: string; name: string }[];
  /** Resolved check label for the rule chip (rule_id → its check). */
  ruleChipLabel: string | null;
  onChange: (next: LogFilters) => void;
}) {
  const set = (patch: Partial<LogFilters>) => onChange({ ...filters, ...patch });

  function onRange(range: QuickRange) {
    if (range === '' ) {
      set({ range, fromTs: '', toTs: '' });
    } else if (range === 'custom') {
      // Keep any existing bounds; the inputs below take over.
      set({ range });
    } else {
      // Preset computed ONCE when picked (not per render — a stable
      // query key; Refresh refetches with the same window).
      set({ range, fromTs: new Date(Date.now() - RANGE_MS[range]).toISOString(), toTs: '' });
    }
  }

  const anyActive =
    filters.verdict || filters.stage || filters.coworkerId || filters.checkId ||
    filters.ruleId || filters.range;

  return (
    <div className="log-bar" data-testid="log-filter-bar">
      {filters.ruleId ? (
        <span className="rule-chip" data-testid="rule-chip">
          🛡 Rule: {ruleChipLabel ?? filters.ruleId}
          <button
            title="Clear rule filter"
            aria-label="Clear rule filter"
            onClick={() => set({ ruleId: '' })}
          >
            ×
          </button>
        </span>
      ) : null}
      <select
        aria-label="Verdict filter"
        value={filters.verdict}
        onChange={(e) => set({ verdict: e.target.value as LogFilters['verdict'] })}
      >
        <option value="">all verdicts</option>
        {SAF_ACTION_ORDER.map((a) => (
          <option key={a} value={a}>
            {SAF_ACTION_LABEL[a]}
          </option>
        ))}
      </select>
      <select
        aria-label="Stage filter"
        value={filters.stage}
        onChange={(e) => set({ stage: e.target.value as LogFilters['stage'] })}
      >
        <option value="">all stages</option>
        {Object.keys(SAF_STAGE_SHORT).map((s) => (
          <option key={s} value={s}>
            {s}
          </option>
        ))}
      </select>
      <select
        aria-label="Coworker filter"
        value={filters.coworkerId}
        onChange={(e) => set({ coworkerId: e.target.value })}
      >
        <option value="">all coworkers</option>
        {coworkers.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </select>
      <select
        aria-label="Check filter"
        value={filters.checkId}
        onChange={(e) => set({ checkId: e.target.value })}
      >
        <option value="">all checks</option>
        {checks.map((c) => (
          <option key={c.id} value={c.id}>
            {checkLabel(c.id)}
          </option>
        ))}
      </select>
      <select
        aria-label="Time range"
        data-testid="log-range"
        value={filters.range}
        onChange={(e) => onRange(e.target.value as QuickRange)}
      >
        <option value="">all time</option>
        <option value="24h">last 24h</option>
        <option value="7d">last 7 days</option>
        <option value="30d">last 30 days</option>
        <option value="custom">custom…</option>
      </select>
      {filters.range === 'custom' ? (
        <>
          <input
            type="datetime-local"
            aria-label="From"
            data-testid="log-from"
            value={filters.fromTs ? filters.fromTs.slice(0, 16) : ''}
            onChange={(e) =>
              set({ fromTs: e.target.value ? new Date(e.target.value).toISOString() : '' })
            }
          />
          <input
            type="datetime-local"
            aria-label="To"
            data-testid="log-to"
            value={filters.toTs ? filters.toTs.slice(0, 16) : ''}
            onChange={(e) =>
              set({ toTs: e.target.value ? new Date(e.target.value).toISOString() : '' })
            }
          />
        </>
      ) : null}
      {anyActive ? (
        <button className="btn-ghost" style={{ padding: '4px 8px' }} onClick={() => onChange({ ...EMPTY_FILTERS })}>
          Clear filters
        </button>
      ) : null}
    </div>
  );
}
