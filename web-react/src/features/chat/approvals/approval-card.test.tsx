// @vitest-environment happy-dom
//
// Re-expression of web/src/components/approval-card.test.ts for the
// React card: params/rationale rendering, countdown urgency, the
// reject-note flow, busy-guard, resolved presentation, and the O.5
// safety banner (incl. the Part J deep link).
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { ApprovalCard as CardData } from '../../../lib/approval-cards';
import { ApprovalCard } from './approval-card';

function card(over: Partial<CardData> = {}): CardData {
  return {
    requestId: 'req-1',
    mcpServerName: 'stripe',
    toolName: 'charge',
    params: { amount: 6200, currency: 'USD' },
    coworkerId: 'cw-1',
    rationale: null,
    requestedAt: new Date(Date.now() - 40_000).toISOString(),
    expiresAt: new Date(Date.now() + 18 * 60_000).toISOString(),
    actionSummary: null,
    status: 'pending',
    resolvedAt: null,
    note: null,
    triggeredBy: null,
    orderTs: Date.now(),
    ...over,
  };
}

function renderCard(
  data: CardData,
  over: Partial<Parameters<typeof ApprovalCard>[0]> = {},
) {
  const onDecide = vi.fn();
  const utils = render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route
          path="/"
          element={
            <ApprovalCard
              card={data}
              busy={false}
              coworkerName="Rex"
              pendingOthers={0}
              highlighted={false}
              now={Date.now()}
              onDecide={onDecide}
              onBackToInbox={() => {}}
              onClearHighlight={() => {}}
              {...over}
            />
          }
        />
        <Route path="/manage/safety-log" element={<div data-testid="safety-log-landing" />} />
      </Routes>
    </MemoryRouter>,
  );
  return { onDecide, ...utils };
}

afterEach(cleanup);

describe('ApprovalCard pending', () => {
  it('renders the tool chip, meta, and both action buttons', () => {
    renderCard(card());
    expect(screen.getByTestId('approval-tool').textContent).toBe('stripe · charge');
    expect(screen.getByTestId('approval-meta').textContent).toContain('Rex coworker');
    expect(screen.getByTestId('approval-approve')).toBeTruthy();
    expect(screen.getByTestId('approval-reject')).toBeTruthy();
    expect(screen.getByTestId('approval-status').textContent).toBe('Approval needed');
  });

  it('renders one params row per entry, strings quoted and numbers bare', () => {
    renderCard(card());
    const rows = screen.getAllByTestId('approval-param-row');
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain('amount');
    expect(rows[0].textContent).toContain('6200');
    expect(rows[1].textContent).toContain('"USD"');
  });

  it('omits the params block entirely when params is empty', () => {
    renderCard(card({ params: {} }));
    expect(screen.queryByTestId('approval-params')).toBeNull();
  });

  it('collapses params to 6 behind a disclosure only past the Lit >8 threshold', () => {
    const many = Object.fromEntries(
      Array.from({ length: 9 }, (_, i) => [`k${i}`, i]),
    );
    renderCard(card({ params: many }));
    expect(screen.getAllByTestId('approval-param-row').length).toBe(6);
    fireEvent.click(screen.getByTestId('approval-params-toggle'));
    expect(screen.getAllByTestId('approval-param-row').length).toBe(9);
  });

  it('shows all 8 params at the threshold (no disclosure)', () => {
    const eight = Object.fromEntries(
      Array.from({ length: 8 }, (_, i) => [`k${i}`, i]),
    );
    renderCard(card({ params: eight }));
    expect(screen.getAllByTestId('approval-param-row').length).toBe(8);
    expect(screen.queryByTestId('approval-params-toggle')).toBeNull();
  });

  it('renders the rationale when present and omits it when null/blank', () => {
    renderCard(card({ rationale: 'duplicate charge cleanup' }));
    expect(screen.getByTestId('approval-rationale').textContent).toContain(
      'duplicate charge cleanup',
    );
    cleanup();
    renderCard(card({ rationale: null }));
    expect(screen.queryByTestId('approval-rationale')).toBeNull();
    cleanup();
    renderCard(card({ rationale: '   ' }));
    expect(screen.queryByTestId('approval-rationale')).toBeNull();
  });

  it('countdown is minutes + not urgent above 5 minutes, urgent below, expired past zero', () => {
    renderCard(card({ expiresAt: new Date(Date.now() + 18 * 60_000).toISOString() }));
    let cd = screen.getByTestId('approval-countdown');
    expect(cd.textContent).toMatch(/m left$/);
    expect(cd.dataset.urgent).toBe('false');
    cleanup();
    renderCard(card({ expiresAt: new Date(Date.now() + 4 * 60_000).toISOString() }));
    cd = screen.getByTestId('approval-countdown');
    expect(cd.dataset.urgent).toBe('true');
    cleanup();
    renderCard(card({ expiresAt: new Date(Date.now() - 1000).toISOString() }));
    expect(screen.getByTestId('approval-countdown').textContent).toBe('expired');
  });

  it('approve emits immediately with no note', () => {
    const { onDecide } = renderCard(card());
    fireEvent.click(screen.getByTestId('approval-approve'));
    expect(onDecide).toHaveBeenCalledWith('approve');
  });

  it('a single Reject click opens the note form and does NOT emit', () => {
    const { onDecide } = renderCard(card());
    fireEvent.click(screen.getByTestId('approval-reject'));
    expect(screen.getByTestId('approval-reject-form')).toBeTruthy();
    expect(onDecide).not.toHaveBeenCalled();
  });

  it('the form Reject submits the trimmed note; empty note omits the field', () => {
    const { onDecide } = renderCard(card());
    fireEvent.click(screen.getByTestId('approval-reject'));
    fireEvent.change(screen.getByTestId('approval-note'), {
      target: { value: '  too high  ' },
    });
    fireEvent.click(screen.getByTestId('approval-reject-confirm'));
    expect(onDecide).toHaveBeenCalledWith('reject', 'too high');
    cleanup();
    const second = renderCard(card());
    fireEvent.click(screen.getByTestId('approval-reject'));
    fireEvent.click(screen.getByTestId('approval-reject-confirm'));
    expect(second.onDecide).toHaveBeenCalledWith('reject', undefined);
  });

  it('Cancel collapses the form without emitting', () => {
    const { onDecide } = renderCard(card());
    fireEvent.click(screen.getByTestId('approval-reject'));
    fireEvent.click(screen.getByTestId('approval-reject-cancel'));
    expect(screen.queryByTestId('approval-reject-form')).toBeNull();
    expect(onDecide).not.toHaveBeenCalled();
  });

  it('busy blocks both approve and the reject form (double-tap guard)', () => {
    const { onDecide } = renderCard(card(), { busy: true });
    fireEvent.click(screen.getByTestId('approval-approve'));
    fireEvent.click(screen.getByTestId('approval-reject'));
    expect(onDecide).not.toHaveBeenCalled();
    expect(screen.queryByTestId('approval-reject-form')).toBeNull();
  });
});

describe('ApprovalCard resolved', () => {
  it('renders the outcome pill + at-time and no decision buttons', () => {
    renderCard(card({ status: 'approved', resolvedAt: Date.parse('2026-07-03T14:05:00') }));
    expect(screen.getByTestId('approval-status').textContent).toBe('Approved');
    expect(screen.getByTestId('approval-resolved-time').textContent).toMatch(/^at /);
    expect(screen.queryByTestId('approval-approve')).toBeNull();
    expect(screen.queryByTestId('approval-reject')).toBeNull();
  });

  it('echoes the note as YOUR REASON only on a rejected card', () => {
    renderCard(card({ status: 'rejected', resolvedAt: Date.now(), note: 'nope' }));
    expect(screen.getByTestId('approval-resolved-note').textContent).toContain('nope');
    cleanup();
    renderCard(card({ status: 'approved', resolvedAt: Date.now(), note: 'nope' }));
    expect(screen.queryByTestId('approval-resolved-note')).toBeNull();
    cleanup();
    renderCard(card({ status: 'rejected', resolvedAt: Date.now(), note: null }));
    expect(screen.queryByTestId('approval-resolved-note')).toBeNull();
  });

  it('compacts params to the first 2 with a Show-all disclosure', () => {
    renderCard(
      card({ status: 'expired', resolvedAt: Date.now(), params: { a: 1, b: 2, c: 3 } }),
    );
    expect(screen.getAllByTestId('approval-param-row').length).toBe(2);
    fireEvent.click(screen.getByTestId('approval-params-toggle'));
    expect(screen.getAllByTestId('approval-param-row').length).toBe(3);
  });

  it('shows the back-to-inbox link only while other approvals pend', () => {
    renderCard(card({ status: 'approved', resolvedAt: Date.now() }), {
      pendingOthers: 2,
    });
    expect(screen.getByTestId('approval-back-to-inbox').textContent).toContain('2 more');
    cleanup();
    renderCard(card({ status: 'approved', resolvedAt: Date.now() }), {
      pendingOthers: 0,
    });
    expect(screen.queryByTestId('approval-back-to-inbox')).toBeNull();
  });

  it('Timed out and Cancelled render their muted labels', () => {
    renderCard(card({ status: 'expired', resolvedAt: Date.now() }));
    expect(screen.getByTestId('approval-status').textContent).toBe('Timed out');
    cleanup();
    renderCard(card({ status: 'cancelled', resolvedAt: Date.now() }));
    expect(screen.getByTestId('approval-status').textContent).toBe('Cancelled');
  });
});

describe('ApprovalCard safety banner (O.5)', () => {
  const tb = { kind: 'safety_rule', rule_id: 'sr-9', check_id: 'pii.regex' };

  it('renders the banner with the catalog label and deep-links into the safety log', () => {
    renderCard(card({ triggeredBy: tb as CardData['triggeredBy'] }));
    const banner = screen.getByTestId('approval-safety-banner');
    expect(banner.textContent).toContain('Paused by a safety rule');
    expect(banner.textContent).not.toContain('pii.regex'); // human label, not the id
    fireEvent.click(screen.getByTestId('approval-safety-link'));
    expect(screen.getByTestId('safety-log-landing')).toBeTruthy();
  });

  it('renders no banner for a business-policy approval (null)', () => {
    renderCard(card({ triggeredBy: null }));
    expect(screen.queryByTestId('approval-safety-banner')).toBeNull();
  });

  it('degrades to no banner on an unknown kind (forward compat)', () => {
    renderCard(
      card({
        triggeredBy: { kind: 'scheduled_task', rule_id: 'x', check_id: 'y' } as unknown as CardData['triggeredBy'],
      }),
    );
    expect(screen.queryByTestId('approval-safety-banner')).toBeNull();
  });
});
