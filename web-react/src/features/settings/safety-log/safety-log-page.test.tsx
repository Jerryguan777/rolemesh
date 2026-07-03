// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { SafetyCheck, SafetyDecision, SafetyRule } from '../../../api/client';
import { SafetyLogPage } from './safety-log-page';

function decision(over: Partial<SafetyDecision>): SafetyDecision {
  return {
    id: 'dec-x',
    tenant_id: 't1',
    coworker_id: null,
    conversation_id: null,
    job_id: null,
    stage: 'pre_tool_call',
    verdict_action: 'allow',
    triggered_rule_ids: [],
    findings: [],
    context_digest: 'sha256:abc',
    context_summary: 'tool ran clean',
    source: 'tenant',
    created_at: '2026-07-01T10:15:30Z',
    ...over,
  } as SafetyDecision;
}

const RULES: SafetyRule[] = [
  {
    id: 'sr-1',
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
  } as SafetyRule,
];

const CHECKS: SafetyCheck[] = [
  {
    id: 'pii.regex',
    version: '1',
    stages: ['pre_tool_call'],
    cost_class: 'cheap',
    action_model: 'fixed',
    natural_actions: {},
    supported_actions: {},
    supported_codes: [],
    config_schema: null,
  } as unknown as SafetyCheck,
];

function renderPage(opts: {
  items: SafetyDecision[];
  total?: number;
  initialEntry?: string;
}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  // Page-0 default query key (all filters empty).
  qc.setQueryData(
    [
      'safety-decisions',
      {
        verdictAction: null,
        stage: null,
        coworkerId: null,
        checkId: null,
        ruleId: null,
        fromTs: null,
        toTs: null,
        limit: 10,
        offset: 0,
      },
    ],
    { items: opts.items, total: opts.total ?? opts.items.length, limit: 10, offset: 0 },
  );
  qc.setQueryData(['safety-checks'], CHECKS);
  qc.setQueryData(['safety-rules'], RULES);
  qc.setQueryData(['coworkers'], [{ id: 'cw-1', name: 'Ops coworker' }]);
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[opts.initialEntry ?? '/manage/safety-log']}>
        <SafetyLogPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(cleanup);

describe('SafetyLogPage', () => {
  it('renders rows: mono timestamp, verdict pill, finding codes (no check_id on the wire)', () => {
    renderPage({
      items: [
        decision({
          id: 'd1',
          verdict_action: 'block',
          findings: [
            { code: 'PII.SSN', severity: 'high', message: 'SSN found', metadata: null },
          ],
        }),
      ],
    });
    const row = document.querySelector('.log-row')!;
    expect(within(row as HTMLElement).getByText('10:15:30')).toBeTruthy();
    expect(within(row as HTMLElement).getByText('Block').className).toContain(
      'saf-act--block',
    );
    expect(within(row as HTMLElement).getByText('PII.SSN')).toBeTruthy();
    expect(within(row as HTMLElement).getByText('organization-wide')).toBeTruthy();
  });

  it('pager shows in-band total; edges disable', () => {
    renderPage({ items: [decision({ id: 'd1' })], total: 23 });
    const pager = screen.getByTestId('log-pager');
    expect(pager.textContent).toContain('Showing 1–10 of 23');
    expect(
      (within(pager).getByText('← Previous') as HTMLButtonElement).disabled,
    ).toBe(true);
    expect((within(pager).getByText('Next →') as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it('deep link ?rule_id renders the chip with the rule check label; × clears it', () => {
    renderPage({ items: [], initialEntry: '/manage/safety-log?rule_id=sr-1' });
    const chip = screen.getByTestId('rule-chip');
    expect(chip.textContent).toContain('Rule: Personal data (regex)');
    fireEvent.click(within(chip).getByLabelText('Clear rule filter'));
    expect(screen.queryByTestId('rule-chip')).toBeNull();
  });

  it('deep link ?check_id pre-selects the check dropdown', () => {
    renderPage({ items: [], initialEntry: '/manage/safety-log?check_id=pii.regex' });
    expect((screen.getByLabelText('Check filter') as HTMLSelectElement).value).toBe(
      'pii.regex',
    );
  });

  it('Clear filters resets everything, rule chip included', () => {
    renderPage({ items: [], initialEntry: '/manage/safety-log?rule_id=sr-1&check_id=pii.regex' });
    fireEvent.click(screen.getByText('Clear filters'));
    expect(screen.queryByTestId('rule-chip')).toBeNull();
    expect((screen.getByLabelText('Check filter') as HTMLSelectElement).value).toBe('');
  });

  it('D-J1: custom range reveals the datetime-local pair', () => {
    renderPage({ items: [] });
    fireEvent.change(screen.getByTestId('log-range'), { target: { value: 'custom' } });
    expect(screen.getByTestId('log-from')).toBeTruthy();
    expect(screen.getByTestId('log-to')).toBeTruthy();
  });

  it('empty state assumes over-narrow filters', () => {
    renderPage({ items: [] });
    expect(screen.getByText('No decisions match your filters.')).toBeTruthy();
    expect(screen.getByText(/wait for new agent activity/)).toBeTruthy();
  });

  it('row click opens the detail modal: platform chip, deleted-rule fallback, findings, dm-note', () => {
    renderPage({
      items: [
        decision({
          id: 'd9',
          verdict_action: 'redact',
          source: 'platform',
          triggered_rule_ids: ['sr-1', 'sr-gone'],
          findings: [
            {
              code: 'PII.EMAIL',
              severity: 'medium',
              message: 'Email redacted',
              metadata: { count: 2 },
            },
          ],
        }),
      ],
    });
    fireEvent.click(document.querySelector('.log-row')!);
    const dlg = screen.getByRole('dialog');
    const text = dlg.textContent ?? '';
    expect(text).toContain('Decision d9');
    expect(within(dlg).getByText('platform rule')).toBeTruthy();
    expect(text).toContain('Personal data (regex)'); // resolved rule
    expect(text).toContain('deleted rule'); // unknown id fallback
    expect(within(dlg).getByText('PII.EMAIL')).toBeTruthy();
    expect(within(dlg).getByText('medium').className).toContain('sev--medium');
    expect(text).toContain('{"count":2}');
    expect(text).toContain('Data minimization');
    expect(text).toContain('10:15:30'); // dm-note anchored on the timestamp
  });

  it('zero findings renders the allow explainer', () => {
    renderPage({ items: [decision({ id: 'd2', findings: [] })] });
    fireEvent.click(document.querySelector('.log-row')!);
    expect(screen.getByText(/No findings — check ran and verdict was/)).toBeTruthy();
  });
});
