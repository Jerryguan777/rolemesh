// @vitest-environment happy-dom
// Behavioural tests for <rm-credential-dialog>.
//
// Contract under test (drafted from v2-B spec):
//   - Provider-locked mode: only the relevant fields render.
//   - Provider-pick mode (provider = null): user can change provider
//     and field set updates.
//   - Bedrock renders 2 fields: the Bedrock long-term API key
//     (api_key slot) and region (default us-east-1). The proxy
//     authenticates with the key as a Bearer token, so no AWS
//     secret/session-token fields are collected.
//   - Save → PUT /credentials/{provider} with the right body
//     shape `{api_key, extras: {...}}` (or `extras: null` when no
//     extras are needed).
//   - Save success fires `credential-saved` AND clears the form
//     (no plaintext lingers after a successful write).
//   - Save failure shows the error inline; the form stays open and
//     the api_key value is preserved so the user can retry.

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import './credential-dialog.js';
import { schemaFor, type CredentialDialog } from './credential-dialog.js';

interface PutCall {
  url: string;
  body: { api_key: string; extras: Record<string, unknown> | null };
}

interface Stub {
  restore: () => void;
  calls: PutCall[];
  shouldFail: { status: number; message: string } | null;
}

function installFetch(): Stub {
  const original = globalThis.fetch;
  const stub: Stub = { restore: () => {}, calls: [], shouldFail: null };
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const m = /\/api\/v1\/credentials\/(\w+)$/.exec(url);
    if (m && init?.method === 'PUT') {
      const body = JSON.parse(init.body as string);
      stub.calls.push({ url, body });
      if (stub.shouldFail) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ code: 'X', message: stub.shouldFail.message }),
            { status: stub.shouldFail.status, headers: { 'Content-Type': 'application/json' } },
          ),
        );
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({
            provider: m[1],
            created_at: '2026-05-23T00:00:00Z',
            updated_at: '2026-05-23T00:00:00Z',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      );
    }
    return Promise.resolve(new Response('not handled', { status: 599 }));
  }) as typeof fetch;
  stub.restore = () => {
    globalThis.fetch = original;
  };
  return stub;
}

async function settle(el: CredentialDialog) {
  await el.updateComplete;
  for (let i = 0; i < 6; i++) {
    await new Promise((r) => setTimeout(r, 0));
    await el.updateComplete;
  }
}

function mount(): CredentialDialog {
  const el = document.createElement('rm-credential-dialog') as CredentialDialog;
  document.body.appendChild(el);
  return el;
}

describe('schemaFor', () => {
  it('throws on unknown provider', () => {
    expect(() => schemaFor('whatever' as never)).toThrow();
  });
  it('exposes one api_key field for every provider', () => {
    for (const p of ['anthropic', 'openai', 'google', 'bedrock'] as const) {
      const s = schemaFor(p);
      expect(s.apiKey.label.length).toBeGreaterThan(0);
    }
  });
});

describe('<rm-credential-dialog>', () => {
  let stub: Stub | null = null;

  beforeEach(() => {
    stub = installFetch();
  });
  afterEach(() => {
    stub?.restore();
    document.body.innerHTML = '';
  });

  it('anthropic locked mode renders one API key input only', async () => {
    const el = mount();
    el.provider = 'anthropic';
    el.open = true;
    await settle(el);

    const inputs = el.querySelectorAll('input');
    // One password (api key). No required text extras for anthropic.
    expect(inputs.length).toBe(1);
    expect(inputs[0]!.getAttribute('type')).toBe('password');
  });

  it('bedrock locked mode renders 2 fields with us-east-1 default region', async () => {
    const el = mount();
    el.provider = 'bedrock';
    el.open = true;
    await settle(el);
    const inputs = el.querySelectorAll('input');
    expect(inputs.length).toBe(2);
    // Region field: find label whose text matches "Region".
    const labels = [...el.querySelectorAll('label')].map((l) => l.textContent?.trim());
    expect(labels).toContain('Region');
    // Default region pre-filled.
    const regionInput = [...inputs].find(
      (i) => i.getAttribute('placeholder') === 'us-east-1',
    )!;
    expect(regionInput.value).toBe('us-east-1');
  });

  it('provider picker renders when provider=null and updates field set on change', async () => {
    const el = mount();
    el.provider = null;
    el.open = true;
    await settle(el);

    const select = el.querySelector('select')!;
    expect(el.querySelectorAll('input').length).toBe(1); // anthropic default
    select.value = 'bedrock';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    await settle(el);
    expect(el.querySelectorAll('input').length).toBe(2); // bedrock fields
  });

  it('PUT body matches the per-provider shape for openai with api_base override', async () => {
    const el = mount();
    el.provider = 'openai';
    el.open = true;
    await settle(el);
    const inputs = el.querySelectorAll<HTMLInputElement>('input');
    // First is api_key (password); second is api_base (optional).
    inputs[0]!.value = 'sk-fake';
    inputs[0]!.dispatchEvent(new Event('input', { bubbles: true }));
    inputs[1]!.value = 'https://gateway.example.com/v1';
    inputs[1]!.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);

    const saveBtn = [...el.querySelectorAll<HTMLButtonElement>('button')].find(
      (b) => b.textContent?.includes('Save'),
    )!;
    saveBtn.click();
    await settle(el);

    expect(stub!.calls).toHaveLength(1);
    expect(stub!.calls[0]).toMatchObject({
      url: expect.stringContaining('/credentials/openai'),
      body: {
        api_key: 'sk-fake',
        extras: { api_base: 'https://gateway.example.com/v1' },
      },
    });
  });

  it('extras=null when no extras are filled in (anthropic happy path)', async () => {
    const el = mount();
    el.provider = 'anthropic';
    el.open = true;
    await settle(el);
    const key = el.querySelector<HTMLInputElement>('input')!;
    key.value = 'sk-fake';
    key.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);
    const saveBtn = [...el.querySelectorAll<HTMLButtonElement>('button')].find(
      (b) => b.textContent?.includes('Save'),
    )!;
    saveBtn.click();
    await settle(el);
    expect(stub!.calls[0]!.body).toEqual({ api_key: 'sk-fake', extras: null });
  });

  it('blocks save when a required extra is empty (bedrock without region)', async () => {
    const el = mount();
    el.provider = 'bedrock';
    el.open = true;
    await settle(el);
    const inputs = el.querySelectorAll<HTMLInputElement>('input');
    // Fill the Bedrock API key, then clear the pre-filled region.
    inputs[0]!.value = 'ABSKFAKE';
    inputs[0]!.dispatchEvent(new Event('input', { bubbles: true }));
    inputs[1]!.value = '';
    inputs[1]!.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);
    const saveBtn = [...el.querySelectorAll<HTMLButtonElement>('button')].find(
      (b) => b.textContent?.includes('Save'),
    )!;
    saveBtn.click();
    await settle(el);
    expect(stub!.calls).toHaveLength(0);
    expect(el.textContent).toContain('Region is required');
  });

  it('emits credential-saved and clears the form on success', async () => {
    const el = mount();
    el.provider = 'anthropic';
    el.open = true;
    await settle(el);
    const saved: string[] = [];
    el.addEventListener('credential-saved', (e) => {
      saved.push((e as CustomEvent<{ provider: string }>).detail.provider);
    });
    const key = el.querySelector<HTMLInputElement>('input')!;
    key.value = 'sk-fake';
    key.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);
    const saveBtn = [...el.querySelectorAll<HTMLButtonElement>('button')].find(
      (b) => b.textContent?.includes('Save'),
    )!;
    saveBtn.click();
    await settle(el);
    expect(saved).toEqual(['anthropic']);
    // After save the dialog closes. The api_key field state should
    // be cleared so reopening shows blank.
    el.open = true;
    await settle(el);
    expect(el.querySelector<HTMLInputElement>('input')!.value).toBe('');
  });

  it('surfaces server error inline and keeps the form open', async () => {
    stub!.shouldFail = { status: 422, message: 'invalid key shape' };
    const el = mount();
    el.provider = 'anthropic';
    el.open = true;
    await settle(el);
    const key = el.querySelector<HTMLInputElement>('input')!;
    key.value = 'bad';
    key.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);
    const saveBtn = [...el.querySelectorAll<HTMLButtonElement>('button')].find(
      (b) => b.textContent?.includes('Save'),
    )!;
    saveBtn.click();
    await settle(el);
    expect(el.open).toBe(true);
    expect(el.textContent).toContain('invalid key shape');
    // Plaintext stays in the input so the user can correct it.
    expect(el.querySelector<HTMLInputElement>('input')!.value).toBe('bad');
  });
});
