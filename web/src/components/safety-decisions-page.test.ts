// @vitest-environment happy-dom
// Safety log page (spec §7) — pins the v1 read split + the revamped list.
//
// Reads (list + detail + rules-for-labels) go through the typed v1 ApiClient;
// CSV export stays on admin (not on v1 per design §3 Phase 4). Also pins the
// filter set: verdict / stage / coworker — NO check dropdown (the v1 endpoint
// has no check_id filter and we don't extend the contract).

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const {
  listDecisionsSpy,
  getDecisionSpy,
  listRulesSpy,
  getTenantIdSpy,
  listCoworkersSpy,
} = vi.hoisted(() => ({
  listDecisionsSpy: vi.fn(),
  getDecisionSpy: vi.fn(),
  listRulesSpy: vi.fn(),
  getTenantIdSpy: vi.fn(),
  listCoworkersSpy: vi.fn(),
}));

vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  return {
    ...actual,
    getApiClient: () => ({
      listSafetyDecisions: listDecisionsSpy,
      getSafetyDecision: getDecisionSpy,
      listSafetyRules: listRulesSpy,
    }),
  };
});

vi.mock('../services/safety-admin-client.js', async () => {
  const actual = await vi.importActual<
    typeof import('../services/safety-admin-client.js')
  >('../services/safety-admin-client.js');
  return {
    ...actual,
    getTenantId: getTenantIdSpy,
    listCoworkers: listCoworkersSpy,
  };
});

import { SafetyDecisionsPage } from './safety-decisions-page.js';
import type { SafetyDecision } from '../api/client.js';

async function waitUntilLoaded(page: SafetyDecisionsPage): Promise<void> {
  for (let i = 0; i < 30; i++) {
    await Promise.resolve();
    await page.updateComplete;
    // @ts-expect-error — touching private state.
    if (page.loading === false) return;
  }
  throw new Error('SafetyDecisionsPage did not finish loading');
}

function makeDecision(overrides: Partial<SafetyDecision> = {}): SafetyDecision {
  return {
    id: 'd1',
    tenant_id: 't1',
    coworker_id: null,
    conversation_id: null,
    job_id: null,
    stage: 'input_prompt',
    verdict_action: 'allow',
    triggered_rule_ids: [],
    findings: [],
    context_digest: 'x'.repeat(16),
    context_summary: '',
    source: 'tenant',
    created_at: '2026-05-21T00:00:00Z',
    ...overrides,
  } as SafetyDecision;
}

async function mount(items: SafetyDecision[] = [], total?: number): Promise<SafetyDecisionsPage> {
  listDecisionsSpy.mockResolvedValue({ total: total ?? items.length, items });
  const page = new SafetyDecisionsPage();
  document.body.appendChild(page);
  await waitUntilLoaded(page);
  await page.updateComplete;
  return page;
}

describe('SafetyDecisionsPage', () => {
  beforeEach(() => {
    listDecisionsSpy.mockResolvedValue({ total: 0, items: [] });
    getDecisionSpy.mockResolvedValue(makeDecision());
    listRulesSpy.mockResolvedValue([]);
    getTenantIdSpy.mockResolvedValue('t1');
    listCoworkersSpy.mockResolvedValue([{ id: 'ops', name: 'Ops coworker' }]);
  });

  afterEach(() => {
    vi.clearAllMocks();
    document.body.innerHTML = '';
  });

  it('list call routes to v1 with page size 10 + offset 0', async () => {
    const page = await mount([]);
    expect(listDecisionsSpy).toHaveBeenCalledTimes(1);
    const args = listDecisionsSpy.mock.calls[0][0];
    expect(args.limit).toBe(10);
    expect(args.offset).toBe(0);
    page.remove();
  });

  it('loads even when the admin tenant lookup fails (CSV is admin-only)', async () => {
    getTenantIdSpy.mockRejectedValue(new Error('admin offline'));
    const page = await mount([]);
    expect(listDecisionsSpy).toHaveBeenCalledTimes(1);
    page.remove();
  });

  it('renders the empty state when no decisions match', async () => {
    const page = await mount([]);
    expect(page.querySelector('[data-testid="saf-log-empty"]')).not.toBeNull();
    page.remove();
  });

  it('renders a row per decision with the verdict + finding codes', async () => {
    const page = await mount([
      makeDecision({
        id: 'd2',
        verdict_action: 'block',
        stage: 'pre_tool_call',
        context_summary: 'tool args contained SSN',
        findings: [{ code: 'PII.SSN', severity: 'high', message: 'm' }],
      }),
    ]);
    const rows = page.querySelectorAll('[data-testid="saf-log-row"]');
    expect(rows.length).toBe(1);
    expect(rows[0].querySelector('.rm-saf-verdict')?.textContent?.trim()).toBe('block');
    expect(rows[0].querySelector('.rm-saf-check')?.textContent).toContain('PII.SSN');
    expect(rows[0].querySelector('.rm-saf-summary')?.textContent).toContain(
      'tool args contained SSN',
    );
    page.remove();
  });

  it('exposes verdict / stage / coworker filters but NOT a check filter', async () => {
    const page = await mount([]);
    expect(page.querySelector('[data-testid="saf-filter-verdict"]')).not.toBeNull();
    expect(page.querySelector('[data-testid="saf-filter-stage"]')).not.toBeNull();
    expect(page.querySelector('[data-testid="saf-filter-coworker"]')).not.toBeNull();
    // The v1 endpoint has no check_id filter — the 4th dropdown is omitted.
    expect(page.querySelector('[data-testid="saf-filter-check"]')).toBeNull();
    page.remove();
  });

  it('changing a filter refetches with the param and resets to page 0', async () => {
    const page = await mount([makeDecision()], 50);
    // advance a page first so we can prove the filter resets offset
    // @ts-expect-error private
    page.next();
    await page.updateComplete;
    await Promise.resolve();
    const verdict = page.querySelector('[data-testid="saf-filter-verdict"]') as HTMLSelectElement;
    verdict.value = 'block';
    verdict.dispatchEvent(new Event('change'));
    await page.updateComplete;
    await Promise.resolve();
    const lastArgs = listDecisionsSpy.mock.calls.at(-1)![0];
    expect(lastArgs.verdictAction).toBe('block');
    expect(lastArgs.offset).toBe(0);
    page.remove();
  });

  it('clear filters resets everything and refetches', async () => {
    const page = await mount([makeDecision()], 5);
    const verdict = page.querySelector('[data-testid="saf-filter-verdict"]') as HTMLSelectElement;
    verdict.value = 'block';
    verdict.dispatchEvent(new Event('change'));
    await page.updateComplete;
    (page.querySelector('[data-testid="saf-clear-filters"]') as HTMLButtonElement).click();
    await page.updateComplete;
    await Promise.resolve();
    const lastArgs = listDecisionsSpy.mock.calls.at(-1)![0];
    expect(lastArgs.verdictAction).toBeUndefined();
    page.remove();
  });

  it('clicking a row opens the detail via the v1 ApiClient', async () => {
    const row = makeDecision({ id: 'click-target' });
    const page = await mount([row]);
    (page.querySelector('[data-testid="saf-log-row"]') as HTMLButtonElement).click();
    await page.updateComplete;
    await Promise.resolve();
    await page.updateComplete;
    expect(getDecisionSpy).toHaveBeenCalledWith('click-target');
    expect(page.querySelector('rm-safety-decision-detail-dialog')).not.toBeNull();
    page.remove();
  });

  it('builds a rule_id → check-label map from the loaded rules', async () => {
    listRulesSpy.mockResolvedValue([
      { id: 'sr-1', check_id: 'pii.regex', stage: 'pre_tool_call', config: {}, priority: 100, enabled: true, tenant_id: 't', coworker_id: null, description: '', created_at: '', updated_at: '', source: 'tenant', tier: null, editable: true },
    ]);
    const page = await mount([]);
    // @ts-expect-error — private state assertion
    expect(page.ruleLabels['sr-1']).toBe('Personal data (regex)');
    page.remove();
  });
});
