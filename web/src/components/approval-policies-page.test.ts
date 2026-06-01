// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApprovalPolicy } from '../api/client.js';

const {
  listSpy,
  createSpy,
  updateSpy,
  deleteSpy,
} = vi.hoisted(() => ({
  listSpy: vi.fn(),
  createSpy: vi.fn(),
  updateSpy: vi.fn(),
  deleteSpy: vi.fn(),
}));

vi.mock('../api/client.js', async () => {
  const actual =
    await vi.importActual<typeof import('../api/client.js')>('../api/client.js');
  return {
    ...actual,
    getApiClient: () => ({
      listApprovalPolicies: listSpy,
      createApprovalPolicy: createSpy,
      updateApprovalPolicy: updateSpy,
      deleteApprovalPolicy: deleteSpy,
    }),
  };
});

import './approval-policies-page.js';
import './approval-policy-dialog.js';
import { summarizeCondition } from './approval-policies-page.js';
import type { ApprovalPoliciesPage } from './approval-policies-page.js';
import type { ApprovalPolicyDialog } from './approval-policy-dialog.js';

function policy(overrides: Partial<ApprovalPolicy> = {}): ApprovalPolicy {
  return {
    id: 'p1',
    tenant_id: 't1',
    mcp_server_name: 'stripe',
    tool_name: 'charge',
    condition_expr: { field: 'amount', op: '>', value: 100 },
    enabled: true,
    priority: 0,
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

async function flush(el: HTMLElement & { updateComplete: Promise<unknown> }) {
  await el.updateComplete;
  await new Promise<void>((r) => setTimeout(r, 0));
  await el.updateComplete;
}

describe('summarizeCondition', () => {
  it('renders always / never', () => {
    expect(summarizeCondition({ always: true })).toBe('always');
    expect(summarizeCondition({ always: false })).toBe('never');
  });
  it('renders a leaf', () => {
    expect(summarizeCondition({ field: 'amount', op: '>', value: 100 })).toBe(
      'amount > 100',
    );
  });
  it('renders a connective', () => {
    expect(
      summarizeCondition({ or: [
        { field: 'a', op: '==', value: 1 },
        { field: 'b', op: '==', value: 2 },
      ] }),
    ).toBe('a == 1 or b == 2');
  });
});

describe('<rm-approval-policies-page>', () => {
  let el: ApprovalPoliciesPage | null = null;
  beforeEach(() => {
    listSpy.mockReset().mockResolvedValue([]);
    createSpy.mockReset().mockResolvedValue(policy());
    updateSpy.mockReset().mockResolvedValue(policy());
    deleteSpy.mockReset().mockResolvedValue(undefined);
  });
  afterEach(() => {
    el?.remove();
    el = null;
  });

  it('lists policies from the API with a condition summary', async () => {
    listSpy.mockResolvedValue([policy({ priority: 7 })]);
    el = document.createElement('rm-approval-policies-page') as ApprovalPoliciesPage;
    document.body.appendChild(el);
    await flush(el);
    expect(listSpy).toHaveBeenCalledTimes(1);
    const row = el.querySelector('[data-testid="policy-row"]');
    expect(row?.textContent).toContain('stripe · charge');
    expect(row?.textContent).toContain('amount > 100');
    expect(row?.textContent).toContain('priority 7');
  });

  it('shows the empty state when there are no policies', async () => {
    el = document.createElement('rm-approval-policies-page') as ApprovalPoliciesPage;
    document.body.appendChild(el);
    await flush(el);
    expect(el.querySelector('.rm-empty')).not.toBeNull();
  });
});

describe('<rm-approval-policy-dialog> create flow', () => {
  let el: ApprovalPolicyDialog | null = null;
  beforeEach(() => {
    createSpy.mockReset().mockResolvedValue(policy());
    updateSpy.mockReset().mockResolvedValue(policy());
  });
  afterEach(() => {
    el?.remove();
    el = null;
  });

  async function mountOpen(
    props: Partial<ApprovalPolicyDialog> = {},
  ): Promise<ApprovalPolicyDialog> {
    const d = document.createElement(
      'rm-approval-policy-dialog',
    ) as ApprovalPolicyDialog;
    Object.assign(d, { open: true, ...props });
    document.body.appendChild(d);
    await flush(d);
    return d;
  }

  it('creates an always-require policy by default', async () => {
    el = await mountOpen();
    el.querySelector<HTMLInputElement>('[data-testid="mcp-server-name"]')!.value =
      'stripe';
    el
      .querySelector<HTMLInputElement>('[data-testid="mcp-server-name"]')!
      .dispatchEvent(new Event('input'));
    el.querySelector<HTMLButtonElement>('[data-testid="submit"]')!.click();
    await flush(el);
    expect(createSpy).toHaveBeenCalledTimes(1);
    const body = createSpy.mock.calls[0][0];
    expect(body.mcp_server_name).toBe('stripe');
    expect(body.tool_name).toBe('*');
    expect(body.condition_expr).toEqual({ always: true });
  });

  it('builds a match-condition from the leaf rows', async () => {
    el = await mountOpen();
    // server name
    const name = el.querySelector<HTMLInputElement>(
      '[data-testid="mcp-server-name"]',
    )!;
    name.value = 'stripe';
    name.dispatchEvent(new Event('input'));
    // switch to match mode
    el.querySelector<HTMLInputElement>('[data-testid="mode-match"]')!.click();
    await flush(el);
    // fill the single leaf row
    const field = el.querySelector<HTMLInputElement>('[data-testid="leaf-field"]')!;
    field.value = 'amount';
    field.dispatchEvent(new Event('input'));
    const value = el.querySelector<HTMLInputElement>('[data-testid="leaf-value"]')!;
    value.value = '100';
    value.dispatchEvent(new Event('input'));
    const op = el.querySelector<HTMLSelectElement>('[data-testid="leaf-op"]')!;
    op.value = '>';
    op.dispatchEvent(new Event('change'));
    await flush(el);

    el.querySelector<HTMLButtonElement>('[data-testid="submit"]')!.click();
    await flush(el);
    expect(createSpy).toHaveBeenCalledTimes(1);
    expect(createSpy.mock.calls[0][0].condition_expr).toEqual({
      field: 'amount',
      op: '>',
      value: 100,
    });
  });

  it('requires server + tool names before submitting', async () => {
    el = await mountOpen();
    // tool_name defaults to "*", but server name is blank → blocked.
    el.querySelector<HTMLButtonElement>('[data-testid="submit"]')!.click();
    await flush(el);
    expect(createSpy).not.toHaveBeenCalled();
    expect(
      el.querySelector('[data-testid="form-error"]')?.textContent,
    ).toContain('required');
  });

  it('opens a complex stored condition read-only and leaves it untouched on save', async () => {
    const complex = { and: [{ or: [{ field: 'a', op: '==', value: 1 }] }] };
    el = await mountOpen({
      editing: policy({ id: 'pX', condition_expr: complex }),
    });
    // The flat builder can't represent the nested condition.
    expect(el.querySelector('[data-testid="condition-readonly"]')).not.toBeNull();
    el.querySelector<HTMLButtonElement>('[data-testid="submit"]')!.click();
    await flush(el);
    expect(updateSpy).toHaveBeenCalledTimes(1);
    // condition_expr is NOT in the patch body — the stored complex expr stays.
    expect(updateSpy.mock.calls[0][1].condition_expr).toBeUndefined();
  });
});
