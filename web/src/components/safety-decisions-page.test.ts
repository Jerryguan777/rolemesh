// @vitest-environment happy-dom
// Safety decisions page — pins the v1 read split.
//
// Reads (list + detail) must go through the typed v1 ApiClient.
// CSV export stays on admin (CSV not on v1 per design §3 Phase 4).

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const {
  listDecisionsSpy,
  getDecisionSpy,
  getTenantIdSpy,
  listCoworkersSpy,
} = vi.hoisted(() => ({
  listDecisionsSpy: vi.fn(),
  getDecisionSpy: vi.fn(),
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

async function waitUntilLoaded(page: SafetyDecisionsPage): Promise<void> {
  for (let i = 0; i < 30; i++) {
    await Promise.resolve();
    await page.updateComplete;
    // @ts-expect-error — touching private state.
    if (page.loading === false) return;
  }
  throw new Error('SafetyDecisionsPage did not finish loading');
}

function makeDecision(overrides: Record<string, unknown> = {}) {
  return {
    id: 'd1',
    tenant_id: 't1',
    coworker_id: null,
    conversation_id: null,
    job_id: null,
    stage: 'input_prompt' as const,
    verdict_action: 'allow' as const,
    triggered_rule_ids: [],
    findings: [],
    context_digest: 'x'.repeat(16),
    context_summary: '',
    created_at: '2026-05-21T00:00:00Z',
    ...overrides,
  };
}

describe('SafetyDecisionsPage routing', () => {
  beforeEach(() => {
    listDecisionsSpy.mockResolvedValue({ total: 0, items: [] });
    getDecisionSpy.mockResolvedValue(makeDecision());
    getTenantIdSpy.mockResolvedValue('t1');
    listCoworkersSpy.mockResolvedValue([]);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('list call routes to v1 ApiClient with pagination + filters', async () => {
    const page = new SafetyDecisionsPage();
    document.body.appendChild(page);
    await waitUntilLoaded(page);

    expect(listDecisionsSpy).toHaveBeenCalledTimes(1);
    const args = listDecisionsSpy.mock.calls[0][0];
    expect(args.limit).toBe(25);
    expect(args.offset).toBe(0);
    document.body.removeChild(page);
  });

  it('detail call routes to v1 ApiClient', async () => {
    const row = makeDecision({ id: 'click-target' });
    listDecisionsSpy.mockResolvedValue({ total: 1, items: [row] });
    const page = new SafetyDecisionsPage();
    document.body.appendChild(page);
    await waitUntilLoaded(page);

    // @ts-expect-error — invoking private method directly.
    await page.openDetail(row);

    expect(getDecisionSpy).toHaveBeenCalledTimes(1);
    expect(getDecisionSpy).toHaveBeenCalledWith('click-target');
    document.body.removeChild(page);
  });

  it('loads the page even when admin tenant lookup fails', async () => {
    // CSV is admin-only; a transient admin failure must not block
    // the v1 read path. The connectedCallback swallows tenant_id
    // errors so reads keep flowing.
    getTenantIdSpy.mockRejectedValue(new Error('admin offline'));
    const page = new SafetyDecisionsPage();
    document.body.appendChild(page);
    await waitUntilLoaded(page);

    expect(listDecisionsSpy).toHaveBeenCalledTimes(1);
    document.body.removeChild(page);
  });
});
