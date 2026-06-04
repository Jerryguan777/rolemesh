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

// ---------------------------------------------------------------------------
// Action matrix panel — server-driven default badge + override picker.
// ---------------------------------------------------------------------------

function makeCheck(overrides: Record<string, unknown> = {}) {
  return {
    id: 'pii.regex',
    version: '1',
    stages: ['input_prompt', 'model_output'],
    cost_class: 'cheap' as const,
    supported_codes: [],
    config_schema: null,
    action_model: 'fixed' as const,
    natural_actions: { input_prompt: 'block', model_output: 'block' },
    supported_actions: {
      // pii.regex @ input_prompt cannot redact (no modified_payload) and
      // require_approval is valid; @ model_output drops warn.
      input_prompt: ['allow', 'block', 'require_approval', 'warn'],
      model_output: ['allow', 'block', 'require_approval'],
    },
    ...overrides,
  };
}

async function openDraftFor(
  page: SafetyRulesPage,
  check_id: string,
  stage: string,
): Promise<void> {
  // Drive the private draft state directly — the panel renders from
  // (check_id, stage); we don't need to click through the selects.
  /* eslint-disable @typescript-eslint/no-explicit-any */
  (page as any).draftMode = 'create';
  (page as any).draft = {
    stage,
    check_id,
    coworker_id: null,
    config: '{}',
    priority: 100,
    enabled: true,
    description: '',
  };
  /* eslint-enable @typescript-eslint/no-explicit-any */
  page.requestUpdate();
  await page.updateComplete;
}

describe('SafetyRulesPage action panel', () => {
  beforeEach(() => {
    listSafetyRulesSpy.mockResolvedValue([]);
    listSafetyRuleAuditSpy.mockResolvedValue([]);
    createRuleSpy.mockResolvedValue(makeRule());
    updateRuleSpy.mockResolvedValue(makeRule());
    deleteRuleSpy.mockResolvedValue(undefined);
    listCoworkersSpy.mockResolvedValue([]);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('shows the fixed-default badge and greys out unsupported actions', async () => {
    listSafetyChecksSpy.mockResolvedValue([makeCheck()]);
    const page = new SafetyRulesPage();
    document.body.appendChild(page);
    await waitUntilLoaded(page);
    await openDraftFor(page, 'pii.regex', 'input_prompt');

    const badge = page.querySelector('[data-testid="action-badge"]');
    expect(badge?.textContent).toContain('This check defaults to');
    expect(badge?.textContent?.toLowerCase()).toContain('block');

    const select = page.querySelector(
      '[data-testid="action-override-select"]',
    ) as HTMLSelectElement | null;
    expect(select).not.toBeNull();
    const opt = (v: string) =>
      Array.from(select!.options).find((o) => o.value === v)!;
    // redact is NOT in supported_actions -> disabled with a reason.
    expect(opt('redact').disabled).toBe(true);
    expect(opt('redact').title).toContain('rewrite the payload');
    // block / require_approval ARE supported -> selectable.
    expect(opt('block').disabled).toBe(false);
    expect(opt('require_approval').disabled).toBe(false);
    document.body.removeChild(page);
  });

  it('config_routed checks show "no fixed default" wording', async () => {
    listSafetyChecksSpy.mockResolvedValue([
      makeCheck({
        id: 'presidio.pii',
        action_model: 'config_routed',
        stages: ['input_prompt'],
        natural_actions: { input_prompt: 'allow' },
        supported_actions: {
          input_prompt: ['allow', 'block', 'redact', 'warn', 'require_approval'],
        },
      }),
    ]);
    const page = new SafetyRulesPage();
    document.body.appendChild(page);
    await waitUntilLoaded(page);
    await openDraftFor(page, 'presidio.pii', 'input_prompt');

    const badge = page.querySelector('[data-testid="action-badge"]');
    expect(badge?.textContent).toContain('No fixed default');
    // redact IS supported for presidio -> selectable.
    const select = page.querySelector(
      '[data-testid="action-override-select"]',
    ) as HTMLSelectElement;
    const redact = Array.from(select.options).find((o) => o.value === 'redact')!;
    expect(redact.disabled).toBe(false);
    document.body.removeChild(page);
  });

  it('aggregated checks explain the voting/aggregation semantics', async () => {
    listSafetyChecksSpy.mockResolvedValue([
      makeCheck({
        id: 'egress.domain_rule',
        action_model: 'aggregated',
        stages: ['egress_request'],
        natural_actions: { egress_request: 'allow' },
        supported_actions: { egress_request: ['allow', 'block'] },
      }),
    ]);
    const page = new SafetyRulesPage();
    document.body.appendChild(page);
    await waitUntilLoaded(page);
    await openDraftFor(page, 'egress.domain_rule', 'egress_request');

    const badge = page.querySelector('[data-testid="action-badge"]');
    expect(badge?.textContent?.toLowerCase()).toContain('aggregation');
    document.body.removeChild(page);
  });

  it('picking an override writes action_override into the config JSON', async () => {
    listSafetyChecksSpy.mockResolvedValue([makeCheck()]);
    const page = new SafetyRulesPage();
    document.body.appendChild(page);
    await waitUntilLoaded(page);
    await openDraftFor(page, 'pii.regex', 'input_prompt');

    const select = page.querySelector(
      '[data-testid="action-override-select"]',
    ) as HTMLSelectElement;
    select.value = 'warn';
    select.dispatchEvent(new Event('change'));
    await page.updateComplete;

    // @ts-expect-error — read private draft state for the assertion.
    const cfg = JSON.parse(page.draft.config);
    expect(cfg.action_override).toBe('warn');
    document.body.removeChild(page);
  });
});
