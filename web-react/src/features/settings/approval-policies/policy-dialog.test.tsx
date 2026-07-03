// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ApprovalPolicy, MCPServer } from '../../../api/client';
import { PolicyDialog } from './policy-dialog';

const SERVERS: MCPServer[] = [
  {
    id: 'mcp-1',
    tenant_id: 't1',
    name: 'records-mcp',
    type: 'http',
    url: 'http://records/mcp',
    auth_mode: 'service',
    description: null,
    tool_reversibility: { lookup_customer: true, refund: false },
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  } as unknown as MCPServer,
];

function renderDialog(opts: {
  editing?: ApprovalPolicy | null;
  duplicating?: ApprovalPolicy | null;
}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  qc.setQueryData(['mcp-servers'], SERVERS);
  const onClose = vi.fn();
  const onSaved = vi.fn();
  render(
    <QueryClientProvider client={qc}>
      <PolicyDialog
        editing={opts.editing ?? null}
        duplicating={opts.duplicating ?? null}
        onClose={onClose}
        onSaved={onSaved}
      />
    </QueryClientProvider>,
  );
  return { onClose, onSaved };
}

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

afterEach(cleanup);

describe('PolicyDialog', () => {
  it('create mode: defaults + Every time selected + Create label', () => {
    renderDialog({});
    expect(screen.getByText('New approval policy')).toBeTruthy();
    expect(screen.getByText('Create policy')).toBeTruthy();
    const always = screen.getByText('Every time');
    expect(always.className).toContain('on');
    // No condition rows in always-mode.
    expect(document.querySelector('.cond-row')).toBeNull();
    // Tool defaults to the wildcard.
    expect((screen.getByLabelText('Tool') as HTMLInputElement).value).toBe('*');
  });

  it('Only when… reveals combiner + rows; typing updates the live preview', () => {
    renderDialog({});
    fireEvent.change(screen.getByLabelText('MCP server'), {
      target: { value: 'records-mcp' },
    });
    fireEvent.click(screen.getByText('Only when…'));
    expect(screen.getByText('All (AND)')).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText('field (e.g. amount)'), {
      target: { value: 'amount' },
    });
    fireEvent.change(screen.getByLabelText('Operator'), { target: { value: '>' } });
    fireEvent.change(screen.getByPlaceholderText('value (e.g. 5000)'), {
      target: { value: '5000' },
    });
    const preview = screen.getByTestId('policy-preview');
    expect(preview.textContent).toContain('records-mcp');
    expect(preview.textContent).toContain('amount > 5000');
    expect(preview.textContent).toContain('pause to confirm');
  });

  it('empty when-rows preview falls closed to "every time" (never approve-nothing)', () => {
    renderDialog({});
    fireEvent.click(screen.getByText('Only when…'));
    expect(screen.getByTestId('policy-preview').textContent).toContain('every time');
  });

  it('save gate: server + tool required', () => {
    renderDialog({});
    fireEvent.change(screen.getByLabelText('MCP server'), { target: { value: '  ' } });
    fireEvent.click(screen.getByText('Create policy'));
    expect(screen.getByRole('alert').textContent).toBe(
      'MCP server name and tool name are required.',
    );
  });

  it('tool datalist offers * + the declared tool_reversibility keys', () => {
    renderDialog({});
    fireEvent.change(screen.getByLabelText('MCP server'), {
      target: { value: 'records-mcp' },
    });
    const opts = [...document.querySelectorAll('#pf-tool-opts option')].map((o) =>
      o.getAttribute('value'),
    );
    expect(opts).toEqual(['*', 'lookup_customer', 'refund']);
  });

  it('changing the server resets the tool to *', () => {
    renderDialog({ duplicating: policy() }); // tool seeded to "refund"
    expect((screen.getByLabelText('Tool') as HTMLInputElement).value).toBe('refund');
    fireEvent.change(screen.getByLabelText('MCP server'), {
      target: { value: 'other-mcp' },
    });
    expect((screen.getByLabelText('Tool') as HTMLInputElement).value).toBe('*');
  });

  it('duplicate seeds ALL fields from the source, enabled included (Lit parity)', () => {
    renderDialog({ duplicating: policy({ enabled: false, priority: 7 }) });
    expect(screen.getByText('Duplicate approval policy')).toBeTruthy();
    expect(screen.getByText('Create policy')).toBeTruthy(); // create-flow label
    expect((screen.getByLabelText('MCP server') as HTMLInputElement).value).toBe(
      'records-mcp',
    );
    expect(
      (screen.getByLabelText(/Priority/) as HTMLInputElement).value,
    ).toBe('7');
    // enabled copies the source — a disabled source duplicates disabled.
    const status = screen.getAllByRole('switch')[0];
    expect(status.getAttribute('aria-checked')).toBe('false');
    // Condition seeded into the builder (leaf row, edit-form bare value).
    expect(
      (screen.getByPlaceholderText('field (e.g. amount)') as HTMLInputElement).value,
    ).toBe('amount');
  });

  it('edit mode: title + Save changes label; leaf expr round-trips into rows', () => {
    renderDialog({ editing: policy() });
    expect(screen.getByText('Edit approval policy')).toBeTruthy();
    expect(screen.getByText('Save changes')).toBeTruthy();
    expect(
      (screen.getByPlaceholderText('value (e.g. 5000)') as HTMLInputElement).value,
    ).toBe('5000');
  });

  it('a nested stored expression opens READ-ONLY: notice + raw JSON, no rows', () => {
    const nested = {
      and: [{ or: [{ field: 'a', op: '==', value: 1 }] }],
    } as unknown as ApprovalPolicy['condition_expr'];
    renderDialog({ editing: policy({ condition_expr: nested }) });
    expect(screen.getByTestId('condition-readonly')).toBeTruthy();
    expect(
      screen.getByText(/advanced condition that the form can't edit/),
    ).toBeTruthy();
    expect(document.querySelector('.cond-readonly')?.textContent).toContain('"or"');
    expect(document.querySelector('.cond-row')).toBeNull();
    // Lit parity: the mode seg is replaced too — switching to "Every
    // time" on a pass-through expression would be a lying no-op.
    expect(screen.queryByText('Every time')).toBeNull();
    // Preview still renders — from the stored expression, collapsed note.
    expect(screen.getByTestId('policy-preview').textContent).toContain(
      '(advanced condition)',
    );
    // Other fields stay editable.
    expect(
      (screen.getByLabelText('MCP server') as HTMLInputElement).disabled,
    ).toBe(false);
  });

  it('remove is disabled on the last row; add appends one', () => {
    renderDialog({});
    fireEvent.click(screen.getByText('Only when…'));
    expect(
      (screen.getByTitle('Remove condition') as HTMLButtonElement).disabled,
    ).toBe(true);
    fireEvent.click(screen.getByText('+ Add condition'));
    expect(document.querySelectorAll('.cond-row').length).toBe(2);
    const removes = screen.getAllByTitle('Remove condition') as HTMLButtonElement[];
    expect(removes[0].disabled).toBe(false);
    fireEvent.click(removes[0]);
    expect(document.querySelectorAll('.cond-row').length).toBe(1);
  });
});
