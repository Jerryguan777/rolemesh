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
import {
  priorityBadgeClass,
  sortPolicies,
} from './approval-policies-page.js';
import type { ApprovalPoliciesPage } from './approval-policies-page.js';
import type { ApprovalPolicyDialog } from './approval-policy-dialog.js';
import { conditionSentence, formatValue } from './condition-form.js';

let _idSeq = 0;
function policy(overrides: Partial<ApprovalPolicy> = {}): ApprovalPolicy {
  _idSeq += 1;
  return {
    id: `p${_idSeq}`,
    tenant_id: 't1',
    mcp_server_name: 'stripe',
    tool_name: 'charge',
    condition_expr: { field: 'amount', op: '>', value: 100 },
    enabled: true,
    priority: 0,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

async function flush(el: HTMLElement & { updateComplete: Promise<unknown> }) {
  await el.updateComplete;
  await new Promise<void>((r) => setTimeout(r, 0));
  await el.updateComplete;
}

// ── pure renderers (single source of truth shared with the list + preview) ──

describe('conditionSentence', () => {
  it('renders every time / never', () => {
    expect(conditionSentence({ always: true })).toBe('every time');
    expect(conditionSentence(null)).toBe('every time');
    expect(conditionSentence(undefined)).toBe('every time');
    expect(conditionSentence({ always: false })).toBe('never');
  });

  it('renders a single leaf, HTML-escaping the operator', () => {
    // `>` must be escaped or it would inject markup into the unsafeHTML sink.
    expect(conditionSentence({ field: 'amount', op: '>', value: 100 })).toBe(
      'when <b>amount &gt; 100</b>',
    );
  });

  it('quotes string values so "5000" is distinguishable from 5000', () => {
    // Quotes are HTML-escaped for the unsafeHTML sink; they render as `"`.
    expect(
      conditionSentence({ field: 'currency', op: '==', value: 'USD' }),
    ).toBe('when <b>currency == &quot;USD&quot;</b>');
  });

  it('joins flat AND / OR with natural-language words', () => {
    expect(
      conditionSentence({
        and: [
          { field: 'a', op: '==', value: 1 },
          { field: 'b', op: '==', value: 2 },
        ],
      }),
    ).toBe('when <b>a == 1 AND b == 2</b>');
    expect(
      conditionSentence({
        or: [
          { field: 'a', op: '==', value: 1 },
          { field: 'b', op: '==', value: 2 },
        ],
      }),
    ).toBe('when <b>a == 1 OR b == 2</b>');
  });

  it('falls back to (advanced condition) for shapes the builder cannot express', () => {
    const nested = { and: [{ or: [{ field: 'a', op: '==', value: 1 }] }] };
    expect(conditionSentence(nested)).toBe('when <i>(advanced condition)</i>');
  });
});

describe('formatValue', () => {
  it('keeps numbers / booleans / null bare and quotes strings', () => {
    expect(formatValue(5000)).toBe('5000');
    expect(formatValue(true)).toBe('true');
    expect(formatValue(null)).toBe('null');
    expect(formatValue('USD')).toBe('"USD"');
    expect(formatValue(['a', 'b'])).toBe('["a","b"]');
  });
});

describe('priorityBadgeClass', () => {
  it('is amber for high, muted for zero, neutral otherwise', () => {
    expect(priorityBadgeClass(20)).toBe('rm-pol-pri--hi');
    expect(priorityBadgeClass(10)).toBe('rm-pol-pri--hi');
    expect(priorityBadgeClass(0)).toBe('rm-pol-pri--zero');
    expect(priorityBadgeClass(5)).toBe('');
    expect(priorityBadgeClass(-3)).toBe('');
  });
});

describe('sortPolicies', () => {
  it('orders by priority desc, then created_at desc (matches server eval)', () => {
    const a = policy({ id: 'a', priority: 5, created_at: '2026-01-01T00:00:00Z' });
    const b = policy({ id: 'b', priority: 20, created_at: '2026-01-01T00:00:00Z' });
    const cOld = policy({ id: 'c', priority: 5, created_at: '2026-01-01T00:00:00Z' });
    const cNew = policy({ id: 'd', priority: 5, created_at: '2026-05-01T00:00:00Z' });
    const out = sortPolicies([a, b, cOld, cNew]);
    expect(out.map((p) => p.id)).toEqual(['b', 'd', 'a', 'c']);
  });
});

// ── list page ──

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

  async function mount(rows: ApprovalPolicy[]): Promise<ApprovalPoliciesPage> {
    listSpy.mockResolvedValue(rows);
    const node = document.createElement(
      'rm-approval-policies-page',
    ) as ApprovalPoliciesPage;
    document.body.appendChild(node);
    await flush(node);
    return node;
  }

  it('renders a priority badge, condition sentence, and (all tools) for *', async () => {
    el = await mount([policy({ priority: 7, tool_name: '*' })]);
    const row = el.querySelector('[data-testid="policy-row"]')!;
    expect(
      row.querySelector('[data-testid="policy-priority"]')?.textContent,
    ).toContain('priority 7');
    expect(row.textContent).toContain('stripe · *');
    expect(row.textContent).toContain('(all tools)');
    expect(
      row.querySelector('[data-testid="policy-sentence"]')?.textContent,
    ).toContain('amount > 100');
  });

  it('renders cards in evaluation order (priority desc, newest tie first)', async () => {
    el = await mount([
      policy({ id: 'lo', priority: 0 }),
      policy({ id: 'hi', priority: 20 }),
      policy({ id: 'mid', priority: 5 }),
    ]);
    const ids = [...el.querySelectorAll('[data-policy-id]')].map((n) =>
      n.getAttribute('data-policy-id'),
    );
    expect(ids).toEqual(['hi', 'mid', 'lo']);
  });

  it('shows the empty state with a create CTA and hides the eval-order hint', async () => {
    el = await mount([]);
    expect(el.querySelector('[data-testid="policy-empty"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="policy-hint"]')).toBeNull();
  });

  it('shows the eval-order hint only when policies exist', async () => {
    el = await mount([policy()]);
    expect(el.querySelector('[data-testid="policy-hint"]')).not.toBeNull();
  });

  it('opens the create dialog from the empty-state CTA', async () => {
    el = await mount([]);
    el
      .querySelector<HTMLButtonElement>('[data-testid="policy-empty"] button')!
      .click();
    await flush(el);
    const dialog = el.querySelector('rm-approval-policy-dialog') as ApprovalPolicyDialog;
    expect(dialog.open).toBe(true);
    expect(dialog.editing).toBeNull();
    expect(dialog.duplicating).toBeNull();
  });

  // ── enable/disable toggle (§5.3) ──

  it('optimistically flips the toggle and PATCHes enabled', async () => {
    el = await mount([policy({ id: 'p1', enabled: true })]);
    el.querySelector<HTMLButtonElement>('[data-testid="policy-toggle"]')!.click();
    await flush(el);
    expect(updateSpy).toHaveBeenCalledWith('p1', { enabled: false });
    expect(
      el.querySelector('[data-testid="policy-toggle"]')?.textContent,
    ).toContain('Disabled');
  });

  it('reverts the toggle and shows a toast when the PATCH fails', async () => {
    updateSpy.mockRejectedValue(new Error('boom'));
    el = await mount([policy({ id: 'p1', enabled: true })]);
    el.querySelector<HTMLButtonElement>('[data-testid="policy-toggle"]')!.click();
    await flush(el);
    // Reverted to the original state.
    expect(
      el.querySelector('[data-testid="policy-toggle"]')?.textContent,
    ).toContain('Enabled');
    expect(el.querySelector('[data-testid="policy-toast"]')?.textContent).toContain(
      'try again',
    );
  });

  // ── duplicate (§5.9) ──

  it('opens the dialog in duplicate (create) mode pre-filled from the source', async () => {
    el = await mount([
      policy({ id: 'src', mcp_server_name: 'erp', tool_name: 'refund', enabled: false }),
    ]);
    el.querySelector<HTMLButtonElement>('[data-testid="policy-duplicate"]')!.click();
    await flush(el);
    const dialog = el.querySelector('rm-approval-policy-dialog') as ApprovalPolicyDialog;
    expect(dialog.open).toBe(true);
    expect(dialog.editing).toBeNull();
    expect(dialog.duplicating?.id).toBe('src');
    // Saving a duplicate POSTs a new policy (never PATCHes the source).
    dialog.querySelector<HTMLButtonElement>('[data-testid="submit"]')!.click();
    await flush(el);
    expect(createSpy).toHaveBeenCalledTimes(1);
    expect(updateSpy).not.toHaveBeenCalled();
    expect(createSpy.mock.calls[0][0].mcp_server_name).toBe('erp');
    expect(createSpy.mock.calls[0][0].enabled).toBe(false);
  });

  // ── delete confirm (§5.10) ──

  it('restates the policy in the delete confirmation and deletes on confirm', async () => {
    el = await mount([
      policy({ id: 'del', mcp_server_name: 'erp', tool_name: 'refund' }),
    ]);
    el.querySelector<HTMLButtonElement>('[data-testid="policy-delete"]')!.click();
    await flush(el);
    const desc = el.querySelector('[data-testid="delete-desc"]');
    expect(desc?.textContent).toContain('erp · refund');
    expect(desc?.textContent).toContain('amount > 100');

    el
      .querySelector<HTMLElement>('[data-testid="confirm-delete-dialog"]')!
      .dispatchEvent(new CustomEvent('confirm', { bubbles: true, composed: true }));
    await flush(el);
    expect(deleteSpy).toHaveBeenCalledWith('del');
    expect(el.querySelectorAll('[data-testid="policy-row"]').length).toBe(0);
  });

  it('does not delete when the confirmation is cancelled', async () => {
    el = await mount([policy({ id: 'del' })]);
    el.querySelector<HTMLButtonElement>('[data-testid="policy-delete"]')!.click();
    await flush(el);
    el
      .querySelector<HTMLElement>('[data-testid="confirm-delete-dialog"]')!
      .dispatchEvent(new CustomEvent('cancel', { bubbles: true, composed: true }));
    await flush(el);
    expect(deleteSpy).not.toHaveBeenCalled();
    expect(el.querySelectorAll('[data-testid="policy-row"]').length).toBe(1);
  });
});

// ── dialog (§5.11-5.14) ──

describe('<rm-approval-policy-dialog>', () => {
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

  function setInput(d: ApprovalPolicyDialog, testid: string, value: string) {
    const input = d.querySelector<HTMLInputElement>(`[data-testid="${testid}"]`)!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  it('exposes Priority and Enabled at the top level (no disclosure)', async () => {
    el = await mountOpen();
    expect(el.querySelector('[data-testid="priority"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="enabled"]')).not.toBeNull();
  });

  it('creates an always-require policy by default', async () => {
    el = await mountOpen();
    setInput(el, 'mcp-server-name', 'stripe');
    el.querySelector<HTMLButtonElement>('[data-testid="submit"]')!.click();
    await flush(el);
    expect(createSpy).toHaveBeenCalledTimes(1);
    const body = createSpy.mock.calls[0][0];
    expect(body.mcp_server_name).toBe('stripe');
    expect(body.tool_name).toBe('*');
    expect(body.condition_expr).toEqual({ always: true });
  });

  it('builds a flat AND of leaves and labels the combiner naturally', async () => {
    el = await mountOpen();
    setInput(el, 'mcp-server-name', 'stripe');
    el.querySelector<HTMLButtonElement>('[data-testid="mode-match"]')!.click();
    await flush(el);
    setInput(el, 'leaf-field', 'amount');
    setInput(el, 'leaf-value', '100');
    el.querySelector<HTMLButtonElement>('[data-testid="add-row"]')!.click();
    await flush(el);
    // The combiner segmented control surfaces only with ≥2 rows.
    expect(el.querySelector('[data-testid="connective-and"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="connective-or"]')?.textContent).toContain(
      'Any (OR)',
    );
    const fields = el.querySelectorAll<HTMLInputElement>('[data-testid="leaf-field"]');
    fields[1].value = 'currency';
    fields[1].dispatchEvent(new Event('input'));
    const values = el.querySelectorAll<HTMLInputElement>('[data-testid="leaf-value"]');
    values[1].value = '"USD"';
    values[1].dispatchEvent(new Event('input'));
    await flush(el);

    el.querySelector<HTMLButtonElement>('[data-testid="submit"]')!.click();
    await flush(el);
    expect(createSpy.mock.calls[0][0].condition_expr).toEqual({
      and: [
        { field: 'amount', op: '==', value: 100 },
        { field: 'currency', op: '==', value: 'USD' },
      ],
    });
  });

  it('fails closed to {always:true} when match mode has no usable rows', async () => {
    el = await mountOpen();
    setInput(el, 'mcp-server-name', 'stripe');
    el.querySelector<HTMLButtonElement>('[data-testid="mode-match"]')!.click();
    await flush(el);
    // Leave the single leaf row empty.
    el.querySelector<HTMLButtonElement>('[data-testid="submit"]')!.click();
    await flush(el);
    expect(createSpy.mock.calls[0][0].condition_expr).toEqual({ always: true });
  });

  it('requires server + tool names before submitting', async () => {
    el = await mountOpen();
    setInput(el, 'tool-name', ''); // blank the default *
    el.querySelector<HTMLButtonElement>('[data-testid="submit"]')!.click();
    await flush(el);
    expect(createSpy).not.toHaveBeenCalled();
    expect(el.querySelector('[data-testid="form-error"]')?.textContent).toContain(
      'required',
    );
  });

  it('live-previews the rule and appends a disabled note when off', async () => {
    el = await mountOpen();
    setInput(el, 'mcp-server-name', 'erp');
    setInput(el, 'tool-name', 'refund');
    setInput(el, 'priority', '5');
    await flush(el);
    const preview = el.querySelector('[data-testid="policy-preview"]')!;
    expect(preview.textContent).toContain('erp · refund');
    expect(preview.textContent).toContain('Priority 5');
    expect(preview.textContent).not.toContain('disabled');

    el.querySelector<HTMLButtonElement>('[data-testid="enabled"]')!.click();
    await flush(el);
    expect(
      el.querySelector('[data-testid="policy-preview"]')?.textContent,
    ).toContain('disabled');
  });

  it('labels title + save button per flow', async () => {
    el = await mountOpen();
    expect(el.querySelector('[data-testid="submit"]')?.textContent).toContain(
      'Create policy',
    );
    el.remove();
    el = await mountOpen({ editing: policy({ id: 'e' }) });
    expect(el.querySelector('[data-testid="submit"]')?.textContent).toContain(
      'Save changes',
    );
  });

  it('opens a complex stored condition read-only and leaves it untouched on save', async () => {
    const complex = { and: [{ or: [{ field: 'a', op: '==', value: 1 }] }] };
    el = await mountOpen({
      editing: policy({ id: 'pX', condition_expr: complex }),
    });
    expect(el.querySelector('[data-testid="condition-readonly"]')).not.toBeNull();
    el.querySelector<HTMLButtonElement>('[data-testid="submit"]')!.click();
    await flush(el);
    expect(updateSpy).toHaveBeenCalledTimes(1);
    // condition_expr is NOT in the patch body — the stored complex expr stays.
    expect(updateSpy.mock.calls[0][1].condition_expr).toBeUndefined();
  });

  it('emits approval-policy-saved with the returned policy', async () => {
    const returned = policy({ id: 'new-1' });
    createSpy.mockResolvedValue(returned);
    el = await mountOpen();
    setInput(el, 'mcp-server-name', 'stripe');
    const saved = vi.fn();
    el.addEventListener('approval-policy-saved', saved as EventListener);
    el.querySelector<HTMLButtonElement>('[data-testid="submit"]')!.click();
    await flush(el);
    expect(saved).toHaveBeenCalledTimes(1);
    const detail = (saved.mock.calls[0][0] as CustomEvent).detail;
    expect(detail.policy.id).toBe('new-1');
  });
});
