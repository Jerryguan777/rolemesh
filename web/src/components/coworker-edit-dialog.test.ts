// @vitest-environment happy-dom
// <rm-coworker-edit-dialog> contract:
//   1. Form seeds from `coworker` on open.
//   2. Buildpatch sends ONLY changed fields (absence = leave alone).
//   3. No-op submit (nothing changed) closes without an API call.
//   4. Save emits `coworker-saved` + closes.
//   5. Failure surfaces inline; dialog stays open.

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import './coworker-edit-dialog.js';
import type { CoworkerEditDialog } from './coworker-edit-dialog.js';
import type { Coworker, Model } from '../api/client.js';

interface PatchCall {
  url: string;
  body: Record<string, unknown>;
}

interface Stub {
  restore: () => void;
  patches: PatchCall[];
  shouldFail: { status: number; message: string } | null;
}

function installFetch(): Stub {
  const original = globalThis.fetch;
  const stub: Stub = { restore: () => {}, patches: [], shouldFail: null };
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    if (
      /\/api\/v1\/coworkers\/[\w-]+$/.test(url) &&
      init?.method === 'PATCH'
    ) {
      const body = JSON.parse(init.body as string);
      stub.patches.push({ url, body });
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
      return Promise.resolve(
        new Response(
          JSON.stringify({
            id: 'cw-1',
            tenant_id: 't1',
            name: body.name ?? 'unchanged',
            folder: 'ops',
            agent_backend: 'claude',
            model_id: body.model_id ?? null,
            system_prompt: body.system_prompt ?? null,
            status: body.status ?? 'active',
            agent_role: 'agent',
            max_concurrent: body.max_concurrent ?? 1,
            created_at: '2026-05-23T00:00:00Z',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      );
    }
    return Promise.resolve(new Response('not found', { status: 404 }));
  }) as unknown as typeof fetch;
  stub.restore = () => {
    globalThis.fetch = original;
  };
  return stub;
}

const EXISTING: Coworker = {
  id: 'cw-1',
  tenant_id: 't1',
  name: 'Ops coworker',
  folder: 'ops',
  agent_backend: 'claude',
  model_id: 'mdl-1',
  system_prompt: null,
  status: 'active',
  agent_role: 'agent',
  max_concurrent: 1,
  created_at: '2026-05-23T00:00:00Z',
} as unknown as Coworker;

const MODELS: Model[] = [
  {
    id: 'mdl-1',
    provider: 'anthropic',
    model_id: 'claude-sonnet-4-6',
    model_family: 'claude',
    display_name: 'Claude Sonnet 4.6',
    is_active: true,
  } as Model,
  {
    id: 'mdl-2',
    provider: 'bedrock',
    model_id: 'claude-haiku-4-5',
    model_family: 'claude',
    display_name: 'Claude Haiku 4.5 (Bedrock)',
    is_active: true,
  } as Model,
];

async function settle(el: CoworkerEditDialog): Promise<void> {
  for (let i = 0; i < 20; i += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

function mount(): CoworkerEditDialog {
  const el = document.createElement(
    'rm-coworker-edit-dialog',
  ) as CoworkerEditDialog;
  document.body.appendChild(el);
  return el;
}

describe('<rm-coworker-edit-dialog>', () => {
  let stub: Stub;

  beforeEach(() => {
    stub = installFetch();
  });

  afterEach(() => {
    document
      .querySelectorAll('rm-coworker-edit-dialog')
      .forEach((el) => el.remove());
    stub.restore();
  });

  it('seeds form fields from the `coworker` prop on open', async () => {
    const el = mount();
    el.coworker = EXISTING;
    el.models = MODELS;
    el.open = true;
    await settle(el);
    const name = el.querySelector<HTMLInputElement>(
      '[data-testid="coworker-edit-name"]',
    )!;
    expect(name.value).toBe('Ops coworker');
    const status = el.querySelector<HTMLSelectElement>(
      '[data-testid="coworker-edit-status"]',
    )!;
    expect(status.value).toBe('active');
  });

  it('no-op save (form unchanged) closes WITHOUT a PATCH call', async () => {
    const el = mount();
    el.coworker = EXISTING;
    el.models = MODELS;
    el.open = true;
    await settle(el);
    const closes: Event[] = [];
    el.addEventListener('close', (e) => closes.push(e));
    el.querySelector<HTMLButtonElement>(
      '[data-testid="coworker-edit-save"]',
    )!.click();
    await settle(el);
    expect(stub.patches).toHaveLength(0);
    expect(closes.length).toBeGreaterThanOrEqual(1);
  });

  it('PATCH body contains ONLY changed fields (absent = leave alone)', async () => {
    const el = mount();
    el.coworker = EXISTING;
    el.models = MODELS;
    el.open = true;
    await settle(el);
    // Change only the status — leave name / prompt / model / max alone.
    const status = el.querySelector<HTMLSelectElement>(
      '[data-testid="coworker-edit-status"]',
    )!;
    status.value = 'paused';
    status.dispatchEvent(new Event('change', { bubbles: true }));
    await settle(el);
    el.querySelector<HTMLButtonElement>(
      '[data-testid="coworker-edit-save"]',
    )!.click();
    await settle(el);
    expect(stub.patches).toHaveLength(1);
    expect(Object.keys(stub.patches[0].body).sort()).toEqual(['status']);
    expect(stub.patches[0].body.status).toBe('paused');
  });

  it('emits coworker-saved + closes on successful PATCH', async () => {
    const el = mount();
    el.coworker = EXISTING;
    el.models = MODELS;
    el.open = true;
    await settle(el);
    const name = el.querySelector<HTMLInputElement>(
      '[data-testid="coworker-edit-name"]',
    )!;
    name.value = 'Ops coworker (renamed)';
    name.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);
    const events: CustomEvent[] = [];
    el.addEventListener('coworker-saved', (e) =>
      events.push(e as CustomEvent),
    );
    el.querySelector<HTMLButtonElement>(
      '[data-testid="coworker-edit-save"]',
    )!.click();
    await settle(el);
    expect(events.length).toBe(1);
    expect(el.open).toBe(false);
  });

  it('surfaces server error inline; dialog stays open on failure', async () => {
    stub.shouldFail = { status: 422, message: 'invalid model' };
    const el = mount();
    el.coworker = EXISTING;
    el.models = MODELS;
    el.open = true;
    await settle(el);
    const status = el.querySelector<HTMLSelectElement>(
      '[data-testid="coworker-edit-status"]',
    )!;
    status.value = 'paused';
    status.dispatchEvent(new Event('change', { bubbles: true }));
    await settle(el);
    el.querySelector<HTMLButtonElement>(
      '[data-testid="coworker-edit-save"]',
    )!.click();
    await settle(el);
    expect(el.open).toBe(true);
    const alert = el.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain('invalid model');
  });
});
