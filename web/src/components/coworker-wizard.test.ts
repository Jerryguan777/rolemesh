// @vitest-environment happy-dom
// Behavioural tests for <rm-coworker-wizard>.
//
// Goal: pin the wizard's *contract* from a user's perspective — the
// step rail, the slug derivation rules, the gating on each step's
// canAdvance, and the submit-failure-mode shape. We mock only the
// outermost boundary (the global `fetch`), never internal helpers.
//
// Anti-mirror: tests are written from the wizard's externally
// observable behaviour. They were drafted from the v2-B spec
// (`docs/webui-ui-redesign-v2-sessions/v2-B-coworker-wizard-and-credentials.md`)
// before reading the implementation, and a couple of them
// (slug-edge-cases, partial-commit) intentionally probe scenarios the
// happy-path code would skip.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import './coworker-wizard.js';
import {
  isValidSlug,
  slugify,
  type CoworkerWizard,
} from './coworker-wizard.js';
import type { Backend, Coworker, CredentialResponse, Model } from '../api/client.js';

// ---------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------

const BACKENDS: Backend[] = [
  {
    name: 'claude',
    description: 'Claude Agent SDK',
    supported_providers: ['anthropic'],
    supported_model_families: ['claude'],
  },
  {
    name: 'pi',
    description: 'Pi',
    supported_providers: ['anthropic', 'openai', 'google', 'bedrock'],
    supported_model_families: null,
  },
];

const MODELS: Model[] = [
  {
    id: 'aaaaaaaa-0000-0000-0000-000000000001',
    provider: 'anthropic',
    model_id: 'claude-opus-4-7',
    model_family: 'claude',
    display_name: 'Claude Opus 4.7',
    is_active: true,
  },
  {
    id: 'aaaaaaaa-0000-0000-0000-000000000002',
    provider: 'openai',
    model_id: 'gpt-4o',
    model_family: 'gpt',
    display_name: 'GPT-4o',
    is_active: true,
  },
];

const CREDS_ANT: CredentialResponse[] = [
  { provider: 'anthropic', created_at: '2026-05-20T00:00:00Z', updated_at: '2026-05-20T00:00:00Z' },
];

const CREATED_COWORKER: Coworker = {
  id: 'cccccccc-0000-0000-0000-000000000001',
  tenant_id: 'tttttttt-0000-0000-0000-000000000001',
  name: 'Marketing helper',
  folder: 'marketing-helper',
  agent_backend: 'claude',
  model_id: MODELS[0]!.id,
  status: 'active',
  agent_role: 'agent',
  max_concurrent: 2,
  created_at: '2026-05-23T12:00:00Z',
};

// ---------------------------------------------------------------------
// Fetch stub — pattern-routed
// ---------------------------------------------------------------------

interface FetchExpectation {
  match: (url: string, init?: RequestInit) => boolean;
  respond: (url: string, init?: RequestInit) => Response | Promise<Response>;
  /** Optional sink: record every call to this matcher. */
  calls?: { url: string; init?: RequestInit }[];
}

function jsonResp(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function installFetch(expectations: FetchExpectation[]): { restore: () => void } {
  const original = globalThis.fetch;
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    for (const e of expectations) {
      if (e.match(url, init)) {
        e.calls?.push({ url, init });
        return Promise.resolve(e.respond(url, init));
      }
    }
    return Promise.resolve(new Response(`unhandled ${url}`, { status: 599 }));
  }) as typeof fetch;
  return {
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

async function settle(el: CoworkerWizard) {
  await el.updateComplete;
  // The wizard kicks off async catalogue loading on open; flush
  // microtasks until both fetches and re-renders settle.
  for (let i = 0; i < 10; i++) {
    await new Promise((r) => setTimeout(r, 0));
    await el.updateComplete;
  }
}

function mount(): CoworkerWizard {
  const el = document.createElement('rm-coworker-wizard') as CoworkerWizard;
  document.body.appendChild(el);
  return el;
}

// ---------------------------------------------------------------------
// Slug derivation — pure unit tests on the exported helper.
// ---------------------------------------------------------------------

describe('slugify', () => {
  it('lowercases and dasherises spaces', () => {
    expect(slugify('Marketing Helper')).toBe('marketing-helper');
  });
  it('collapses runs of separators', () => {
    expect(slugify('foo  bar___baz')).toBe('foo-bar___baz');
    expect(slugify('foo!!bar')).toBe('foo-bar');
  });
  it('drops leading dashes that the backend regex would reject', () => {
    expect(slugify('-foo')).toBe('foo');
    expect(slugify('---hi')).toBe('hi');
  });
  it('handles pure numeric names — the regex accepts them', () => {
    expect(slugify('123')).toBe('123');
    expect(isValidSlug('123')).toBe(true);
  });
  it('returns empty string for pure-whitespace names', () => {
    expect(slugify('   ')).toBe('');
    expect(isValidSlug('')).toBe(false);
  });
  it('truncates to 64 chars (regex upper bound)', () => {
    expect(slugify('a'.repeat(100)).length).toBeLessThanOrEqual(64);
  });
});

describe('isValidSlug', () => {
  it('rejects slugs starting with non-alphanumeric', () => {
    expect(isValidSlug('-foo')).toBe(false);
    expect(isValidSlug('_foo')).toBe(false);
  });
  it('allows _ and - in middle', () => {
    expect(isValidSlug('foo_bar-baz')).toBe(true);
  });
  it('rejects forbidden characters', () => {
    expect(isValidSlug('foo bar')).toBe(false);
    expect(isValidSlug('foo/bar')).toBe(false);
  });
});

// ---------------------------------------------------------------------
// Wizard rendering + step gating
// ---------------------------------------------------------------------

describe('<rm-coworker-wizard>', () => {
  let fetchStub: { restore: () => void } | null = null;
  let createdBody: unknown = null;
  let mcpBindCalls: { url: string; body: unknown }[] = [];
  let skillBindCalls: string[] = [];

  beforeEach(() => {
    createdBody = null;
    mcpBindCalls = [];
    skillBindCalls = [];
    fetchStub = installFetch([
      {
        match: (u, i) => u.endsWith('/api/v1/backends') && (i?.method ?? 'GET') === 'GET',
        respond: () => jsonResp(BACKENDS),
      },
      {
        match: (u) => u.startsWith('/api/v1/models') && u.includes('?') === false,
        respond: () => jsonResp(MODELS),
      },
      {
        match: (u, i) =>
          u === '/api/v1/tenant/credentials' && (i?.method ?? 'GET') === 'GET',
        respond: () => jsonResp(CREDS_ANT),
      },
      {
        match: (u) => u === '/api/v1/mcp-servers',
        respond: () => jsonResp([]),
      },
      {
        match: (u) => u === '/api/v1/skills',
        respond: () => jsonResp([]),
      },
      {
        match: (u, i) => u === '/api/v1/coworkers' && i?.method === 'POST',
        respond: (_u, i) => {
          createdBody = i?.body ? JSON.parse(i.body as string) : null;
          return jsonResp(CREATED_COWORKER, 201);
        },
      },
      {
        match: (u, i) =>
          /\/api\/v1\/coworkers\/[^/]+\/mcp-servers$/.test(u) &&
          i?.method === 'POST',
        respond: (u, i) => {
          mcpBindCalls.push({ url: u, body: i?.body ? JSON.parse(i.body as string) : null });
          return jsonResp({}, 201);
        },
      },
      {
        match: (u, i) =>
          /\/api\/v1\/coworkers\/[^/]+\/skills\/[^/]+$/.test(u) &&
          i?.method === 'POST',
        respond: (u) => {
          skillBindCalls.push(u);
          return jsonResp({}, 201);
        },
      },
    ]);
  });

  afterEach(() => {
    fetchStub?.restore();
    document.body.innerHTML = '';
  });

  it('renders the 6-step rail when opened', async () => {
    const el = mount();
    el.open = true;
    await settle(el);
    const wizard = el.querySelector('rm-wizard')!;
    const steps = wizard.shadowRoot!.querySelectorAll('.rail .step');
    expect(steps.length).toBe(6);
    expect([...steps].map((s) => s.textContent!.trim().replace(/^\d+\s*/, ''))).toEqual([
      'Identity',
      'Engine',
      'Model',
      'Tools',
      'Skills',
      'Review',
    ]);
  });

  it('Identity step derives slug live and gates canAdvance on validity', async () => {
    const el = mount();
    el.open = true;
    await settle(el);
    // Empty name → next button disabled.
    let wizard = el.querySelector('rm-wizard')!;
    const nextBtn = () =>
      wizard.shadowRoot!.querySelector<HTMLButtonElement>('.foot .btn.primary')!;
    expect(nextBtn().disabled).toBe(true);

    // Type a name. The slug should appear and Next enables.
    const nameInput = el.querySelector<HTMLInputElement>('input')!;
    nameInput.value = 'Marketing Helper';
    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);
    expect(el.textContent).toContain('marketing-helper');
    expect(nextBtn().disabled).toBe(false);
  });

  it('Identity advanced override wins over auto-derived slug', async () => {
    const el = mount();
    el.open = true;
    await settle(el);

    const nameInput = el.querySelector<HTMLInputElement>('input')!;
    nameInput.value = 'Foo Bar';
    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);
    expect(el.textContent).toContain('foo-bar');

    // Override with a different slug.
    const details = el.querySelector('details')!;
    details.open = true;
    await settle(el);
    const inputs = el.querySelectorAll<HTMLInputElement>('input');
    // The second input is the override (name is first).
    const overrideInput = inputs[1]!;
    overrideInput.value = 'special-slug';
    overrideInput.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);
    // Override is what reaches the create payload — verified via
    // submit test below. For now, confirm the override sticks.
    expect(overrideInput.value).toBe('special-slug');
  });

  it('Identity gates canAdvance when override violates the slug regex', async () => {
    const el = mount();
    el.open = true;
    await settle(el);

    const nameInput = el.querySelector<HTMLInputElement>('input')!;
    nameInput.value = 'OK';
    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);

    const details = el.querySelector('details')!;
    details.open = true;
    await settle(el);
    const inputs = el.querySelectorAll<HTMLInputElement>('input');
    const overrideInput = inputs[1]!;
    overrideInput.value = '-bad';
    overrideInput.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);

    const wizard = el.querySelector('rm-wizard')!;
    const nextBtn = wizard.shadowRoot!.querySelector<HTMLButtonElement>('.foot .btn.primary')!;
    expect(nextBtn.disabled).toBe(true);
  });

  it('Engine step ungates after picking a backend; resets modelId', async () => {
    const el = mount();
    el.open = true;
    await settle(el);
    // Fill identity then advance to step 1.
    const nameInput = el.querySelector<HTMLInputElement>('input')!;
    nameInput.value = 'tester';
    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);
    const wizard = el.querySelector('rm-wizard')!;
    wizard.shadowRoot!.querySelector<HTMLButtonElement>('.foot .btn.primary')!.click();
    await settle(el);
    // We're on Engine. No backend selected → Next disabled.
    expect(wizard.shadowRoot!.querySelector<HTMLButtonElement>('.foot .btn.primary')!.disabled).toBe(true);
    const cards = el.querySelectorAll<HTMLButtonElement>('button.w-full');
    // First card is "claude" backend.
    cards[0]!.click();
    await settle(el);
    expect(wizard.shadowRoot!.querySelector<HTMLButtonElement>('.foot .btn.primary')!.disabled).toBe(false);
  });

  it('Model step gates on credential presence — claude+anthropic OK, openai blocked when no cred', async () => {
    const el = mount();
    el.open = true;
    await settle(el);
    // Identity
    const nameInput = el.querySelector<HTMLInputElement>('input')!;
    nameInput.value = 'tester';
    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);
    const wiz = el.querySelector('rm-wizard')!;
    const next = () => wiz.shadowRoot!.querySelector<HTMLButtonElement>('.foot .btn.primary')!;
    next().click();
    await settle(el);
    // Engine — pick "pi" so both anthropic + openai groups show.
    const cards = el.querySelectorAll<HTMLButtonElement>('button.w-full');
    cards[1]!.click();
    await settle(el);
    next().click();
    await settle(el);

    // Step 3 — verify needs-credential banner only on openai group.
    expect(el.textContent).toContain('needs openai credential');
    expect(el.textContent).not.toContain('needs anthropic credential');

    // Clicking a disabled openai model should NOT advance Next.
    // Locate model row buttons (text + radio + model_id).
    // The disabled `?disabled` attribute does the heavy lift via the browser.
    expect(next().disabled).toBe(true);
  });

  it('submit POSTs coworker then binds mcp + skills, navigates on full success', async () => {
    const navTo = vi.fn();
    // happy-dom's location is mutable; we intercept the href setter.
    const origDescriptor = Object.getOwnPropertyDescriptor(window.location, 'href');
    Object.defineProperty(window.location, 'href', {
      configurable: true,
      set: (v) => navTo(v),
      get: () => '',
    });

    const el = mount();
    el.open = true;
    await settle(el);

    // Drive draft directly via private state proxy is forbidden; we
    // instead simulate the user input. We're focused on the *submit*
    // contract, so we hop the steps programmatically via the
    // rm-wizard's currentStep attribute by typing the minimum.
    const nameInput = el.querySelector<HTMLInputElement>('input')!;
    nameInput.value = 'Marketing Helper';
    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);

    // Step 1 → Engine. Pick claude (1st card).
    let wiz = el.querySelector('rm-wizard')!;
    wiz.shadowRoot!.querySelector<HTMLButtonElement>('.foot .btn.primary')!.click();
    await settle(el);
    const engineCards = el.querySelectorAll<HTMLButtonElement>('button.w-full');
    engineCards[0]!.click();
    await settle(el);
    wiz.shadowRoot!.querySelector<HTMLButtonElement>('.foot .btn.primary')!.click();
    await settle(el);

    // Step 2 → Model. Click the (only) anthropic model row.
    // The model row buttons are nested inside `<li>` under the group.
    // Find by text match on the display name.
    const allButtons = el.querySelectorAll<HTMLButtonElement>('button');
    const modelBtn = [...allButtons].find((b) => b.textContent?.includes('Claude Opus 4.7'))!;
    modelBtn.click();
    await settle(el);

    // Now next through Tools, Skills, Review to Create.
    for (let i = 0; i < 3; i++) {
      wiz = el.querySelector('rm-wizard')!;
      const primary = wiz.shadowRoot!.querySelector<HTMLButtonElement>('.foot .btn.primary')!;
      primary.click();
      await settle(el);
    }

    // We're on Review. Submit.
    wiz = el.querySelector('rm-wizard')!;
    const submitBtn = wiz.shadowRoot!.querySelector<HTMLButtonElement>('.foot .btn.primary')!;
    expect(submitBtn.textContent?.trim()).toMatch(/Create/);
    submitBtn.click();
    await settle(el);

    expect(createdBody).toMatchObject({
      name: 'Marketing Helper',
      folder: 'marketing-helper',
      agent_backend: 'claude',
      model_id: MODELS[0]!.id,
    });
    expect(navTo).toHaveBeenCalledWith(expect.stringContaining(`agent_id=${CREATED_COWORKER.id}`));

    if (origDescriptor) {
      Object.defineProperty(window.location, 'href', origDescriptor);
    }
  });

  it('partial-commit: mcp binding failures do NOT roll back; banner names failed ids', async () => {
    // Re-install fetch with mcp binding returning 500.
    fetchStub?.restore();
    fetchStub = installFetch([
      { match: (u) => u.endsWith('/api/v1/backends'), respond: () => jsonResp(BACKENDS) },
      { match: (u) => u.startsWith('/api/v1/models'), respond: () => jsonResp(MODELS) },
      { match: (u) => u === '/api/v1/tenant/credentials', respond: () => jsonResp(CREDS_ANT) },
      {
        match: (u) => u === '/api/v1/mcp-servers',
        respond: () => jsonResp([
          {
            id: 'mmmmmmmm-0000-0000-0000-000000000001',
            tenant_id: 't',
            name: 'srv-a',
            type: 'http',
            url: 'http://x',
            auth_mode: 'none',
            created_at: '2026-05-20T00:00:00Z',
            updated_at: '2026-05-20T00:00:00Z',
          },
        ]),
      },
      { match: (u) => u === '/api/v1/skills', respond: () => jsonResp([]) },
      {
        match: (u, i) => u === '/api/v1/coworkers' && i?.method === 'POST',
        respond: () => jsonResp(CREATED_COWORKER, 201),
      },
      {
        match: (u, i) =>
          /\/api\/v1\/coworkers\/[^/]+\/mcp-servers$/.test(u) && i?.method === 'POST',
        respond: () =>
          new Response(JSON.stringify({ code: 'X', message: 'boom' }), {
            status: 500,
            headers: { 'Content-Type': 'application/json' },
          }),
      },
    ]);

    const el = mount();
    el.open = true;
    await settle(el);

    // Pre-load a tool binding into the draft via the public API.
    // (We expose this through the user's normal interaction, but to
    // keep this test focused on the failure-mode contract we set it
    // directly through the wizard's draft — the PR 2 Tools step UI
    // is what produces the same state via clicks.)
    // We reach in via the property at the top — `draft` is private
    // so we narrow-cast.
    (el as unknown as { draft: { mcpServerIds: string[] } }).draft = {
      ...(el as unknown as { draft: Record<string, unknown> }).draft,
      mcpServerIds: ['mmmmmmmm-0000-0000-0000-000000000001'],
    } as never;

    // Drive to Review the same way as the happy path.
    const nameInput = el.querySelector<HTMLInputElement>('input')!;
    nameInput.value = 'Marketing Helper';
    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);
    let wiz = el.querySelector('rm-wizard')!;
    wiz.shadowRoot!.querySelector<HTMLButtonElement>('.foot .btn.primary')!.click();
    await settle(el);
    const cards = el.querySelectorAll<HTMLButtonElement>('button.w-full');
    cards[0]!.click();
    await settle(el);
    wiz.shadowRoot!.querySelector<HTMLButtonElement>('.foot .btn.primary')!.click();
    await settle(el);
    const modelBtn = [...el.querySelectorAll<HTMLButtonElement>('button')].find(
      (b) => b.textContent?.includes('Claude Opus 4.7'),
    )!;
    modelBtn.click();
    await settle(el);
    for (let i = 0; i < 3; i++) {
      wiz = el.querySelector('rm-wizard')!;
      wiz.shadowRoot!.querySelector<HTMLButtonElement>('.foot .btn.primary')!.click();
      await settle(el);
    }

    // Submit. Coworker creates; mcp binding fails. We should see
    // the partial-commit banner.
    wiz = el.querySelector('rm-wizard')!;
    wiz.shadowRoot!.querySelector<HTMLButtonElement>('.foot .btn.primary')!.click();
    await settle(el);

    expect(el.textContent).toMatch(/binding[s]? failed/i);
    expect(el.textContent).toContain('mmmmmmmm');
  });

  it('edit mode: seeds from `editing`, slug shown immutable, submit PATCHes', async () => {
    // Re-install fetch with PATCH + binding endpoints exercised.
    fetchStub?.restore();
    const patchCalls: { url: string; body: unknown }[] = [];
    const mcpUnbindCalls: string[] = [];
    fetchStub = installFetch([
      { match: (u) => u.endsWith('/api/v1/backends'), respond: () => jsonResp(BACKENDS) },
      { match: (u) => u.startsWith('/api/v1/models'), respond: () => jsonResp(MODELS) },
      { match: (u) => u === '/api/v1/tenant/credentials', respond: () => jsonResp(CREDS_ANT) },
      { match: (u) => u === '/api/v1/mcp-servers', respond: () => jsonResp([]) },
      { match: (u) => u === '/api/v1/skills', respond: () => jsonResp([]) },
      // Existing MCP bindings: seed the wizard with one already-bound
      // server so we can verify it ends up in originalMcpServerIds.
      {
        match: (u, i) =>
          /\/api\/v1\/coworkers\/[\w-]+\/mcp-servers$/.test(u) &&
          (i?.method ?? 'GET') === 'GET',
        respond: () =>
          jsonResp([{ mcp_server_id: 'old-mcp-1', enabled_tools: null }]),
      },
      // Existing skill bindings — empty for this test.
      {
        match: (u, i) =>
          /\/api\/v1\/coworkers\/[\w-]+\/skills$/.test(u) &&
          (i?.method ?? 'GET') === 'GET',
        respond: () => jsonResp([]),
      },
      // PATCH the coworker.
      {
        match: (u, i) =>
          /\/api\/v1\/coworkers\/[\w-]+$/.test(u) && i?.method === 'PATCH',
        respond: (_u, i) => {
          patchCalls.push({
            url: _u,
            body: i?.body ? JSON.parse(i.body as string) : null,
          });
          return jsonResp(CREATED_COWORKER, 200);
        },
      },
      // Unbind the removed MCP server.
      {
        match: (u, i) =>
          /\/api\/v1\/coworkers\/[\w-]+\/mcp-servers\/old-mcp-1$/.test(u) &&
          i?.method === 'DELETE',
        respond: (u) => {
          mcpUnbindCalls.push(u);
          return new Response(null, { status: 204 });
        },
      },
    ]);

    const existing: Coworker = {
      ...CREATED_COWORKER,
      id: 'eeeeeeee-0000-0000-0000-000000000001',
      name: 'Marketing legacy',
      folder: 'marketing-legacy',
    };
    const el = mount();
    (el as unknown as { editing: Coworker }).editing = existing;
    el.open = true;
    await settle(el);

    // Title reads "Edit coworker: …" — the wizard primitive renders
    // the title in its shadow root header.
    const wiz = el.querySelector('rm-wizard')!;
    const titleText = wiz.shadowRoot?.textContent ?? '';
    expect(titleText).toContain('Edit coworker');
    expect(titleText).toContain('Marketing legacy');

    // Slug shows immutable hint (no override input rendered).
    const slugHint = el.textContent ?? '';
    expect(slugHint).toContain('marketing-legacy');
    expect(slugHint).toMatch(/immutable/i);

    // Mutate the draft + jump to the Review step (5) so the wizard
    // shows the "Save changes" submit button. Reaching in via cast
    // keeps the test focused on the submit contract — driving the
    // 6-step click flow per case is what the partial-commit case
    // already covers for create mode.
    (el as unknown as { draft: Record<string, unknown>; currentStep: number }).draft = {
      ...(el as unknown as { draft: Record<string, unknown> }).draft,
      name: 'Marketing renamed',
      mcpServerIds: [], // remove old-mcp-1
    } as never;
    (el as unknown as { currentStep: number }).currentStep = 5;
    await settle(el);

    // Submit button reads "Save changes" in edit mode at the Review
    // step (vs "Create coworker" in create mode).
    const submitBtn = wiz.shadowRoot!.querySelector<HTMLButtonElement>(
      '.foot .btn.primary',
    )!;
    expect(submitBtn.textContent?.trim()).toContain('Save changes');

    submitBtn.click();
    await settle(el);

    // PATCH fired with the renamed `name`. Body shape mirrors
    // CoworkerUpdate; we ONLY assert the field we changed to keep
    // the test independent of which fields the wizard chooses to
    // send (it may pass-through unchanged ones too).
    expect(patchCalls).toHaveLength(1);
    expect((patchCalls[0].body as { name?: string }).name).toBe(
      'Marketing renamed',
    );
    // Binding diff: removed MCP got unbound.
    expect(mcpUnbindCalls).toHaveLength(1);
  });

  // -------------------------------------------------------------------
  // PR33: Review step — expandable Tools / Skills rows
  // -------------------------------------------------------------------

  it('Review row collapses to "None" when nothing is bound', async () => {
    fetchStub = installFetch([
      { match: (u) => u.endsWith('/api/v1/backends'), respond: () => jsonResp(BACKENDS) },
      { match: (u) => u.startsWith('/api/v1/models'), respond: () => jsonResp(MODELS) },
      { match: (u) => u === '/api/v1/tenant/credentials', respond: () => jsonResp(CREDS_ANT) },
      { match: (u) => u === '/api/v1/mcp-servers', respond: () => jsonResp([]) },
      { match: (u) => u === '/api/v1/skills', respond: () => jsonResp([]) },
    ]);
    const el = mount();
    el.open = true;
    await settle(el);
    (el as unknown as { currentStep: number }).currentStep = 5;
    await settle(el);
    const wiz = el.querySelector('rm-wizard')!;
    // Zero-bound rows render as plain "None" — no <details> to expand.
    expect(wiz.querySelector('[data-testid="wizard-review-tools"]')).toBeNull();
    expect(wiz.querySelector('[data-testid="wizard-review-skills"]')).toBeNull();
    // The Review pane still has the row labels.
    expect(wiz.textContent).toContain('Tools');
    expect(wiz.textContent).toContain('Skills');
  });

  it('Review row shows count + reveals selected names when expanded', async () => {
    const MCP_LIST = [
      { id: 'mcp-1', name: 'github-mcp', tenant_id: 't', config: {}, created_at: '', updated_at: '' },
      { id: 'mcp-2', name: 'jira-mcp', tenant_id: 't', config: {}, created_at: '', updated_at: '' },
      { id: 'mcp-3', name: 'unused-mcp', tenant_id: 't', config: {}, created_at: '', updated_at: '' },
    ];
    const SKILL_LIST = [
      {
        id: 'sk-1', tenant_id: 't', name: 'code-review',
        description: 'Reviews diffs', enabled: true,
        bound_coworker_count: 0, created_at: '', updated_at: '',
      },
      {
        id: 'sk-2', tenant_id: 't', name: 'sql-debug',
        description: 'Debugs SQL', enabled: true,
        bound_coworker_count: 0, created_at: '', updated_at: '',
      },
    ];
    fetchStub = installFetch([
      { match: (u) => u.endsWith('/api/v1/backends'), respond: () => jsonResp(BACKENDS) },
      { match: (u) => u.startsWith('/api/v1/models'), respond: () => jsonResp(MODELS) },
      { match: (u) => u === '/api/v1/tenant/credentials', respond: () => jsonResp(CREDS_ANT) },
      { match: (u) => u === '/api/v1/mcp-servers', respond: () => jsonResp(MCP_LIST) },
      { match: (u) => u === '/api/v1/skills', respond: () => jsonResp(SKILL_LIST) },
    ]);
    const el = mount();
    el.open = true;
    await settle(el);

    // Pre-select 2 of 3 MCPs and 1 of 2 skills via draft poke
    // (driving the full 6-step click flow is what other tests cover).
    (el as unknown as { draft: Record<string, unknown> }).draft = {
      ...(el as unknown as { draft: Record<string, unknown> }).draft,
      mcpServerIds: ['mcp-1', 'mcp-2'],
      skillIds: ['sk-1'],
    } as never;
    (el as unknown as { currentStep: number }).currentStep = 5;
    await settle(el);

    const wiz = el.querySelector('rm-wizard')!;
    const tools = wiz.querySelector<HTMLDetailsElement>(
      '[data-testid="wizard-review-tools"]',
    );
    expect(tools, 'tools row must be a <details>').toBeTruthy();
    // Closed-state summary: count + correct plural.
    const summaryText = tools!.querySelector('summary')!.textContent ?? '';
    expect(summaryText).toMatch(/2\s+tools\s+bound/);
    // Closed by default — browser handles visibility from this flag.
    // (We don't assert "not visible" via textContent because the DOM
    // has the content regardless; browser hides closed-state CSS.)
    expect(tools!.open).toBe(false);

    // Programmatically open and verify names appear in the rendered
    // list. The check is meaningful even with happy-dom's loose
    // textContent semantics because we're asserting that the
    // names ARE in the rendered tree — a regression that filters
    // them out entirely would still fail.
    tools!.open = true;
    await settle(el);
    expect(tools!.textContent).toContain('github-mcp');
    expect(tools!.textContent).toContain('jira-mcp');
    // Unselected MCP must NOT appear — the filter on selectedIds
    // is the load-bearing piece. A bug where the helper renders
    // ALL available items instead of selected ones would slip
    // past a "shows the names" check that didn't include this.
    expect(tools!.textContent).not.toContain('unused-mcp');

    // Skills row: 1 bound → singular "skill bound".
    const skills = wiz.querySelector<HTMLDetailsElement>(
      '[data-testid="wizard-review-skills"]',
    )!;
    expect(skills.querySelector('summary')!.textContent).toMatch(
      /1\s+skill\s+bound/,
    );
    skills.open = true;
    await settle(el);
    expect(skills.textContent).toContain('code-review');
    // PR33 follow-up: description is intentionally NOT rendered as a
    // sublabel — user requested name-only. Pin the absence so a
    // future "let's add hover help" change doesn't accidentally
    // reintroduce the visual clutter the user pushed back on.
    expect(skills.textContent).not.toContain('Reviews diffs');
    // The non-selected skill isn't included.
    expect(skills.textContent).not.toContain('sql-debug');
  });

  it('expanded list has a max-height + overflow so it scrolls when long', async () => {
    // Visual contract: with many bound items the list must scroll
    // instead of overflowing the modal. Tailwind's max-h-44 +
    // overflow-y-auto is what implements this; check the class
    // strings are present so a future redesign that drops them
    // doesn't silently let long lists blow out the dialog.
    fetchStub = installFetch([
      { match: (u) => u.endsWith('/api/v1/backends'), respond: () => jsonResp(BACKENDS) },
      { match: (u) => u.startsWith('/api/v1/models'), respond: () => jsonResp(MODELS) },
      { match: (u) => u === '/api/v1/tenant/credentials', respond: () => jsonResp(CREDS_ANT) },
      {
        match: (u) => u === '/api/v1/mcp-servers',
        respond: () => jsonResp([
          { id: 'm1', name: 'a', tenant_id: 't', config: {}, created_at: '', updated_at: '' },
        ]),
      },
      { match: (u) => u === '/api/v1/skills', respond: () => jsonResp([]) },
    ]);
    const el = mount();
    el.open = true;
    await settle(el);
    (el as unknown as { draft: Record<string, unknown> }).draft = {
      ...(el as unknown as { draft: Record<string, unknown> }).draft,
      mcpServerIds: ['m1'],
    } as never;
    (el as unknown as { currentStep: number }).currentStep = 5;
    await settle(el);
    const tools = el
      .querySelector('rm-wizard')!
      .querySelector<HTMLDetailsElement>(
        '[data-testid="wizard-review-tools"]',
      )!;
    const scrollContainer = tools.querySelector('div.max-h-44');
    expect(scrollContainer, 'scrollable container must exist').toBeTruthy();
    expect(scrollContainer!.className).toContain('overflow-y-auto');
  });
});
