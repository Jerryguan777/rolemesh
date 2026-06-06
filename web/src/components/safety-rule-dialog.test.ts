// @vitest-environment happy-dom
// Safety rule dialog (spec §6.11) — the three editor experiences, the
// scope-immutability lock, the action_override write rule, and the routing
// form ⇄ config round-trip.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { createRuleSpy, updateRuleSpy } = vi.hoisted(() => ({
  createRuleSpy: vi.fn(),
  updateRuleSpy: vi.fn(),
}));

vi.mock('../services/safety-admin-client.js', async () => {
  const actual = await vi.importActual<
    typeof import('../services/safety-admin-client.js')
  >('../services/safety-admin-client.js');
  return { ...actual, createRule: createRuleSpy, updateRule: updateRuleSpy };
});

// Value import registers the custom element (via @customElement decorator) AND
// makes SafetyRuleDialog available for static method access in G4 tests.
import { SafetyRuleDialog } from './safety-rule-dialog.js';
import type { SafetyCheck, SafetyRule } from '../api/client.js';

const piiRegex: SafetyCheck = {
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

const presidio: SafetyCheck = {
  id: 'presidio.pii',
  version: '1',
  stages: ['post_tool_result'],
  cost_class: 'slow',
  action_model: 'config_routed',
  natural_actions: { post_tool_result: 'allow' },
  supported_actions: { post_tool_result: ['allow', 'block', 'redact', 'warn'] },
  supported_codes: [],
  config_schema: null,
} as SafetyCheck;

const domainAllowlist: SafetyCheck = {
  id: 'domain_allowlist',
  version: '1',
  stages: ['pre_tool_call'],
  cost_class: 'cheap',
  action_model: 'fixed',
  natural_actions: { pre_tool_call: 'block' },
  supported_actions: { pre_tool_call: ['allow', 'block', 'require_approval', 'warn'] },
  supported_codes: [],
  config_schema: null,
} as SafetyCheck;

const egressDomainRule: SafetyCheck = {
  id: 'egress.domain_rule',
  version: '1',
  stages: ['egress_request'],
  cost_class: 'cheap',
  action_model: 'aggregated',
  natural_actions: { egress_request: 'allow' },
  supported_actions: { egress_request: ['allow', 'block'] },
  supported_codes: [],
  config_schema: null,
} as SafetyCheck;

const ALL_CHECKS = [piiRegex, presidio, domainAllowlist, egressDomainRule];

function makeRule(over: Partial<SafetyRule> = {}): SafetyRule {
  return {
    id: 'r1',
    tenant_id: 't1',
    coworker_id: null,
    stage: 'pre_tool_call',
    check_id: 'pii.regex',
    config: {},
    priority: 100,
    enabled: true,
    description: '',
    created_at: '2026-05-21T00:00:00Z',
    updated_at: '2026-05-21T00:00:00Z',
    source: 'tenant',
    tier: null,
    editable: true,
    ...over,
  } as SafetyRule;
}

async function mount(props: Partial<SafetyRuleDialog> = {}): Promise<SafetyRuleDialog> {
  const el = document.createElement('rm-safety-rule-dialog') as SafetyRuleDialog;
  // Append + await once to force the upgrade (happy-dom upgrades on connect),
  // THEN set reactive props — props set on a pre-upgrade element shadow Lit's
  // accessors and seedForm never sees them.
  document.body.appendChild(el);
  await el.updateComplete;
  el.checks = ALL_CHECKS;
  el.coworkers = [{ id: 'ops', name: 'Ops coworker' }];
  Object.assign(el, props);
  el.open = true;
  await el.updateComplete;
  await el.updateComplete;
  return el;
}

const $ = <T extends Element>(el: Element, sel: string): T | null =>
  el.querySelector<T>(sel);

// Module-level hooks so EVERY test (across all describes) starts with the
// write spies armed — a per-describe beforeEach that some blocks omitted left
// the spies returning undefined after a prior clearAllMocks, which made submit
// throw and skip the createRule call (a cross-test pollution bug).
beforeEach(() => {
  createRuleSpy.mockResolvedValue(makeRule());
  updateRuleSpy.mockResolvedValue(makeRule());
});
afterEach(() => {
  vi.clearAllMocks();
  document.body.innerHTML = '';
});

describe('SafetyRuleDialog — three editor experiences (§6.11.1)', () => {
  it('Experience 1 — fixed check shows the segmented action control', async () => {
    const el = await mount({ duplicating: makeRule({ check_id: 'pii.regex' }) });
    expect($(el, '[data-testid="saf-action-field"]')).not.toBeNull();
    expect($(el, '[data-testid="saf-action-seg"]')).not.toBeNull();
    el.remove();
  });

  it('Experience 1 — allow & redact are disabled; warn/approve/block usable', async () => {
    const el = await mount({ duplicating: makeRule({ check_id: 'pii.regex' }) });
    const btn = (a: string) =>
      $<HTMLButtonElement>(el, `[data-testid="saf-action-seg"] button[data-action="${a}"]`)!;
    expect(btn('allow').disabled).toBe(true); // cannot downgrade to allow
    expect(btn('redact').disabled).toBe(true); // only Presidio redacts
    expect(btn('warn').disabled).toBe(false);
    expect(btn('require_approval').disabled).toBe(false);
    expect(btn('block').disabled).toBe(false); // natural / default
    // the disabled buttons carry an explanatory tooltip
    expect(btn('redact').getAttribute('data-reason')).toBeTruthy();
    el.remove();
  });

  it('Experience 2 — config_routed check hides the action field, shows routing', async () => {
    const el = await mount({ duplicating: makeRule({ check_id: 'presidio.pii', stage: 'post_tool_result' }) });
    expect($(el, '[data-testid="saf-action-field"]')).toBeNull();
    expect($(el, '[data-testid="saf-routing"]')).not.toBeNull();
    el.remove();
  });

  it('Experience 2 — routing dropdowns exclude allow (the blank option is allow)', async () => {
    const el = await mount({ duplicating: makeRule({ check_id: 'presidio.pii', stage: 'post_tool_result' }) });
    const select = $<HTMLSelectElement>(el, '[data-testid="saf-routing"] select')!;
    const values = [...select.options].map((o) => o.value);
    expect(values).toContain(''); // — allow these —
    expect(values).not.toContain('allow');
    expect(values).toContain('redact'); // presidio can redact at post_tool_result
    el.remove();
  });

  it('Experience 3 — host-list check hides the action field, shows host textarea', async () => {
    const el = await mount({ duplicating: makeRule({ check_id: 'domain_allowlist', stage: 'pre_tool_call' }) });
    expect($(el, '[data-testid="saf-action-field"]')).toBeNull();
    expect($(el, '[data-testid="saf-hosts"]')).not.toBeNull();
    el.remove();
  });
});

describe('SafetyRuleDialog — scope immutability (§6.11.3)', () => {
  it('locks the scope select + shows the hint in edit mode', async () => {
    const el = await mount({ editing: makeRule({ coworker_id: 'ops' }) });
    const scope = $<HTMLSelectElement>(el, '[data-testid="saf-scope"]')!;
    expect(scope.disabled).toBe(true);
    expect($(el, '[data-testid="saf-scope-locked"]')).not.toBeNull();
    el.remove();
  });

  it('leaves the scope select editable in create / duplicate mode', async () => {
    const el = await mount({ duplicating: makeRule({ coworker_id: 'ops' }) });
    const scope = $<HTMLSelectElement>(el, '[data-testid="saf-scope"]')!;
    expect(scope.disabled).toBe(false);
    expect($(el, '[data-testid="saf-scope-locked"]')).toBeNull();
    el.remove();
  });

  it('never sends coworker_id on an edit PATCH', async () => {
    updateRuleSpy.mockResolvedValue(makeRule());
    const el = await mount({ editing: makeRule({ id: 'r9', coworker_id: 'ops' }) });
    ($(el, '[data-testid="saf-submit"]') as HTMLButtonElement).click();
    await el.updateComplete;
    await Promise.resolve();
    expect(updateRuleSpy).toHaveBeenCalledTimes(1);
    const [, body] = updateRuleSpy.mock.calls[0];
    expect(body).not.toHaveProperty('coworker_id');
    el.remove();
  });
});

describe('SafetyRuleDialog — action_override write rule', () => {
  it('writes action_override only when a non-natural action is picked', async () => {
    const el = await mount({ duplicating: makeRule({ check_id: 'pii.regex', stage: 'pre_tool_call' }) });
    ($(el, '[data-testid="saf-action-seg"] button[data-action="warn"]') as HTMLButtonElement).click();
    await el.updateComplete;
    ($(el, '[data-testid="saf-submit"]') as HTMLButtonElement).click();
    await el.updateComplete;
    await Promise.resolve();
    const [body] = createRuleSpy.mock.calls[0];
    expect((body.config as Record<string, unknown>).action_override).toBe('warn');
    el.remove();
  });

  it('omits action_override when the natural action stays selected', async () => {
    const el = await mount({ duplicating: makeRule({ check_id: 'pii.regex', stage: 'pre_tool_call' }) });
    ($(el, '[data-testid="saf-submit"]') as HTMLButtonElement).click();
    await el.updateComplete;
    await Promise.resolve();
    const [body] = createRuleSpy.mock.calls[0];
    expect((body.config as Record<string, unknown>).action_override).toBeUndefined();
    el.remove();
  });

  it('never writes action_override for a config_routed check; converts to backend format', async () => {
    // Backend format for presidio is block_codes/redact_codes, not routing.
    const el = await mount({ duplicating: makeRule({ check_id: 'presidio.pii', stage: 'post_tool_result', config: { block_codes: ['US_SSN'], redact_codes: [] } }) });
    ($(el, '[data-testid="saf-submit"]') as HTMLButtonElement).click();
    await el.updateComplete;
    await Promise.resolve();
    const [body] = createRuleSpy.mock.calls[0];
    expect((body.config as Record<string, unknown>).action_override).toBeUndefined();
    expect((body.config as Record<string, unknown>).block_codes).toEqual(['US_SSN']);
    expect((body.config as Record<string, unknown>).redact_codes).toEqual([]);
    el.remove();
  });
});

describe('SafetyRuleDialog — presidio routing form ⇄ backend format round-trip', () => {
  it('loads backend block_codes/redact_codes and saves back in the same format', async () => {
    updateRuleSpy.mockResolvedValue(makeRule());
    // Server stores block_codes + redact_codes; dialog converts internally for display.
    const el = await mount({
      editing: makeRule({
        id: 'r5',
        check_id: 'presidio.pii',
        stage: 'post_tool_result',
        config: { block_codes: ['US_SSN'], redact_codes: ['EMAIL_ADDRESS'], score_threshold: 0.6 },
      }),
    });
    // The routing select for EMAIL_ADDRESS should exist (loaded from redact_codes).
    const emailSel = $<HTMLSelectElement>(
      el,
      '[data-testid="saf-routing"] select[data-routing-code="EMAIL_ADDRESS"]',
    )!;
    expect(emailSel).not.toBeNull();
    // Save without touching → round-trips back to block_codes/redact_codes.
    ($(el, '[data-testid="saf-submit"]') as HTMLButtonElement).click();
    await el.updateComplete;
    await Promise.resolve();
    const [, body] = updateRuleSpy.mock.calls[0];
    expect((body.config as Record<string, unknown>).block_codes).toEqual(['US_SSN']);
    expect((body.config as Record<string, unknown>).redact_codes).toEqual(['EMAIL_ADDRESS']);
    expect((body.config as Record<string, unknown>).score_threshold).toBeDefined();
    el.remove();
  });

  it('host-list check reads existing allowed_hosts into the textarea on load', async () => {
    const el = await mount({
      editing: makeRule({
        check_id: 'domain_allowlist',
        stage: 'pre_tool_call',
        config: { allowed_hosts: ['api.stripe.com', '*.internal.acme.com'] },
      }),
    });
    const ta = $<HTMLTextAreaElement>(el, '[data-testid="saf-hosts"]')!;
    expect(ta.value).toContain('api.stripe.com');
    expect(ta.value).toContain('*.internal.acme.com');
    el.remove();
  });

  it('host-list check writes allowed_hosts to backend on save', async () => {
    updateRuleSpy.mockResolvedValue(makeRule());
    const el = await mount({
      editing: makeRule({
        id: 'r7',
        check_id: 'domain_allowlist',
        stage: 'pre_tool_call',
        config: { allowed_hosts: ['api.stripe.com'] },
      }),
    });
    ($(el, '[data-testid="saf-submit"]') as HTMLButtonElement).click();
    await el.updateComplete;
    await Promise.resolve();
    const [, body] = updateRuleSpy.mock.calls[0];
    expect((body.config as Record<string, unknown>).allowed_hosts).toEqual(['api.stripe.com']);
    expect((body.config as Record<string, unknown>).domain_patterns).toBeUndefined();
    el.remove();
  });

  it('clearing a routing dropdown removes entity from the output lists', async () => {
    updateRuleSpy.mockResolvedValue(makeRule());
    const el = await mount({
      editing: makeRule({
        id: 'r6',
        check_id: 'presidio.pii',
        stage: 'post_tool_result',
        config: { block_codes: ['US_SSN'], redact_codes: ['EMAIL_ADDRESS'] },
      }),
    });
    const emailSel = $<HTMLSelectElement>(
      el,
      '[data-testid="saf-routing"] select[data-routing-code="EMAIL_ADDRESS"]',
    )!;
    // Clear EMAIL_ADDRESS routing → it should vanish from redact_codes.
    emailSel.value = '';
    emailSel.dispatchEvent(new Event('change'));
    await el.updateComplete;
    ($(el, '[data-testid="saf-submit"]') as HTMLButtonElement).click();
    await el.updateComplete;
    await Promise.resolve();
    const [, body] = updateRuleSpy.mock.calls[0];
    expect((body.config as Record<string, unknown>).block_codes).toEqual(['US_SSN']);
    expect((body.config as Record<string, unknown>).redact_codes).toEqual([]);
    el.remove();
  });
});

// G1/G2 — egress.domain_rule shared host-list form (spec §6.12.1)
describe('SafetyRuleDialog — egress.domain_rule host-list form (G1/G2)', () => {
  it('shows the domain_patterns textarea (not allowed_hosts) for egress', async () => {
    const el = await mount({
      duplicating: makeRule({ check_id: 'egress.domain_rule', stage: 'egress_request', config: {} }),
    });
    expect($(el, '[data-testid="saf-hosts"]')).not.toBeNull();
    // ports input is egress-only
    expect($(el, '[data-testid="saf-egress-ports"]')).not.toBeNull();
    el.remove();
  });

  it('does NOT show the ports input for domain_allowlist', async () => {
    const el = await mount({
      duplicating: makeRule({ check_id: 'domain_allowlist', stage: 'pre_tool_call', config: {} }),
    });
    expect($(el, '[data-testid="saf-egress-ports"]')).toBeNull();
    el.remove();
  });

  it('loads existing domain_patterns into the textarea', async () => {
    const el = await mount({
      editing: makeRule({
        check_id: 'egress.domain_rule',
        stage: 'egress_request',
        config: { domain_patterns: ['api.stripe.com', '*.slack.com'], ports: [443] },
      }),
    });
    const ta = $<HTMLTextAreaElement>(el, '[data-testid="saf-hosts"]')!;
    expect(ta.value).toContain('api.stripe.com');
    expect(ta.value).toContain('*.slack.com');
    const portsInput = $<HTMLInputElement>(el, '[data-testid="saf-egress-ports"]')!;
    expect(portsInput.value).toContain('443');
    el.remove();
  });

  it('writes domain_patterns (not allowed_hosts) to backend on save', async () => {
    updateRuleSpy.mockResolvedValue(makeRule());
    const el = await mount({
      editing: makeRule({
        id: 'r8',
        check_id: 'egress.domain_rule',
        stage: 'egress_request',
        config: { domain_patterns: ['api.stripe.com'] },
      }),
    });
    ($(el, '[data-testid="saf-submit"]') as HTMLButtonElement).click();
    await el.updateComplete;
    await Promise.resolve();
    const [, body] = updateRuleSpy.mock.calls[0];
    expect((body.config as Record<string, unknown>).domain_patterns).toEqual(['api.stripe.com']);
    expect((body.config as Record<string, unknown>).allowed_hosts).toBeUndefined();
    expect((body.config as Record<string, unknown>).domain_pattern).toBeUndefined(); // old singular key gone
    el.remove();
  });

  it('includes ports in backend config when set', async () => {
    createRuleSpy.mockResolvedValue(makeRule());
    const el = await mount({
      duplicating: makeRule({ check_id: 'egress.domain_rule', stage: 'egress_request', config: {} }),
    });
    // Simulate typing domain_patterns
    const ta = $<HTMLTextAreaElement>(el, '[data-testid="saf-hosts"]')!;
    ta.value = 'api.stripe.com';
    ta.dispatchEvent(new Event('input'));
    // Simulate ports input
    const portsInput = $<HTMLInputElement>(el, '[data-testid="saf-egress-ports"]')!;
    portsInput.value = '443, 8443';
    portsInput.dispatchEvent(new Event('input'));
    await el.updateComplete;
    ($(el, '[data-testid="saf-submit"]') as HTMLButtonElement).click();
    await el.updateComplete;
    await Promise.resolve();
    const [body] = createRuleSpy.mock.calls[0];
    expect((body.config as Record<string, unknown>).ports).toEqual([443, 8443]);
    el.remove();
  });

  it('omits ports from backend config when the ports input is empty', async () => {
    createRuleSpy.mockResolvedValue(makeRule());
    const el = await mount({
      duplicating: makeRule({ check_id: 'egress.domain_rule', stage: 'egress_request', config: {} }),
    });
    const ta = $<HTMLTextAreaElement>(el, '[data-testid="saf-hosts"]')!;
    ta.value = 'api.stripe.com';
    ta.dispatchEvent(new Event('input'));
    await el.updateComplete;
    ($(el, '[data-testid="saf-submit"]') as HTMLButtonElement).click();
    await el.updateComplete;
    await Promise.resolve();
    const [body] = createRuleSpy.mock.calls[0];
    expect((body.config as Record<string, unknown>).ports).toBeUndefined();
    el.remove();
  });
});

// G1/G2 — onBlur domain normalization (spec §6.12.1)
describe('SafetyRuleDialog — host-list onBlur normalization (G1/G2)', () => {
  it('strips https:// scheme on blur for egress', async () => {
    const el = await mount({
      duplicating: makeRule({ check_id: 'egress.domain_rule', stage: 'egress_request', config: {} }),
    });
    const ta = $<HTMLTextAreaElement>(el, '[data-testid="saf-hosts"]')!;
    ta.value = 'https://www.reddit.com/\nhttps://api.stripe.com/v1/charges';
    ta.dispatchEvent(new Event('blur'));
    await el.updateComplete;
    expect(ta.value).toBe('www.reddit.com\napi.stripe.com');
    el.remove();
  });

  it('lowercases domain entries on blur', async () => {
    const el = await mount({
      duplicating: makeRule({ check_id: 'domain_allowlist', stage: 'pre_tool_call', config: {} }),
    });
    const ta = $<HTMLTextAreaElement>(el, '[data-testid="saf-hosts"]')!;
    ta.value = 'Www.Stripe.Com\nAPI.Example.COM';
    ta.dispatchEvent(new Event('blur'));
    await el.updateComplete;
    expect(ta.value).toBe('www.stripe.com\napi.example.com');
    el.remove();
  });

  it('drops blank lines on blur', async () => {
    const el = await mount({
      duplicating: makeRule({ check_id: 'egress.domain_rule', stage: 'egress_request', config: {} }),
    });
    const ta = $<HTMLTextAreaElement>(el, '[data-testid="saf-hosts"]')!;
    ta.value = 'api.stripe.com\n\n\n*.slack.com\n';
    ta.dispatchEvent(new Event('blur'));
    await el.updateComplete;
    expect(ta.value).toBe('api.stripe.com\n*.slack.com');
    el.remove();
  });

  it('preserves wildcard prefixes on blur', async () => {
    const el = await mount({
      duplicating: makeRule({ check_id: 'domain_allowlist', stage: 'pre_tool_call', config: {} }),
    });
    const ta = $<HTMLTextAreaElement>(el, '[data-testid="saf-hosts"]')!;
    ta.value = '*.stripe.com';
    ta.dispatchEvent(new Event('blur'));
    await el.updateComplete;
    expect(ta.value).toBe('*.stripe.com');
    el.remove();
  });
});

// G4 — client-side schema validation (spec §6.12.3 / §6.18)
describe('SafetyRuleDialog — schema validation (G4)', () => {
  // A SafetyCheck with a realistic config_schema for pii.regex.
  const piiWithSchema: SafetyCheck = {
    ...piiRegex,
    config_schema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        patterns: {
          type: 'object',
          propertyNames: { enum: ['SSN', 'CREDIT_CARD', 'EMAIL', 'PHONE_US', 'IP_ADDRESS'] },
          additionalProperties: { type: 'boolean' },
        },
      },
    },
  };

  // A SafetyCheck with minItems on domain_patterns.
  const egressWithSchema: SafetyCheck = {
    ...egressDomainRule,
    config_schema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        domain_patterns: { type: 'array', items: { type: 'string' }, minItems: 1 },
        ports: { type: 'array', items: { type: 'integer' } },
      },
      required: ['domain_patterns'],
    },
  };

  it('Ajv catches additionalProperties violation', async () => {
    const el = await mount({
      checks: [piiWithSchema, presidio, domainAllowlist, egressDomainRule],
    });
    // Inject bad config via advanced JSON textarea.
    const adv = $<HTMLButtonElement>(el, '[data-testid="saf-adv-toggle"]');
    adv?.click();
    await el.updateComplete;
    const ta = $<HTMLTextAreaElement>(el, '[data-testid="saf-config-json"]');
    if (ta) {
      ta.value = '{"patterns": {}, "unknown_extra_field": true}';
      ta.dispatchEvent(new Event('input'));
    }
    await el.updateComplete;
    ($<HTMLButtonElement>(el, '[data-testid="saf-submit"]'))!.click();
    await el.updateComplete;
    // Should NOT have called createRule.
    expect(createRuleSpy).not.toHaveBeenCalled();
    expect($<HTMLElement>(el, '[data-testid="saf-error-banner"]')).not.toBeNull();
    el.remove();
  });

  it('sanity check fires when patterns is empty (config_schema present)', async () => {
    const el = await mount({
      checks: [piiWithSchema, presidio, domainAllowlist, egressDomainRule],
    });
    // Open config and submit without selecting any pattern (empty patterns).
    ($<HTMLButtonElement>(el, '[data-testid="saf-submit"]'))!.click();
    await el.updateComplete;
    expect(createRuleSpy).not.toHaveBeenCalled();
    expect($<HTMLElement>(el, '[data-testid="saf-error-banner"]')).not.toBeNull();
    el.remove();
  });

  it('sanity check does NOT fire when config_schema is null', async () => {
    // piiRegex has config_schema: null — submission should go through.
    createRuleSpy.mockResolvedValue(makeRule());
    const el = await mount();
    ($<HTMLButtonElement>(el, '[data-testid="saf-submit"]'))!.click();
    await el.updateComplete;
    await Promise.resolve();
    expect(createRuleSpy).toHaveBeenCalledTimes(1);
    el.remove();
  });

  it('Ajv catches minItems violation (egress domain_patterns empty)', async () => {
    const el = await mount({
      checks: [piiRegex, presidio, domainAllowlist, egressWithSchema],
      duplicating: makeRule({ check_id: 'egress.domain_rule', stage: 'egress_request', config: {} }),
    });
    ($<HTMLButtonElement>(el, '[data-testid="saf-submit"]'))!.click();
    await el.updateComplete;
    expect(createRuleSpy).not.toHaveBeenCalled();
    expect($<HTMLElement>(el, '[data-testid="saf-error-banner"]')).not.toBeNull();
    el.remove();
  });

  it('error banner lists all errors', async () => {
    const el = await mount({
      checks: [piiWithSchema, presidio, domainAllowlist, egressDomainRule],
    });
    ($<HTMLButtonElement>(el, '[data-testid="saf-submit"]'))!.click();
    await el.updateComplete;
    const banner = $<HTMLElement>(el, '[data-testid="saf-error-banner"]')!;
    expect(banner).not.toBeNull();
    expect(banner.querySelector('ul')).not.toBeNull();
    el.remove();
  });
});

// G4 — FastAPI 4xx translator (SafetyRuleDialog._parseBackend400)
describe('SafetyRuleDialog — FastAPI 4xx translator (G4)', () => {
  it('translates extra_forbidden to user-friendly message', () => {
    const result = SafetyRuleDialog._parseBackend400({
      detail: [{ type: 'extra_forbidden', loc: ['body', 'config', 'hosts'], msg: 'Extra inputs are not permitted' }],
    });
    expect(result).toHaveLength(1);
    expect(result[0].message).toMatch(/unknown field/i);
    expect(result[0].message).toMatch(/hosts/);
  });

  it('translates missing to user-friendly message', () => {
    const result = SafetyRuleDialog._parseBackend400({
      detail: [{ type: 'missing', loc: ['body', 'config', 'domain_patterns'], msg: 'Field required' }],
    });
    expect(result[0].message).toMatch(/required field/i);
    expect(result[0].message).toMatch(/domain_patterns/);
  });

  it('translates enum to message', () => {
    const result = SafetyRuleDialog._parseBackend400({
      detail: [{ type: 'enum', loc: ['body', 'config', 'patterns', 'SNN'], msg: 'Input should be one of SSN, CREDIT_CARD' }],
    });
    expect(result[0].message).toMatch(/invalid value/i);
  });

  it('translates int_parsing to user-friendly message', () => {
    const result = SafetyRuleDialog._parseBackend400({
      detail: [{ type: 'int_parsing', loc: ['body', 'config', 'ports', '0'], msg: 'Input should be a valid integer' }],
    });
    expect(result[0].message).toMatch(/must be a number/i);
  });

  it('returns empty array when detail is missing', () => {
    expect(SafetyRuleDialog._parseBackend400({})).toEqual([]);
    expect(SafetyRuleDialog._parseBackend400(null)).toEqual([]);
    expect(SafetyRuleDialog._parseBackend400({ other: 'key' })).toEqual([]);
  });

  it('returns empty array for non-array detail', () => {
    expect(SafetyRuleDialog._parseBackend400({ detail: 'string error' })).toEqual([]);
  });
});

// G3 — duplicate-rule detection (spec §6.10a)
describe('SafetyRuleDialog — duplicate detection (G3)', () => {
  // Default seedForm picks stages[0] = 'input_prompt' for pii.regex.
  // existingRule must share the same (check, scope, stage) triple.
  const existingRule = makeRule({
    id: 'existing-r1',
    check_id: 'pii.regex',
    stage: 'input_prompt',
    coworker_id: null,
    config: { patterns: { SSN: true } },
    priority: 50,
    enabled: false,
    source: 'tenant',
  });

  it('detects a triple match and shows the info banner', async () => {
    const el = await mount({ rules: [existingRule] });
    // Default create mode opens with pii.regex / pre_tool_call / all coworkers
    // which matches existingRule's triple.
    expect($(el, '[data-testid="saf-dup-banner-info"]')).not.toBeNull();
    el.remove();
  });

  it('flips the dialog title to "Edit existing rule" on match', async () => {
    const el = await mount({ rules: [existingRule] });
    // The title is passed as an attribute to <rm-dialog>; check the attribute.
    const dlg = $<HTMLElement>(el, 'rm-dialog')!;
    expect(dlg.getAttribute('title')).toBe('Edit existing rule');
    el.remove();
  });

  it('pre-loads the existing rule config, priority, and enabled state', async () => {
    const el = await mount({ rules: [existingRule] });
    expect($<HTMLInputElement>(el, '[data-testid="saf-priority"]')!.value).toBe('50');
    // enabled toggle should reflect existingRule.enabled = false
    const toggle = $<HTMLButtonElement>(el, '[data-testid="saf-enabled"]')!;
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
    el.remove();
  });

  it('locks the scope select when in dup-target mode', async () => {
    const el = await mount({ rules: [existingRule] });
    const scope = $<HTMLSelectElement>(el, '[data-testid="saf-scope"]')!;
    expect(scope.disabled).toBe(true);
    el.remove();
  });

  it('"Create a separate rule anyway" clears dupTarget and shows warn banner', async () => {
    const el = await mount({ rules: [existingRule] });
    expect($(el, '[data-testid="saf-dup-banner-info"]')).not.toBeNull();
    ($<HTMLButtonElement>(el, '[data-testid="saf-dup-force-create"]'))!.click();
    await el.updateComplete;
    expect($(el, '[data-testid="saf-dup-banner-info"]')).toBeNull();
    expect($(el, '[data-testid="saf-dup-banner-warn"]')).not.toBeNull();
    el.remove();
  });

  it('save btn label changes: info→"Save changes", force-create→"Create separate rule"', async () => {
    const el = await mount({ rules: [existingRule] });
    const btn = $<HTMLButtonElement>(el, '[data-testid="saf-submit"]')!;
    expect(btn.textContent?.trim()).toBe('Save changes');
    ($<HTMLButtonElement>(el, '[data-testid="saf-dup-force-create"]'))!.click();
    await el.updateComplete;
    expect(btn.textContent?.trim()).toBe('Create separate rule');
    el.remove();
  });

  it('"Switch back" clears forceCreate and re-detects (restores info banner)', async () => {
    const el = await mount({ rules: [existingRule] });
    ($<HTMLButtonElement>(el, '[data-testid="saf-dup-force-create"]'))!.click();
    await el.updateComplete;
    expect($(el, '[data-testid="saf-dup-banner-warn"]')).not.toBeNull();
    ($<HTMLButtonElement>(el, '[data-testid="saf-dup-switch-back"]'))!.click();
    await el.updateComplete;
    expect($(el, '[data-testid="saf-dup-banner-info"]')).not.toBeNull();
    el.remove();
  });

  it('skip: real edit mode — no detection fires', async () => {
    const el = await mount({ editing: existingRule, rules: [existingRule] });
    expect($(el, '[data-testid="saf-dup-banner-info"]')).toBeNull();
    el.remove();
  });

  it('skip: force-create already on — triple change does not re-flip', async () => {
    const el = await mount({ rules: [existingRule] });
    ($<HTMLButtonElement>(el, '[data-testid="saf-dup-force-create"]'))!.click();
    await el.updateComplete;
    // Stage change should NOT re-flip to info banner while forceCreate is true.
    const stageSelect = $<HTMLSelectElement>(el, '[data-testid="saf-stage"]');
    if (stageSelect) {
      stageSelect.value = 'input_prompt';
      stageSelect.dispatchEvent(new Event('change'));
      await el.updateComplete;
    }
    // Still in force-create mode (no re-trigger).
    expect($(el, '[data-testid="saf-dup-banner-warn"]')).not.toBeNull();
    expect($(el, '[data-testid="saf-dup-banner-info"]')).toBeNull();
    el.remove();
  });

  it('platform-tier overlap shows FYI banner, not info banner', async () => {
    const platformRule = makeRule({
      id: 'plat-r1',
      check_id: 'pii.regex',
      stage: 'input_prompt', // matches default seedForm triple
      coworker_id: null,
      source: 'platform',
    });
    const el = await mount({ rules: [platformRule] });
    expect($(el, '[data-testid="saf-dup-banner-info"]')).toBeNull();
    expect($(el, '[data-testid="saf-dup-banner-fyi"]')).not.toBeNull();
    el.remove();
  });

  it('routes to updateRule(existingRule.id) when submitting in dup-target mode', async () => {
    updateRuleSpy.mockResolvedValue(makeRule({ id: 'existing-r1' }));
    const el = await mount({ rules: [existingRule] });
    expect($(el, '[data-testid="saf-dup-banner-info"]')).not.toBeNull();
    ($<HTMLButtonElement>(el, '[data-testid="saf-submit"]'))!.click();
    await el.updateComplete;
    await Promise.resolve();
    expect(updateRuleSpy).toHaveBeenCalledTimes(1);
    expect(updateRuleSpy.mock.calls[0][0]).toBe('existing-r1');
    expect(createRuleSpy).not.toHaveBeenCalled();
    el.remove();
  });

  it('routes to createRule when submitting in force-create mode', async () => {
    createRuleSpy.mockResolvedValue(makeRule());
    const el = await mount({ rules: [existingRule] });
    ($<HTMLButtonElement>(el, '[data-testid="saf-dup-force-create"]'))!.click();
    await el.updateComplete;
    ($<HTMLButtonElement>(el, '[data-testid="saf-submit"]'))!.click();
    await el.updateComplete;
    await Promise.resolve();
    expect(createRuleSpy).toHaveBeenCalledTimes(1);
    expect(updateRuleSpy).not.toHaveBeenCalled();
    el.remove();
  });

  it('does not detect duplicating rule against itself', async () => {
    // Duplicating a rule: the source (existingRule) should not match against itself.
    const el = await mount({ duplicating: existingRule, rules: [existingRule] });
    expect($(el, '[data-testid="saf-dup-banner-info"]')).toBeNull();
    el.remove();
  });
});
