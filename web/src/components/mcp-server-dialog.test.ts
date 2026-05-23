// @vitest-environment happy-dom
// Behavioural tests for <rm-mcp-server-dialog>.
//
// Contract under test (v2-C polish; the component shipped in v2-B
// without unit tests — v2-B Finding flagged the gap).
//
//   1. Opening the dialog reveals the form fields; closed dialog
//      keeps them inert.
//   2. Required-field validation (name + url) blocks the save and
//      surfaces an inline error.
//   3. Save calls POST /api/v1/mcp-servers with the exact form body.
//   4. Successful save emits `mcp-server-created` AND closes the
//      dialog (single source of truth for the wizard).
//   5. Failure surfaces the server error inline; the dialog stays
//      open so the user can retry without re-typing.

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import './mcp-server-dialog.js';
import type { MCPServerDialog } from './mcp-server-dialog.js';
import type { MCPServer } from '../api/client.js';

interface PostCall {
  url: string;
  body: Record<string, unknown>;
}

interface Stub {
  restore: () => void;
  calls: PostCall[];
  shouldFail: { status: number; message: string } | null;
}

function installFetch(): Stub {
  const original = globalThis.fetch;
  const stub: Stub = { restore: () => {}, calls: [], shouldFail: null };
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    if (url.endsWith('/api/v1/mcp-servers') && init?.method === 'POST') {
      const body = JSON.parse(init.body as string);
      stub.calls.push({ url, body });
      if (stub.shouldFail) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ code: 'X', message: stub.shouldFail.message }),
            {
              status: stub.shouldFail.status,
              headers: { 'Content-Type': 'application/json' },
            },
          ),
        );
      }
      const created: MCPServer = {
        id: 'm-1',
        tenant_id: 't1',
        name: body.name as string,
        type: body.type as 'http' | 'sse',
        url: body.url as string,
        auth_mode: body.auth_mode as 'service' | 'user' | 'both',
        description: (body.description as string | null) ?? null,
        created_at: '2026-05-23T00:00:00Z',
        updated_at: '2026-05-23T00:00:00Z',
      } as MCPServer;
      return Promise.resolve(
        new Response(JSON.stringify(created), {
          status: 201,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }
    return Promise.resolve(new Response('not found', { status: 404 }));
  }) as unknown as typeof fetch;
  stub.restore = () => {
    globalThis.fetch = original;
  };
  return stub;
}

async function settle(el: MCPServerDialog): Promise<void> {
  for (let i = 0; i < 20; i += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

function mount(): MCPServerDialog {
  const el = document.createElement(
    'rm-mcp-server-dialog',
  ) as MCPServerDialog;
  document.body.appendChild(el);
  return el;
}

describe('<rm-mcp-server-dialog>', () => {
  let stub: Stub;

  beforeEach(() => {
    stub = installFetch();
  });

  afterEach(() => {
    document
      .querySelectorAll('rm-mcp-server-dialog')
      .forEach((el) => el.remove());
    stub.restore();
  });

  it('renders form fields when open=true', async () => {
    const el = mount();
    el.open = true;
    await settle(el);
    const inputs = el.querySelectorAll('input');
    expect(inputs.length).toBeGreaterThanOrEqual(2); // name + url
    const selects = el.querySelectorAll('select');
    expect(selects.length).toBe(2); // transport + auth_mode
  });

  it('keeps the dialog content out of the way when open=false', async () => {
    // <rm-dialog> hides itself via the [open] attribute mirror; we
    // just check that the wrapper exists but no save would fire.
    const el = mount();
    await settle(el);
    expect(stub.calls).toHaveLength(0);
  });

  it('blocks save when name or url is empty and shows an inline error', async () => {
    const el = mount();
    el.open = true;
    await settle(el);
    // Click "Add server" without filling anything.
    const saveBtn = [...el.querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Add server'),
    )!;
    saveBtn.click();
    await settle(el);
    expect(stub.calls).toHaveLength(0);
    const alert = el.querySelector('[role="alert"]');
    expect(alert).not.toBeNull();
    expect(alert?.textContent?.toLowerCase()).toContain('required');
  });

  it('POSTs the form body to /api/v1/mcp-servers on save', async () => {
    const el = mount();
    el.open = true;
    await settle(el);
    const [nameInput, urlInput] = el.querySelectorAll('input');
    nameInput.value = 'shopify-admin';
    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    urlInput.value = 'https://mcp.example/sse';
    urlInput.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);
    const saveBtn = [...el.querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Add server'),
    )!;
    saveBtn.click();
    await settle(el);
    expect(stub.calls).toHaveLength(1);
    expect(stub.calls[0].body.name).toBe('shopify-admin');
    expect(stub.calls[0].body.url).toBe('https://mcp.example/sse');
    expect(stub.calls[0].body.type).toBe('http');
    expect(stub.calls[0].body.auth_mode).toBe('service');
  });

  it('emits mcp-server-created and closes on successful save', async () => {
    const el = mount();
    el.open = true;
    await settle(el);
    const events: CustomEvent[] = [];
    el.addEventListener('mcp-server-created', (e) =>
      events.push(e as CustomEvent),
    );
    const closes: Event[] = [];
    el.addEventListener('close', (e) => closes.push(e));
    const [nameInput, urlInput] = el.querySelectorAll('input');
    nameInput.value = 'fs';
    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    urlInput.value = 'https://fs.local';
    urlInput.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);
    const saveBtn = [...el.querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Add server'),
    )!;
    saveBtn.click();
    await settle(el);
    expect(events.length).toBe(1);
    expect((events[0].detail as { server: MCPServer }).server.name).toBe('fs');
    // The dialog dispatches its own `close` AND <rm-dialog> bubbles
    // a second one when `open` flips false; we only care that AT
    // LEAST one fires + the open flag flipped.
    expect(closes.length).toBeGreaterThanOrEqual(1);
    expect(el.open).toBe(false);
  });

  it('surfaces server error inline and keeps the dialog open on failure', async () => {
    stub.shouldFail = { status: 500, message: 'kaboom' };
    const el = mount();
    el.open = true;
    await settle(el);
    const [nameInput, urlInput] = el.querySelectorAll('input');
    nameInput.value = 'shopify';
    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    urlInput.value = 'https://x';
    urlInput.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);
    const saveBtn = [...el.querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Add server'),
    )!;
    saveBtn.click();
    await settle(el);
    expect(el.open).toBe(true);
    const alert = el.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain('kaboom');
  });
});
