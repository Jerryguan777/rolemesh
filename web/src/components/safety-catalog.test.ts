import { describe, expect, it } from 'vitest';

import type { SafetyCheck, SafetyStage } from '../api/client.js';
import {
  SAFETY_CHECK_CATALOG,
  actionButtonState,
  checkLabel,
  effectiveAction,
  naturalAction,
  safSentence,
  safWhatPhrase,
} from './safety-catalog.js';

// Minimal wire SafetyCheck factory — only the behaviour fields the helpers
// read. Mirrors the real registry shape (verified against the live dump).
function check(over: Partial<SafetyCheck> & Pick<SafetyCheck, 'id'>): SafetyCheck {
  return {
    version: '1',
    stages: [],
    cost_class: 'cheap',
    action_model: 'fixed',
    natural_actions: {},
    supported_actions: {},
    supported_codes: [],
    config_schema: null,
    ...over,
  } as SafetyCheck;
}

const piiRegex = check({
  id: 'pii.regex',
  action_model: 'fixed',
  stages: ['input_prompt', 'pre_tool_call', 'post_tool_result', 'model_output'],
  natural_actions: {
    input_prompt: 'block',
    pre_tool_call: 'block',
    post_tool_result: 'block',
    model_output: 'block',
  },
  supported_actions: {
    input_prompt: ['allow', 'block', 'require_approval', 'warn'],
    pre_tool_call: ['allow', 'block', 'require_approval', 'warn'],
    post_tool_result: ['allow', 'block', 'warn'],
    model_output: ['allow', 'block', 'require_approval'],
  },
});

const presidio = check({
  id: 'presidio.pii',
  action_model: 'config_routed',
  stages: ['input_prompt', 'post_tool_result', 'model_output'],
  natural_actions: { input_prompt: 'allow', post_tool_result: 'allow', model_output: 'allow' },
  supported_actions: {
    post_tool_result: ['allow', 'block', 'redact', 'warn'],
  },
});

const domainAllowlist = check({
  id: 'domain_allowlist',
  action_model: 'fixed',
  stages: ['pre_tool_call'],
  natural_actions: { pre_tool_call: 'block' },
  supported_actions: { pre_tool_call: ['allow', 'block', 'require_approval', 'warn'] },
});

describe('safety catalog coverage', () => {
  it('labels every catalog check and never echoes the raw id', () => {
    for (const id of Object.keys(SAFETY_CHECK_CATALOG)) {
      expect(checkLabel(id)).toBe(SAFETY_CHECK_CATALOG[id].label);
      expect(checkLabel(id)).not.toBe(id);
    }
  });

  it('falls back to the id for an unknown check', () => {
    expect(checkLabel('not.a.real.check')).toBe('not.a.real.check');
  });
});

describe('actionButtonState — backend override whitelist {block,warn,require_approval}', () => {
  // The natural action is the default and is always pickable.
  it('enables the natural action even though it is not "overridable"', () => {
    const presidioNatural = naturalAction(presidio, 'post_tool_result' as SafetyStage);
    expect(presidioNatural).toBe('allow');
    expect(
      actionButtonState(presidio, 'post_tool_result' as SafetyStage, 'allow', 'allow').enabled,
    ).toBe(true);
  });

  // allow is server-"supported" everywhere but cannot be written as an
  // override — picking it on a block-natural check would 400. Must be
  // disabled with a "disable the rule instead" hint, NOT the generic reason.
  it('disables allow on a block-natural check with the disable-rule hint', () => {
    const st = actionButtonState(
      piiRegex,
      'pre_tool_call' as SafetyStage,
      'allow',
      'block',
    );
    expect(st.enabled).toBe(false);
    expect(st.reason).toMatch(/disable the rule/i);
  });

  // redact is only synthesizable by Presidio routing, never an override.
  it('disables redact on pii.regex (cannot rewrite payload)', () => {
    const st = actionButtonState(
      piiRegex,
      'pre_tool_call' as SafetyStage,
      'redact',
      'block',
    );
    expect(st.enabled).toBe(false);
    expect(st.reason).toMatch(/redact|rewrite|presidio/i);
  });

  it('enables warn + require_approval where the stage supports them', () => {
    expect(
      actionButtonState(piiRegex, 'pre_tool_call' as SafetyStage, 'warn', 'block').enabled,
    ).toBe(true);
    expect(
      actionButtonState(piiRegex, 'pre_tool_call' as SafetyStage, 'require_approval', 'block')
        .enabled,
    ).toBe(true);
  });

  // warn is excluded on model_output by the wire matrix — server-driven.
  it('disables an action the stage does not support, with a reason', () => {
    const st = actionButtonState(
      piiRegex,
      'model_output' as SafetyStage,
      'warn',
      'block',
    );
    expect(st.enabled).toBe(false);
    expect(st.reason.length).toBeGreaterThan(0);
  });
});

describe('effectiveAction', () => {
  it('uses the config override when present', () => {
    const a = effectiveAction(
      { check_id: 'pii.regex', stage: 'pre_tool_call' as SafetyStage, config: { action_override: 'warn' } },
      piiRegex,
    );
    expect(a).toBe('warn');
  });

  it('falls back to the natural action with no override', () => {
    const a = effectiveAction(
      { check_id: 'pii.regex', stage: 'pre_tool_call' as SafetyStage, config: {} },
      piiRegex,
    );
    expect(a).toBe('block');
  });

  it('returns block for host-list checks regardless of action_model', () => {
    const a = effectiveAction(
      { check_id: 'domain_allowlist', stage: 'pre_tool_call' as SafetyStage, config: { allowed_hosts: ['a.com'] } },
      domainAllowlist,
    );
    expect(a).toBe('block');
  });

  it('returns null for presidio.pii with empty block/redact lists (inert)', () => {
    // Backend stores block_codes + redact_codes (not a routing dict).
    const a = effectiveAction(
      { check_id: 'presidio.pii', stage: 'post_tool_result' as SafetyStage, config: { block_codes: [], redact_codes: [] } },
      presidio,
    );
    expect(a).toBeNull();
  });

  it('picks redact over block for the presidio pill (most-severe wins)', () => {
    const a = effectiveAction(
      {
        check_id: 'presidio.pii',
        stage: 'post_tool_result' as SafetyStage,
        config: { block_codes: ['US_SSN'], redact_codes: ['EMAIL_ADDRESS'] },
      },
      presidio,
    );
    expect(a).toBe('redact');
  });
});

describe('safWhatPhrase', () => {
  it('names the entities for pii.regex (backend patterns dict, uppercase keys)', () => {
    // Backend stores { patterns: { SSN: true, CREDIT_CARD: true } }
    expect(safWhatPhrase('pii.regex', { patterns: { SSN: true, CREDIT_CARD: true } })).toBe(
      'detect SSNs, credit cards',
    );
  });

  it('shows nothing-configured for pii.regex with empty patterns', () => {
    expect(safWhatPhrase('pii.regex', { patterns: {} })).toBe(
      'detect configured personal data',
    );
  });

  it('avoids the word "inert" for an unconfigured presidio check', () => {
    // Backend stores { block_codes: [], redact_codes: [] }
    const phrase = safWhatPhrase('presidio.pii', { block_codes: [], redact_codes: [] });
    expect(phrase).not.toMatch(/inert/i);
    expect(phrase).toMatch(/running but doing nothing/i);
  });

  it('summarizes presidio block/redact codes with a +N overflow', () => {
    // Backend stores block_codes + redact_codes
    const phrase = safWhatPhrase('presidio.pii', {
      block_codes: ['US_SSN', 'PERSON'],
      redact_codes: ['EMAIL_ADDRESS', 'PHONE_NUMBER'],
    });
    expect(phrase).toContain('SSNs→block');
    expect(phrase).toContain('+1 more');
  });

  it('counts hosts for an allowlist', () => {
    expect(safWhatPhrase('domain_allowlist', { allowed_hosts: ['a.com', 'b.com'] })).toBe(
      'allow only 2 hosts',
    );
    expect(safWhatPhrase('domain_allowlist', { allowed_hosts: ['a.com'] })).toBe('allow only 1 host');
  });
});

describe('safSentence', () => {
  it('renders a fixed-check sentence with the effective verb and scope', () => {
    const s = safSentence(
      { check_id: 'pii.regex', stage: 'pre_tool_call' as SafetyStage, config: { patterns: { SSN: true } } },
      piiRegex,
      null,
    );
    expect(s).toContain('Before tool calls');
    expect(s).toContain('detect SSNs');
    expect(s).toContain('<b>block the call</b>');
    expect(s).toContain('All coworkers.');
  });

  it('shows the coworker name when scoped', () => {
    const s = safSentence(
      { check_id: 'pii.regex', stage: 'pre_tool_call' as SafetyStage, config: {} },
      piiRegex,
      'Ops coworker',
    );
    expect(s).toContain('Ops coworker only.');
  });

  it('omits the verb for an inert routed check (no contradiction)', () => {
    const s = safSentence(
      { check_id: 'presidio.pii', stage: 'post_tool_result' as SafetyStage, config: { block_codes: [], redact_codes: [] } },
      presidio,
      null,
    );
    expect(s).not.toContain('<b>');
    expect(s).toContain('running but doing nothing');
  });

  it('uses the allowlist phrasing for host-list checks', () => {
    const s = safSentence(
      { check_id: 'domain_allowlist', stage: 'pre_tool_call' as SafetyStage, config: { allowed_hosts: ['a.com', 'b.com', 'c.com'] } },
      domainAllowlist,
      'Ops coworker',
    );
    expect(s).toContain('allow only 3 hosts');
    expect(s).toContain('<b>block the call</b>');
    expect(s).toContain('(for anything else)');
  });

  it('escapes a coworker name with HTML metacharacters', () => {
    const s = safSentence(
      { check_id: 'pii.regex', stage: 'pre_tool_call' as SafetyStage, config: {} },
      piiRegex,
      '<script>x</script>',
    );
    expect(s).not.toContain('<script>');
    expect(s).toContain('&lt;script&gt;');
  });
});
