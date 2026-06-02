// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { MCPServer } from '../api/client.js';

// The dialog fetches the tenant's MCP servers on open to seed the server / tool
// combobox suggestions. Mock the client so the fetch is deterministic; the
// policy CRUD methods are stubs (these tests never submit).
const { listServersSpy } = vi.hoisted(() => ({ listServersSpy: vi.fn() }));

vi.mock('../api/client.js', async () => {
  const actual =
    await vi.importActual<typeof import('../api/client.js')>('../api/client.js');
  return {
    ...actual,
    getApiClient: () => ({
      listMCPServers: listServersSpy,
      createApprovalPolicy: vi.fn(),
      updateApprovalPolicy: vi.fn(),
    }),
  };
});

import './approval-policy-dialog.js';
import type { ApprovalPolicyDialog } from './approval-policy-dialog.js';
import type { Combobox } from './combobox.js';

function server(name: string, tools: Record<string, boolean>): MCPServer {
  return {
    id: `id-${name}`,
    tenant_id: 't1',
    name,
    type: 'http',
    url: `https://${name}.example`,
    auth_mode: 'service',
    tool_reversibility: tools,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  } as MCPServer;
}

/** Mount the dialog, open it, wait for the async server-load + re-render. */
async function mountOpen(): Promise<ApprovalPolicyDialog> {
  const el = document.createElement(
    'rm-approval-policy-dialog',
  ) as ApprovalPolicyDialog;
  document.body.appendChild(el);
  el.open = true;
  await el.updateComplete;
  await new Promise<void>((r) => setTimeout(r, 0)); // loadServers resolves
  await el.updateComplete;
  return el;
}

function combos(el: HTMLElement): Combobox[] {
  return Array.from(el.querySelectorAll('rm-combobox')) as Combobox[];
}

/** Drive a combobox as if the user typed `value` into it. */
function typeInto(combo: Combobox, value: string): void {
  const input = combo.querySelector('input')!;
  input.value = value;
  input.dispatchEvent(new Event('input'));
}

describe('approval-policy-dialog — server/tool comboboxes', () => {
  afterEach(() => {
    document.body.innerHTML = '';
    listServersSpy.mockReset();
  });

  it('seeds the server combobox with the configured MCP servers', async () => {
    listServersSpy.mockResolvedValue([server('stripe', {}), server('mock-fs', {})]);
    const el = await mountOpen();
    const [serverCombo] = combos(el);
    expect([...serverCombo.options].sort()).toEqual(['mock-fs', 'stripe']);
  });

  it('tool suggestions follow the selected server (no cross-server leak)', async () => {
    listServersSpy.mockResolvedValue([
      server('stripe', { charge: false, refund: false }),
      server('mock-fs', { write_file: false, read_file: true }),
    ]);
    const el = await mountOpen();
    const [serverCombo, toolCombo] = combos(el);

    typeInto(serverCombo, 'stripe');
    await el.updateComplete;
    expect([...toolCombo.options].sort()).toEqual(['*', 'charge', 'refund']);
    expect(toolCombo.options).not.toContain('write_file');

    typeInto(serverCombo, 'mock-fs');
    await el.updateComplete;
    expect([...toolCombo.options].sort()).toEqual(['*', 'read_file', 'write_file']);
  });

  it('an unknown server offers only * and never blocks free-text entry', async () => {
    // The whole point: gate a server/tool that has never connected.
    listServersSpy.mockResolvedValue([server('stripe', { charge: false })]);
    const el = await mountOpen();
    const [serverCombo, toolCombo] = combos(el);

    typeInto(serverCombo, 'totally-new-server');
    await el.updateComplete;
    expect(toolCombo.options).toEqual(['*']);

    typeInto(toolCombo, 'some_uncatalogued_tool');
    await el.updateComplete;
    expect(toolCombo.value).toBe('some_uncatalogued_tool');
  });

  it('degrades gracefully when the server fetch fails', async () => {
    listServersSpy.mockRejectedValue(new Error('network'));
    const el = await mountOpen();
    const [serverCombo, toolCombo] = combos(el);
    expect(serverCombo.options).toEqual([]);
    expect(toolCombo.options).toEqual(['*']); // wildcard always available
  });
});
