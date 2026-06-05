// @vitest-environment happy-dom
// Safety rules page (spec §6) — pins the v1/admin read-vs-write split AND the
// revamped two-tier list behaviour.
//
// What this test catches:
//   * Reads on mount go through the typed v1 ApiClient.
//   * Writes (toggle / delete) keep using safety-admin-client (admin).
//   * Platform-tier rows are audit-only — no edit / duplicate / delete and the
//     toggle is inert (a regression that exposed those would let an org mutate
//     a cross-tenant default it doesn't own).
//   * The card renders the human label, sentence, action pill, and chips.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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

import { SafetyRulesPage, auditSummary } from './safety-rules-page.js';
import type { SafetyCheck, SafetyRule, SafetyRuleAuditEntry } from '../api/client.js';

async function waitUntilLoaded(page: SafetyRulesPage): Promise<void> {
  for (let i = 0; i < 30; i++) {
    await Promise.resolve();
    await page.updateComplete;
    // @ts-expect-error — touching private state for assertion
    if (page.loading === false) return;
  }
  throw new Error('SafetyRulesPage did not finish loading');
}

function piiCheck(): SafetyCheck {
  return {
    id: 'pii.regex',
    version: '1',
    stages: ['input_prompt', 'pre_tool_call'],
    cost_class: 'cheap',
    action_model: 'fixed',
    natural_actions: { input_prompt: 'block', pre_tool_call: 'block' },
    supported_actions: {
      input_prompt: ['allow', 'block', 'require_approval', 'warn'],
      pre_tool_call: ['allow', 'block', 'require_approval', 'warn'],
    },
    supported_codes: [],
    config_schema: null,
  } as SafetyCheck;
}

function makeRule(overrides: Partial<SafetyRule> = {}): SafetyRule {
  return {
    id: 'r1',
    tenant_id: 't1',
    coworker_id: null,
    stage: 'pre_tool_call',
    check_id: 'pii.regex',
    config: { entities: ['ssn'] },
    priority: 100,
    enabled: true,
    description: '',
    created_at: '2026-05-21T00:00:00Z',
    updated_at: '2026-05-21T00:00:00Z',
    source: 'tenant',
    tier: null,
    editable: true,
    ...overrides,
  } as SafetyRule;
}

async function mount(rules: SafetyRule[]): Promise<SafetyRulesPage> {
  listSafetyRulesSpy.mockResolvedValue(rules);
  const page = new SafetyRulesPage();
  document.body.appendChild(page);
  await waitUntilLoaded(page);
  await page.updateComplete;
  return page;
}

describe('SafetyRulesPage', () => {
  beforeEach(() => {
    listSafetyRulesSpy.mockResolvedValue([]);
    listSafetyChecksSpy.mockResolvedValue([piiCheck()]);
    listSafetyRuleAuditSpy.mockResolvedValue([]);
    createRuleSpy.mockResolvedValue(makeRule());
    updateRuleSpy.mockResolvedValue(makeRule({ enabled: false }));
    deleteRuleSpy.mockResolvedValue(undefined);
    listCoworkersSpy.mockResolvedValue([{ id: 'ops', name: 'Ops coworker' }]);
  });

  afterEach(() => {
    vi.clearAllMocks();
    document.body.innerHTML = '';
  });

  it('reads via the typed v1 ApiClient + admin coworker list on mount', async () => {
    const page = await mount([]);
    expect(listSafetyRulesSpy).toHaveBeenCalledTimes(1);
    expect(listSafetyChecksSpy).toHaveBeenCalledTimes(1);
    expect(listCoworkersSpy).toHaveBeenCalledTimes(1);
    page.remove();
  });

  it('shows the rich empty state when there are no rules', async () => {
    const page = await mount([]);
    expect(page.querySelector('[data-testid="saf-empty"]')).not.toBeNull();
    page.remove();
  });

  it('renders the human label, sentence, and action pill on a card', async () => {
    const page = await mount([makeRule()]);
    const row = page.querySelector('[data-testid="saf-row"]')!;
    expect(row.querySelector('.rm-mn b')?.textContent).toContain('Personal data (regex)');
    expect(row.querySelector('[data-testid="saf-sentence"]')?.textContent).toContain(
      'Before tool calls',
    );
    // pii.regex natural action is block — the pill reflects the effective action.
    expect(row.querySelector('[data-testid="saf-action-pill"]')?.textContent?.trim()).toBe(
      'block',
    );
    page.remove();
  });

  it('shows the scope chip only for a coworker-scoped rule', async () => {
    const page = await mount([
      makeRule({ id: 'org-wide', coworker_id: null }),
      makeRule({ id: 'scoped', coworker_id: 'ops' }),
    ]);
    const rows = page.querySelectorAll('[data-testid="saf-row"]');
    const byId = (id: string) =>
      [...rows].find((r) => r.getAttribute('data-rule-id') === id)!;
    expect(byId('org-wide').querySelector('.rm-saf-scope')).toBeNull();
    expect(byId('scoped').querySelector('.rm-saf-scope')?.textContent).toContain(
      'Ops coworker',
    );
    page.remove();
  });

  describe('two-tier platform / organization split (§6.2)', () => {
    it('renders the platform banner only when a platform rule exists', async () => {
      const orgOnly = await mount([makeRule()]);
      expect(orgOnly.querySelector('[data-testid="saf-platform-banner"]')).toBeNull();
      orgOnly.remove();

      const withPlatform = await mount([
        makeRule({ id: 'plt', source: 'platform', tier: 'floor', editable: false }),
      ]);
      expect(
        withPlatform.querySelector('[data-testid="saf-platform-banner"]'),
      ).not.toBeNull();
      withPlatform.remove();
    });

    it('platform rows expose audit only — no edit / duplicate / delete', async () => {
      const page = await mount([
        makeRule({ id: 'plt', source: 'platform', tier: 'floor', editable: false }),
      ]);
      const card = page.querySelector('[data-rule-id="plt"]')!;
      expect(card.querySelector('[data-testid="saf-audit"]')).not.toBeNull();
      expect(card.querySelector('[data-testid="saf-edit"]')).toBeNull();
      expect(card.querySelector('[data-testid="saf-duplicate"]')).toBeNull();
      expect(card.querySelector('[data-testid="saf-delete"]')).toBeNull();
      page.remove();
    });

    it('platform toggle is inert (a span, not a clickable button)', async () => {
      const page = await mount([
        makeRule({ id: 'plt', source: 'platform', tier: 'floor', editable: false }),
      ]);
      const toggle = page
        .querySelector('[data-rule-id="plt"]')!
        .querySelector('[data-testid="saf-toggle"]')!;
      expect(toggle.tagName).toBe('SPAN');
      // org rows render a <button> toggle instead:
      page.remove();
      const orgPage = await mount([makeRule({ id: 'org' })]);
      const orgToggle = orgPage
        .querySelector('[data-rule-id="org"]')!
        .querySelector('[data-testid="saf-toggle"]')!;
      expect(orgToggle.tagName).toBe('BUTTON');
      orgPage.remove();
    });

    it('does not toggle a platform rule even if toggleEnabled is called', async () => {
      const page = await mount([
        makeRule({ id: 'plt', source: 'platform', tier: 'floor', editable: false }),
      ]);
      // @ts-expect-error — exercise the guard directly
      await page.toggleEnabled(page.rules[0]);
      expect(updateRuleSpy).not.toHaveBeenCalled();
      page.remove();
    });
  });

  it('toggle on an org rule PATCHes via the admin client (optimistic)', async () => {
    const page = await mount([makeRule({ id: 'org', enabled: true })]);
    const toggle = page
      .querySelector('[data-rule-id="org"]')!
      .querySelector('[data-testid="saf-toggle"]') as HTMLButtonElement;
    toggle.click();
    await page.updateComplete;
    expect(updateRuleSpy).toHaveBeenCalledWith('org', { enabled: false });
    page.remove();
  });

  it('delete confirm DELETEs via the admin client', async () => {
    const page = await mount([makeRule({ id: 'org' })]);
    (
      page
        .querySelector('[data-rule-id="org"]')!
        .querySelector('[data-testid="saf-delete"]') as HTMLButtonElement
    ).click();
    await page.updateComplete;
    const confirm = page.querySelector('rm-confirm-dialog')!;
    confirm.dispatchEvent(new CustomEvent('confirm'));
    await page.updateComplete;
    await Promise.resolve();
    expect(deleteRuleSpy).toHaveBeenCalledWith('org');
    page.remove();
  });

  it('audit drawer reads the timeline from the v1 client', async () => {
    listSafetyRuleAuditSpy.mockResolvedValue([
      {
        id: 'a1',
        rule_id: 'org',
        tenant_id: 't1',
        actor_user_id: 'u1',
        action: 'created',
        before_state: null,
        after_state: { check_id: 'pii.regex', stage: 'pre_tool_call' },
        note: null,
        created_at: '2026-05-21T00:00:00Z',
      } as SafetyRuleAuditEntry,
    ]);
    const page = await mount([makeRule({ id: 'org' })]);
    (
      page
        .querySelector('[data-rule-id="org"]')!
        .querySelector('[data-testid="saf-audit"]') as HTMLButtonElement
    ).click();
    await page.updateComplete;
    await Promise.resolve();
    await page.updateComplete;
    expect(listSafetyRuleAuditSpy).toHaveBeenCalledWith('org');
    page.remove();
  });
});

describe('auditSummary', () => {
  const base = (a: Partial<SafetyRuleAuditEntry>): SafetyRuleAuditEntry =>
    ({
      id: 'x',
      rule_id: 'r',
      tenant_id: 't',
      actor_user_id: null,
      before_state: null,
      after_state: null,
      note: null,
      created_at: '2026-05-21T00:00:00Z',
      action: 'updated',
      ...a,
    }) as SafetyRuleAuditEntry;

  it('summarizes a create with the check label', () => {
    const s = auditSummary(base({ action: 'created', after_state: { check_id: 'pii.regex', stage: 'pre_tool_call' } }));
    expect(s).toContain('Created');
    expect(s).toContain('Personal data (regex)');
  });

  it('diffs a priority change with friendly arrows', () => {
    const s = auditSummary(
      base({ before_state: { priority: 50 }, after_state: { priority: 100 } }),
    );
    expect(s).toBe('priority: 50 → 100');
  });

  it('renders enabled changes as on/off, not true/false', () => {
    const s = auditSummary(
      base({ before_state: { enabled: false }, after_state: { enabled: true } }),
    );
    expect(s).toBe('enabled: off → on');
  });

  it('surfaces an action_override change buried in config', () => {
    const s = auditSummary(
      base({
        before_state: { config: {} },
        after_state: { config: { action_override: 'warn' } },
      }),
    );
    expect(s).toContain('action: default → warn');
  });
});
