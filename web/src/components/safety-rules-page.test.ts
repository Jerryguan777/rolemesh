// @vitest-environment happy-dom
// Safety rules page — pins the v1/admin read-vs-write split.
//
// What this test catches:
//   * Reads on mount go through the typed v1 ApiClient
//     (listSafetyRules / listSafetyChecks / listSafetyRuleAudit).
//   * Writes (create/update/delete + toggle) keep using
//     safety-admin-client (admin POST/PATCH/DELETE).
//   * A refactor that "helpfully" repointed a write to v1 would
//     blow up at runtime (no v1 write endpoint exists), so we
//     pin the routing here.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Hoisted spies so the factories below (which run before
// top-level `const`s) can reference them.
const {
  listSafetyRulesSpy,
  listSafetyChecksSpy,
  listSafetyRuleAuditSpy,
  createRuleSpy,
  updateRuleSpy,
  deleteRuleSpy,
  listCoworkersSpy,
} = vi.hoisted(() => ({
  listSafetyRulesSpy: vi.fn(),
  listSafetyChecksSpy: vi.fn(),
  listSafetyRuleAuditSpy: vi.fn(),
  createRuleSpy: vi.fn(),
  updateRuleSpy: vi.fn(),
  deleteRuleSpy: vi.fn(),
  listCoworkersSpy: vi.fn(),
}));

vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  return {
    ...actual,
    getApiClient: () => ({
      listSafetyRules: listSafetyRulesSpy,
      listSafetyChecks: listSafetyChecksSpy,
      listSafetyRuleAudit: listSafetyRuleAuditSpy,
    }),
  };
});

vi.mock('../services/safety-admin-client.js', async () => {
  const actual = await vi.importActual<
    typeof import('../services/safety-admin-client.js')
  >('../services/safety-admin-client.js');
  return {
    ...actual,
    createRule: createRuleSpy,
    updateRule: updateRuleSpy,
    deleteRule: deleteRuleSpy,
    listCoworkers: listCoworkersSpy,
  };
});

import { SafetyRulesPage } from './safety-rules-page.js';

async function waitUntilLoaded(page: SafetyRulesPage): Promise<void> {
  for (let i = 0; i < 30; i++) {
    await Promise.resolve();
    await page.updateComplete;
    // @ts-expect-error — touching private state for assertion
    if (page.loading === false) return;
  }
  throw new Error('SafetyRulesPage did not finish loading');
}

function makeRule(overrides: Record<string, unknown> = {}) {
  return {
    id: 'r1',
    tenant_id: 't1',
    coworker_id: null,
    stage: 'input_prompt' as const,
    check_id: 'prompt-pii',
    config: {},
    priority: 100,
    enabled: true,
    description: '',
    created_at: '2026-05-21T00:00:00Z',
    updated_at: '2026-05-21T00:00:00Z',
    ...overrides,
  };
}

describe('SafetyRulesPage routing', () => {
  beforeEach(() => {
    listSafetyRulesSpy.mockResolvedValue([]);
    listSafetyChecksSpy.mockResolvedValue([]);
    listSafetyRuleAuditSpy.mockResolvedValue([]);
    createRuleSpy.mockResolvedValue(makeRule());
    updateRuleSpy.mockResolvedValue(makeRule({ enabled: false }));
    deleteRuleSpy.mockResolvedValue(undefined);
    listCoworkersSpy.mockResolvedValue([]);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('reads via the typed v1 ApiClient on mount', async () => {
    const page = new SafetyRulesPage();
    document.body.appendChild(page);
    await waitUntilLoaded(page);

    expect(listSafetyRulesSpy).toHaveBeenCalledTimes(1);
    expect(listSafetyChecksSpy).toHaveBeenCalledTimes(1);
    // Coworker list is admin-side (CSV export + sidebar share it);
    // the v1 read split deliberately doesn't reach for it.
    expect(listCoworkersSpy).toHaveBeenCalledTimes(1);
    document.body.removeChild(page);
  });

  it('audit timeline call routes to the v1 client', async () => {
    const rule = makeRule();
    listSafetyRulesSpy.mockResolvedValue([rule]);
    const page = new SafetyRulesPage();
    document.body.appendChild(page);
    await waitUntilLoaded(page);

    // @ts-expect-error — invoking private method directly to pin
    //                    the routing without spinning up a click.
    await page.openDetail(rule);

    expect(listSafetyRuleAuditSpy).toHaveBeenCalledTimes(1);
    expect(listSafetyRuleAuditSpy).toHaveBeenCalledWith('r1');
    document.body.removeChild(page);
  });

  it('toggleEnabled write goes through admin updateRule, NOT v1', async () => {
    const rule = makeRule({ enabled: true });
    listSafetyRulesSpy.mockResolvedValue([rule]);
    const page = new SafetyRulesPage();
    document.body.appendChild(page);
    await waitUntilLoaded(page);

    // @ts-expect-error — exercise the write handler directly.
    await page.toggleEnabled(rule);

    expect(updateRuleSpy).toHaveBeenCalledTimes(1);
    expect(updateRuleSpy).toHaveBeenCalledWith('r1', { enabled: false });
    // Critically: no v1-write call exists. If a refactor wires a
    // write to the v1 client, this assertion would still pass —
    // but the runtime would 404. The negative side is structural
    // (no v1 write method on ApiClient).
    document.body.removeChild(page);
  });

  it('delete uses admin deleteRule', async () => {
    const rule = makeRule();
    listSafetyRulesSpy.mockResolvedValue([rule]);
    const page = new SafetyRulesPage();
    document.body.appendChild(page);
    await waitUntilLoaded(page);

    // happy-dom does not implement window.confirm — assign a stub.
    window.confirm = () => true;

    // @ts-expect-error — exercise the write handler directly.
    await page.removeRule(rule);

    expect(deleteRuleSpy).toHaveBeenCalledTimes(1);
    expect(deleteRuleSpy).toHaveBeenCalledWith('r1');
    document.body.removeChild(page);
  });
});
