import { describe, expect, it } from 'vitest';
import type { SafetyRule } from '../../../api/client';
import { findDuplicate } from './duplicate-detection';
import { auditSummary } from './audit-summary';
import type { SafetyRuleAuditEntry } from '../../../api/client';

function rule(over: Partial<SafetyRule>): SafetyRule {
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
    source: 'tenant',
    tier: null,
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
    ...over,
  } as SafetyRule;
}

describe('findDuplicate (G3 triple collision)', () => {
  const triple = {
    checkId: 'pii.regex',
    stage: 'pre_tool_call' as const,
    coworkerId: null,
  };

  it('finds an org rule with the same (check, stage, scope) triple', () => {
    const hit = findDuplicate([rule({})], triple);
    expect(hit.orgMatch?.id).toBe('r1');
    expect(hit.platformMatch).toBeNull();
  });

  it('a DISABLED rule still collides (one toggle from running)', () => {
    expect(findDuplicate([rule({ enabled: false })], triple).orgMatch).not.toBeNull();
  });

  it('different scope does not collide', () => {
    expect(
      findDuplicate([rule({ coworker_id: 'cw-1' })], triple).orgMatch,
    ).toBeNull();
    expect(
      findDuplicate([rule({})], { ...triple, coworkerId: 'cw-1' }).orgMatch,
    ).toBeNull();
  });

  it('different stage or check does not collide', () => {
    expect(findDuplicate([rule({ stage: 'model_output' })], triple).orgMatch).toBeNull();
    expect(
      findDuplicate([rule({ check_id: 'secret_scanner' })], triple).orgMatch,
    ).toBeNull();
  });

  it('excludes the duplicate source (a rule never collides with itself)', () => {
    expect(findDuplicate([rule({ id: 'src' })], triple, 'src').orgMatch).toBeNull();
  });

  it('platform match reported only when no org collision (FYI, no flip)', () => {
    const plat = rule({ id: 'p1', source: 'platform' });
    const hit = findDuplicate([plat], triple);
    expect(hit.orgMatch).toBeNull();
    expect(hit.platformMatch?.id).toBe('p1');
    // Org collision takes precedence.
    const both = findDuplicate([plat, rule({ id: 'o1' })], triple);
    expect(both.orgMatch?.id).toBe('o1');
    expect(both.platformMatch).toBeNull();
  });

  it('empty triple → no detection', () => {
    expect(
      findDuplicate([rule({})], { checkId: '', stage: '', coworkerId: null }).orgMatch,
    ).toBeNull();
  });
});

describe('auditSummary (client diff — the wire has no summary field)', () => {
  function entry(over: Partial<SafetyRuleAuditEntry>): SafetyRuleAuditEntry {
    return {
      id: 'a1',
      rule_id: 'r1',
      tenant_id: 't1',
      action: 'updated',
      actor_user_id: 'u1',
      before_state: null,
      after_state: null,
      created_at: '2026-06-01T00:00:00Z',
      ...over,
    } as SafetyRuleAuditEntry;
  }

  it('created → labels the check + stage', () => {
    const s = auditSummary(
      entry({
        action: 'created',
        after_state: { check_id: 'pii.regex', stage: 'pre_tool_call' },
      }),
    );
    expect(s).toBe('Created — Personal data (regex), pre_tool_call');
  });

  it('deleted → Deleted', () => {
    expect(auditSummary(entry({ action: 'deleted' }))).toBe('Deleted');
  });

  it('diffs priority/enabled and formats booleans as on/off', () => {
    const s = auditSummary(
      entry({
        before_state: { priority: 50, enabled: false },
        after_state: { priority: 100, enabled: true },
      }),
    );
    expect(s).toBe('priority: 50 → 100; enabled: off → on');
  });

  it('surfaces action_override changes from inside config', () => {
    const s = auditSummary(
      entry({
        before_state: { config: {} },
        after_state: { config: { action_override: 'warn' } },
      }),
    );
    expect(s).toBe('action: default → warn');
  });

  it('falls back to "Configuration updated" when only config internals changed', () => {
    const s = auditSummary(
      entry({
        before_state: { config: { patterns: { SSN: true } } },
        after_state: { config: { patterns: { SSN: true, EMAIL: true } } },
      }),
    );
    expect(s).toBe('Configuration updated');
  });
});
