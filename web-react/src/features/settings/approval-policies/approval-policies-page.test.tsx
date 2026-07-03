// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ApprovalPolicy } from '../../../api/client';
import { ApprovalPoliciesPage, sortPolicies } from './approval-policies-page';

function policy(over: Partial<ApprovalPolicy>): ApprovalPolicy {
  return {
    id: 'pol-x',
    tenant_id: 't1',
    mcp_server_name: 'records-mcp',
    tool_name: 'refund',
    condition_expr: { always: true },
    enabled: true,
    priority: 0,
    created_at: '2026-06-20T10:00:00Z',
    updated_at: '2026-06-20T10:00:00Z',
    ...over,
  } as ApprovalPolicy;
}

function renderPage(seed: ApprovalPolicy[]) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  qc.setQueryData(['approval-policies'], seed);
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ApprovalPoliciesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(cleanup);

describe('sortPolicies', () => {
  it('orders priority desc, created_at desc on ties (server evaluation order)', () => {
    const rows = [
      policy({ id: 'a', priority: 0, created_at: '2026-06-25T10:00:00Z' }),
      policy({ id: 'b', priority: 20, created_at: '2026-06-20T10:00:00Z' }),
      policy({ id: 'c', priority: 0, created_at: '2026-06-26T10:00:00Z' }),
    ];
    expect(sortPolicies(rows).map((r) => r.id)).toEqual(['b', 'c', 'a']);
  });
});

describe('ApprovalPoliciesPage', () => {
  it('renders cards in evaluation order with priority badges', () => {
    renderPage([
      policy({ id: 'lo', priority: 0, tool_name: 'a_tool' }),
      policy({ id: 'hi', priority: 20, tool_name: 'z_tool' }),
    ]);
    const cards = document.querySelectorAll('.pol-card');
    expect(cards.length).toBe(2);
    expect(cards[0].getAttribute('data-policy-id')).toBe('hi');
    expect(within(cards[0] as HTMLElement).getByText('priority 20')).toBeTruthy();
  });

  it('shows the evaluation-order hint (5-minute timeout copy) under a non-empty list', () => {
    renderPage([policy({})]);
    const hint = document.querySelector('.page-hint');
    expect(hint?.textContent).toContain('highest priority wins');
    expect(hint?.textContent).toContain('5 minutes');
  });

  it('empty list: hint hidden, empty state carries the true behavior + CTA', () => {
    renderPage([]);
    expect(document.querySelector('.page-hint')).toBeNull();
    expect(screen.getByText('No approval policies yet.')).toBeTruthy();
    expect(screen.getByText(/Every tool call runs without asking/)).toBeTruthy();
    expect(screen.getByText('Create your first policy')).toBeTruthy();
  });

  it('delete confirm restates the rule via the sentence renderer + consequences', () => {
    renderPage([
      policy({
        tool_name: '*',
        condition_expr: { field: 'amount', op: '>', value: 5000 },
      }),
    ]);
    fireEvent.click(screen.getByTitle('Delete policy'));
    const dlg = screen.getByRole('alertdialog');
    expect(within(dlg).getByText(/records-mcp · any tool/)).toBeTruthy();
    expect(within(dlg).getByText('amount > 5000')).toBeTruthy();
    expect(
      within(dlg).getByText(/matching calls will run without asking/),
    ).toBeTruthy();
    expect(within(dlg).getByText(/stay live until decided or expired/)).toBeTruthy();
    expect(within(dlg).getByText('Delete policy')).toBeTruthy();
  });

  it('optimistic toggle: flips immediately, reverts + toasts when the PATCH fails', async () => {
    renderPage([policy({ id: 'p1', enabled: true })]);
    const sw = screen.getByRole('switch');
    expect(sw.getAttribute('aria-checked')).toBe('true');
    fireEvent.click(sw);
    // Optimistic flip is synchronous.
    expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('false');
    // No backend in the test env → the PATCH rejects → revert + toast.
    await screen.findByText('Couldn’t update — try again');
    expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('true');
  });
});
