// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { ApprovalPolicy } from '../../../api/client';
import { priorityBadgeClass } from '../../../lib/rule-ordering';
import { PolicyCard } from './policy-card';

function policy(over: Partial<ApprovalPolicy> = {}): ApprovalPolicy {
  return {
    id: 'pol-1',
    tenant_id: 't1',
    mcp_server_name: 'records-mcp',
    tool_name: 'refund',
    condition_expr: { field: 'amount', op: '>', value: 5000 },
    enabled: true,
    priority: 20,
    created_at: '2026-06-20T10:00:00Z',
    updated_at: '2026-06-20T10:00:00Z',
    ...over,
  } as ApprovalPolicy;
}

function renderCard(p: ApprovalPolicy, toggling = false) {
  const onToggle = vi.fn();
  const onEdit = vi.fn();
  const onDuplicate = vi.fn();
  const onDelete = vi.fn();
  render(
    <PolicyCard
      policy={p}
      toggling={toggling}
      flash={false}
      onToggle={onToggle}
      onEdit={onEdit}
      onDuplicate={onDuplicate}
      onDelete={onDelete}
    />,
  );
  return { onToggle, onEdit, onDuplicate, onDelete };
}

afterEach(cleanup);

describe('priorityBadgeClass', () => {
  it('≥10 amber, 0 muted, else neutral', () => {
    expect(priorityBadgeClass(10)).toContain('pol-pri--hi');
    expect(priorityBadgeClass(20)).toContain('pol-pri--hi');
    expect(priorityBadgeClass(0)).toContain('pol-pri--zero');
    expect(priorityBadgeClass(5)).toBe('');
  });
});

describe('PolicyCard', () => {
  it('renders badge, target, and the condition sentence + framing', () => {
    renderCard(policy());
    expect(screen.getByText('priority 20')).toBeTruthy();
    expect(screen.getByText(/records-mcp/)).toBeTruthy();
    // conditionSentence bolds the leaf; formatValue renders 5000 bare.
    expect(screen.getByText('amount > 5000')).toBeTruthy();
    expect(screen.getByText(/pause to confirm/)).toBeTruthy();
  });

  it('wildcard tool renders * with the muted (all tools) suffix', () => {
    renderCard(policy({ tool_name: '*' }));
    expect(screen.getByText('(all tools)')).toBeTruthy();
  });

  it('disabled policy dims the body (off class) but the switch stays live', () => {
    const { onToggle } = renderCard(policy({ enabled: false }));
    expect(document.querySelector('.pol-card')?.className).toContain('off');
    const sw = screen.getByRole('switch');
    expect(sw.getAttribute('aria-checked')).toBe('false');
    expect(screen.getByText('Disabled')).toBeTruthy();
    fireEvent.click(sw);
    expect(onToggle).toHaveBeenCalled();
  });

  it('toggling disables the switch (double-click guard)', () => {
    renderCard(policy(), true);
    expect((screen.getByRole('switch') as HTMLButtonElement).disabled).toBe(true);
  });

  it('hover actions dispatch edit / duplicate / delete', () => {
    const { onEdit, onDuplicate, onDelete } = renderCard(policy());
    fireEvent.click(screen.getByTitle('Edit policy'));
    fireEvent.click(screen.getByTitle('Duplicate policy'));
    fireEvent.click(screen.getByTitle('Delete policy'));
    expect(onEdit).toHaveBeenCalled();
    expect(onDuplicate).toHaveBeenCalled();
    expect(onDelete).toHaveBeenCalled();
  });
});
