// @vitest-environment happy-dom
// <rm-message-editor> — pins the v2-C composer toolbar contract:
//   * three buttons render (attach / coworker selector / send)
//   * coworker menu opens / closes
//   * picking a coworker triggers a location.href navigation with
//     the new agent_id and drops chat_id (so chat-panel starts fresh)
//   * attach button is a placeholder (no navigation, transient toast)
//   * existing send / stop semantics still work
//
// We do NOT pin the placeholder text or the colour palette — those
// are visual polish that should be free to evolve.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import './message-editor.js';
import type { MessageEditor } from './message-editor.js';
import type { Coworker } from '../api/client.js';

const listCoworkersSpy = vi.fn();

vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  return {
    ...actual,
    getApiClient: () => ({
      listCoworkers: listCoworkersSpy,
    }),
  };
});

function makeCoworker(id: string, name: string, role = 'agent'): Coworker {
  return {
    id,
    tenant_id: 't1',
    name,
    folder: name.toLowerCase(),
    agent_backend: 'claude',
    status: 'active',
    agent_role: role,
    max_concurrent: 1,
    created_at: '2026-01-01T00:00:00Z',
  } as unknown as Coworker;
}

interface LocationStub {
  hrefAssignments: string[];
  hrefGetter: string;
  searchGetter: string;
  restore: () => void;
}

function stubLocation(search = '?agent_id=cw-a'): LocationStub {
  const stub: LocationStub = {
    hrefAssignments: [],
    hrefGetter: `http://localhost/${search}#/`,
    searchGetter: search,
    restore: () => {},
  };
  const desc = {
    href: Object.getOwnPropertyDescriptor(location, 'href'),
    search: Object.getOwnPropertyDescriptor(location, 'search'),
  };
  Object.defineProperty(location, 'href', {
    configurable: true,
    get: () => stub.hrefGetter,
    set: (v: string) => {
      stub.hrefAssignments.push(v);
      stub.hrefGetter = v;
    },
  });
  Object.defineProperty(location, 'search', {
    configurable: true,
    get: () => stub.searchGetter,
  });
  stub.restore = () => {
    if (desc.href) Object.defineProperty(location, 'href', desc.href);
    if (desc.search) Object.defineProperty(location, 'search', desc.search);
  };
  return stub;
}

async function settle(el: MessageEditor): Promise<void> {
  for (let i = 0; i < 20; i += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

async function mount(): Promise<MessageEditor> {
  const el = document.createElement('rm-message-editor') as MessageEditor;
  el.connected = true;
  document.body.appendChild(el);
  await settle(el);
  return el;
}

describe('<rm-message-editor>', () => {
  let loc: LocationStub;

  beforeEach(() => {
    listCoworkersSpy.mockReset();
    listCoworkersSpy.mockResolvedValue([
      makeCoworker('cw-a', 'Ops coworker', 'operations'),
      makeCoworker('cw-b', 'Finance coworker', 'finance'),
    ]);
    loc = stubLocation('?agent_id=cw-a');
  });

  afterEach(() => {
    document
      .querySelectorAll('rm-message-editor')
      .forEach((el) => el.remove());
    loc.restore();
  });

  it('renders all three toolbar buttons (attach / coworker selector / send)', async () => {
    const el = await mount();
    expect(el.querySelector('[data-testid="composer-attach"]')).not.toBeNull();
    expect(
      el.querySelector('[data-testid="composer-coworker-btn"]'),
    ).not.toBeNull();
    expect(el.querySelector('[data-testid="composer-send"]')).not.toBeNull();
  });

  it('shows the active coworker name on the selector chip', async () => {
    const el = await mount();
    const chip = el.querySelector('[data-testid="composer-coworker-btn"]');
    expect(chip?.textContent).toContain('Ops coworker');
  });

  it('opens the coworker menu on chip click and lists every coworker', async () => {
    const el = await mount();
    expect(el.querySelector('[data-testid="composer-coworker-menu"]')).toBeNull();
    el.querySelector<HTMLButtonElement>(
      '[data-testid="composer-coworker-btn"]',
    )!.click();
    await settle(el);
    const menu = el.querySelector('[data-testid="composer-coworker-menu"]');
    expect(menu).not.toBeNull();
    const options = el.querySelectorAll(
      '[data-testid="composer-coworker-option"]',
    );
    expect(options.length).toBe(2);
  });

  it('picking a different coworker assigns location.href with the new agent_id and drops chat_id', async () => {
    loc.restore();
    loc = stubLocation('?agent_id=cw-a&chat_id=conv-1');
    const el = await mount();
    el.querySelector<HTMLButtonElement>(
      '[data-testid="composer-coworker-btn"]',
    )!.click();
    await settle(el);
    const opt = el.querySelector<HTMLButtonElement>(
      '[data-coworker-id="cw-b"]',
    )!;
    opt.click();
    expect(loc.hrefAssignments).toHaveLength(1);
    expect(loc.hrefAssignments[0]).toContain('agent_id=cw-b');
    // Switching coworker must reset the conversation so chat-panel
    // starts fresh. We pin the absence of chat_id explicitly.
    expect(loc.hrefAssignments[0]).not.toContain('chat_id=');
  });

  it('picking the SAME coworker is a no-op (does not navigate)', async () => {
    const el = await mount();
    el.querySelector<HTMLButtonElement>(
      '[data-testid="composer-coworker-btn"]',
    )!.click();
    await settle(el);
    const same = el.querySelector<HTMLButtonElement>(
      '[data-coworker-id="cw-a"]',
    )!;
    same.click();
    expect(loc.hrefAssignments).toHaveLength(0);
  });

  it('attach button does NOT navigate; surfaces a toast instead', async () => {
    const el = await mount();
    el.querySelector<HTMLButtonElement>(
      '[data-testid="composer-attach"]',
    )!.click();
    await settle(el);
    expect(loc.hrefAssignments).toHaveLength(0);
    expect(
      el.querySelector('[data-testid="composer-attach-toast"]'),
    ).not.toBeNull();
  });

  it('typing + clicking send emits the send event with the trimmed value', async () => {
    const el = await mount();
    const ta = el.querySelector<HTMLTextAreaElement>('textarea')!;
    ta.value = 'hello world';
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);
    const events: CustomEvent[] = [];
    el.addEventListener('send', (e) => events.push(e as CustomEvent));
    el.querySelector<HTMLButtonElement>('[data-testid="composer-send"]')!.click();
    expect(events).toHaveLength(1);
    expect((events[0].detail as { content: string }).content).toBe('hello world');
  });

  it('empty input keeps send disabled (no event)', async () => {
    const el = await mount();
    const events: CustomEvent[] = [];
    el.addEventListener('send', (e) => events.push(e as CustomEvent));
    el.querySelector<HTMLButtonElement>('[data-testid="composer-send"]')!.click();
    expect(events).toHaveLength(0);
  });

  it('a failed listCoworkers leaves the editor usable (empty selector menu only)', async () => {
    listCoworkersSpy.mockRejectedValue(new Error('boom'));
    const el = await mount();
    // Send still works.
    const ta = el.querySelector<HTMLTextAreaElement>('textarea')!;
    ta.value = 'hi';
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    await settle(el);
    const events: CustomEvent[] = [];
    el.addEventListener('send', (e) => events.push(e as CustomEvent));
    el.querySelector<HTMLButtonElement>('[data-testid="composer-send"]')!.click();
    expect(events).toHaveLength(1);
    // Menu shows the "No coworkers configured" hint.
    el.querySelector<HTMLButtonElement>(
      '[data-testid="composer-coworker-btn"]',
    )!.click();
    await settle(el);
    const menu = el.querySelector('[data-testid="composer-coworker-menu"]');
    expect(menu?.textContent?.toLowerCase()).toContain('no coworkers');
  });
});
