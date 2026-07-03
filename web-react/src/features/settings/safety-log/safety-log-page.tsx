// SafetyLogPage — the read-only decision log (spec Part J; behavioral
// reference web/src/components/safety-decisions-page.ts). Every check
// decision lands here, allows included. No create/edit/delete — the
// header actions are Refresh + Export CSV (ghost; a read-only page has
// no primary action).
//
// Deep links (G6): ?check_id=X pre-selects the check dropdown;
// ?rule_id=Y sets the programmatic chip (server-side triggered_rule_ids
// containment); both AND-combine. The chat approval card sends
// ?rule_id only — landing on the rule's full filtered list (newest
// first) is deliberate: seeing the pattern beats auto-opening one
// decision. This route is load-bearing for the HITL loop.
//
// Sort: created_at desc, always — no sort control. Pagination: 10/page
// offset-based; the page envelope carries total in-band.

import { useMemo, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { ArrowLeft } from 'lucide-react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { getApiClient, type SafetyDecision } from '../../../api/client';
import {
  useCoworkers,
  useSafetyChecks,
  useSafetyDecisions,
  useSafetyRules,
} from '../../../api/queries';
import { BrandMark } from '../../../components/brand-mark';
import { checkLabel } from '../../../lib/safety-catalog';
import { DecisionDetailDialog } from './decision-detail-dialog';
import { DecisionRow } from './decision-row';
import { EMPTY_FILTERS, FilterBar, type LogFilters } from './filter-bar';
import './safety-log.css';

const PAGE_SIZE = 10;

export function SafetyLogPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();

  // Deep-link params applied ONCE as initial state (Lit G6 semantics) —
  // later filter edits own the state; the URL is not two-way bound.
  const [filters, setFilters] = useState<LogFilters>(() => ({
    ...EMPTY_FILTERS,
    checkId: searchParams.get('check_id') ?? '',
    ruleId: searchParams.get('rule_id') ?? '',
  }));
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<SafetyDecision | null>(null);

  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  function showToast(msg: string) {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast(msg);
    toastTimer.current = setTimeout(() => setToast(null), 3200);
  }

  const wireFilters = useMemo(
    () => ({
      verdictAction: filters.verdict || null,
      stage: filters.stage || null,
      coworkerId: filters.coworkerId || null,
      checkId: filters.checkId || null,
      ruleId: filters.ruleId || null,
      fromTs: filters.fromTs || null,
      toTs: filters.toTs || null,
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    }),
    [filters, page],
  );
  const decisionsQ = useSafetyDecisions(wireFilters);
  const checksQ = useSafetyChecks();
  const rulesQ = useSafetyRules();
  const coworkersQ = useCoworkers();

  const coworkers = coworkersQ.data ?? [];
  const rules = rulesQ.data ?? [];
  const coworkerName = (id: string | null | undefined): string => {
    if (!id) return 'organization-wide';
    const cw = coworkers.find((c) => c.id === id);
    return cw ? cw.name : id.slice(0, 8);
  };

  // The chip label = the rule's check label (the log outlives its rules:
  // an unknown id keeps the raw id as the label).
  const chipRule = filters.ruleId
    ? (rules.find((r) => r.id === filters.ruleId) ?? null)
    : null;
  const ruleChipLabel = chipRule ? checkLabel(chipRule.check_id) : null;

  function onFiltersChange(next: LogFilters) {
    setFilters(next);
    setPage(0); // every filter change resets to page 0
  }

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['safety-decisions'] });
  }

  /** Authenticated blob download (a plain <a href> cannot carry the
   *  bearer token) with the CURRENT filters — Lit parity. */
  async function exportCsv() {
    try {
      const blob = await getApiClient().downloadSafetyDecisionsCsv(wireFilters);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `safety-log-${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      showToast((err as Error).message ?? 'export failed');
    }
  }

  const pageData = decisionsQ.data;
  const items = pageData?.items ?? [];
  const total = pageData?.total ?? 0;
  const start = total === 0 ? 0 : page * PAGE_SIZE + 1;
  const end = Math.min((page + 1) * PAGE_SIZE, total);

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
          <h1 className="page-title">Safety log</h1>
          <div className="page-sub" style={{ maxWidth: 640 }}>
            Every check decision lands here — both allows and blocks. Raw payloads
            are never stored — only a SHA-256 digest and a short summary. To
            root-cause, open the conversation around the timestamp.
          </div>
        </div>
        <span style={{ display: 'inline-flex', gap: 8 }}>
          <button className="btn-ghost" onClick={refresh}>
            Refresh
          </button>
          <button className="btn-ghost" onClick={() => void exportCsv()}>
            Export CSV
          </button>
        </span>
      </div>

      <FilterBar
        filters={filters}
        checks={checksQ.data ?? []}
        coworkers={coworkers}
        ruleChipLabel={ruleChipLabel}
        onChange={onFiltersChange}
      />

      <div className="grid-scroll">
        {decisionsQ.isLoading ? (
          <div className="page-sub">Loading…</div>
        ) : decisionsQ.isError ? (
          <div className="row-error">
            Failed to load the safety log — retry from the sidebar.
          </div>
        ) : items.length === 0 ? (
          <div className="grid-empty">
            <div style={{ margin: 'auto', textAlign: 'center' }}>
              <BrandMark size={128} />
              <div style={{ marginTop: '0.75rem', fontSize: '1rem' }}>
                No decisions match your filters.
              </div>
              <div
                style={{
                  marginTop: 4,
                  fontSize: '0.875rem',
                  color: 'var(--rm-text-muted)',
                }}
              >
                Try clearing some filters, or wait for new agent activity.
              </div>
            </div>
          </div>
        ) : (
          <>
            {items.map((d) => (
              <DecisionRow
                key={d.id}
                decision={d}
                coworkerName={coworkerName(d.coworker_id)}
                onOpen={() => setSelected(d)}
              />
            ))}
            <div className="pager" data-testid="log-pager">
              <span>
                Showing {start}–{end} of {total}
              </span>
              <span className="sp" />
              <button
                className="btn-ghost"
                disabled={page === 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
              >
                ← Previous
              </button>
              <button
                className="btn-ghost"
                disabled={(page + 1) * PAGE_SIZE >= total}
                onClick={() => setPage((p) => p + 1)}
              >
                Next →
              </button>
            </div>
          </>
        )}
      </div>

      {selected ? (
        <DecisionDetailDialog
          decision={selected}
          rules={rules}
          coworkerName={coworkerName(selected.coworker_id)}
          onClose={() => setSelected(null)}
        />
      ) : null}

      {toast ? (
        <div className="toast" role="status">
          {toast}
        </div>
      ) : null}
    </div>
  );
}
