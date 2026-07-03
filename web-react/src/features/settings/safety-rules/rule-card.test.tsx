// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { SafetyCheck, SafetyRule } from '../../../api/client';
import { RuleCard } from './rule-card';

function rule(over: Partial<SafetyRule> = {}): SafetyRule {
  return {
    id: 'r1',
    tenant_id: 't1',
    coworker_id: null,
    stage: 'pre_tool_call',
    check_id: 'pii.regex',
    config: { patterns: { SSN: true } },
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

function check(over: Partial<SafetyCheck> & Pick<SafetyCheck, 'id'>): SafetyCheck {
  return {
    version: '1',
    stages: ['pre_tool_call'],
    cost_class: 'cheap',
    action_model: 'fixed',
    natural_actions: { pre_tool_call: 'block' },
    supported_actions: {},
    supported_codes: [],
    config_schema: null,
    ...over,
  } as SafetyCheck;
}

function renderCard(r: SafetyRule, c: SafetyCheck | null, cwName: string | null = null) {
  const fns = {
    onToggle: vi.fn(),
    onEdit: vi.fn(),
    onDuplicate: vi.fn(),
    onAudit: vi.fn(),
    onDelete: vi.fn(),
  };
  render(
    <RuleCard
      rule={r}
      check={c}
      coworkerName={cwName}
      toggling={false}
      flash={false}
      {...fns}
    />,
  );
  return fns;
}

afterEach(cleanup);

describe('RuleCard', () => {
  it('org card: label (never the id), sentence, action pill, all four acts', () => {
    const fns = renderCard(rule(), check({ id: 'pii.regex' }));
    expect(screen.getByText('Personal data (regex)')).toBeTruthy();
    expect(screen.queryByText('pii.regex')).toBeNull();
    expect(screen.getByText(/detect SSNs/)).toBeTruthy();
    // fixed check, natural block, no override → Block pill.
    expect(screen.getByText('Block').className).toContain('saf-act--block');
    fireEvent.click(screen.getByTitle('Edit rule'));
    fireEvent.click(screen.getByTitle(/Duplicate/));
    fireEvent.click(screen.getByTitle('Change history'));
    fireEvent.click(screen.getByTitle('Delete rule'));
    expect(fns.onEdit).toHaveBeenCalled();
    expect(fns.onDuplicate).toHaveBeenCalled();
    expect(fns.onAudit).toHaveBeenCalled();
    expect(fns.onDelete).toHaveBeenCalled();
  });

  it('platform card: accent badge, fixed-on toggle, audit-only acts', () => {
    renderCard(rule({ source: 'platform', tier: 'floor' }), check({ id: 'pii.regex' }));
    expect(document.querySelector('.pol-pri--plat')).toBeTruthy();
    const toggle = screen.getByTitle('Platform-tier rules are always enabled');
    expect(toggle.textContent).toContain('Enabled');
    expect(screen.queryByTitle('Edit rule')).toBeNull();
    expect(screen.queryByTitle('Delete rule')).toBeNull();
    expect(screen.getByTitle('Change history')).toBeTruthy();
  });

  it('slow chip when cost_class=slow; scope chip when coworker-bound', () => {
    renderCard(
      rule({ coworker_id: 'cw-1' }),
      check({ id: 'pii.regex', cost_class: 'slow' }),
      'Ops coworker',
    );
    expect(screen.getByText('slow').className).toContain('saf-chip--slow');
    expect(screen.getByText('Ops coworker').className).toContain('saf-chip');
  });

  it('action pill OMITTED for config-routed and host-list rules', () => {
    renderCard(
      rule({
        check_id: 'presidio.pii',
        config: { block_codes: ['PII.SSN'], redact_codes: [] },
      }),
      check({ id: 'presidio.pii', action_model: 'config_routed' }),
    );
    expect(document.querySelector('.saf-act')).toBeNull();
    cleanup();
    renderCard(
      rule({ check_id: 'domain_allowlist', config: { allowed_hosts: ['a.com'] } }),
      check({ id: 'domain_allowlist' }),
    );
    expect(document.querySelector('.saf-act')).toBeNull();
  });

  it('disabled rule dims the body but keeps the switch live', () => {
    const { onToggle } = renderCard(rule({ enabled: false }), check({ id: 'pii.regex' }));
    expect(document.querySelector('.pol-card')?.className).toContain('off');
    fireEvent.click(screen.getByRole('switch'));
    expect(onToggle).toHaveBeenCalled();
  });
});
