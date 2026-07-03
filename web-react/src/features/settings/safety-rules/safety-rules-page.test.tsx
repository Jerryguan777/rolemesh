// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { SafetyCheck, SafetyRule } from '../../../api/client';
import { SafetyRulesPage } from './safety-rules-page';

function rule(over: Partial<SafetyRule>): SafetyRule {
  return {
    id: 'r-x',
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

const CHECKS: SafetyCheck[] = [
  {
    id: 'pii.regex',
    version: '1',
    stages: ['pre_tool_call'],
    cost_class: 'cheap',
    action_model: 'fixed',
    natural_actions: { pre_tool_call: 'block' },
    supported_actions: {},
    supported_codes: [],
    config_schema: null,
  } as unknown as SafetyCheck,
];

function renderPage(rules: SafetyRule[]) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  qc.setQueryData(['safety-rules'], rules);
  qc.setQueryData(['safety-checks'], CHECKS);
  qc.setQueryData(['coworkers'], [{ id: 'cw-1', name: 'Ops coworker' }]);
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SafetyRulesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(cleanup);

describe('SafetyRulesPage', () => {
  it('two tiers never interleave: platform (banner) first, then org section', () => {
    renderPage([
      rule({ id: 'org-hi', priority: 999 }),
      rule({ id: 'plat-lo', priority: 1, source: 'platform', tier: 'floor' }),
    ]);
    expect(screen.getByText(/Platform defaults/)).toBeTruthy();
    expect(screen.getByText("Your organization's rules")).toBeTruthy();
    const cards = [...document.querySelectorAll('[data-rule-id]')];
    // Platform card first despite lower priority — tiers don't interleave.
    expect(cards[0].getAttribute('data-rule-id')).toBe('plat-lo');
    expect(cards[1].getAttribute('data-rule-id')).toBe('org-hi');
  });

  it('no platform rules → no banner, no section label', () => {
    renderPage([rule({ id: 'o1' })]);
    expect(screen.queryByText(/Platform defaults/)).toBeNull();
    expect(screen.queryByText("Your organization's rules")).toBeNull();
  });

  it('hint carries snapshot semantics in plain words; hidden when empty', () => {
    renderPage([rule({ id: 'o1' })]);
    expect(
      screen.getByText(/tasks already in progress keep the rules they started with/),
    ).toBeTruthy();
    cleanup();
    renderPage([]);
    expect(document.querySelector('.page-hint')).toBeNull();
    expect(screen.getByText('No safety rules yet.')).toBeTruthy();
    expect(screen.getByText(/no automatic guardrails/)).toBeTruthy();
  });

  it('delete confirm: four parts in everyday language', () => {
    renderPage([rule({ id: 'o1' })]);
    fireEvent.click(screen.getByTitle('Delete rule'));
    const dlg = screen.getByRole('alertdialog');
    const text = dlg.textContent ?? '';
    // 1. which rule (label + sentence)
    expect(within(dlg).getByText('Personal data (regex)')).toBeTruthy();
    expect(text).toContain('detect SSNs');
    // 2. new tasks stop running the check
    expect(text).toMatch(/stops running on new agent tasks/);
    // 3. already-running tasks keep it (expected, not a bug)
    expect(text).toMatch(/keep using this rule until they finish/);
    // 4. what survives (log entries + change history)
    expect(text).toMatch(/safety log entries are kept/i);
    expect(text).toMatch(/change history .* is also kept/i);
    expect(within(dlg).getByText('Delete rule')).toBeTruthy();
  });
});
