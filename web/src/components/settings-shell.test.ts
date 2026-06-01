// @vitest-environment happy-dom
// <rm-settings-shell> — pins the slot map. Every entry in the
// sidebar must:
//   1. render a heading + the correct page component when clicked
//   2. update `location.hash` to the canonical `#/manage/<slug>`
//   3. highlight itself with the `active` class when the hash points
//      to its slug
//
// We do NOT mock the v1.1 page components. Their internal API calls
// fail in the unit-test environment (no fetch backend), but that's
// fine — what we're pinning here is the slot routing, not the page
// behaviour. The page tests (coworkers-page.test.ts,
// safety-rules-page.test.ts, …) cover the inner behaviour
// independently.

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';

// Mock every API client the slotted pages reach for, so mounting
// them in a unit test doesn't fan out to console.warn noise from
// unmocked fetches. Stubs all return rejected promises shaped like
// `ApiError` so the pages render their empty / error states.
vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  const stub = vi.fn().mockResolvedValue([]);
  const single = vi.fn().mockResolvedValue(null);
  return {
    ...actual,
    getApiClient: () => ({
      listCoworkers: stub,
      listMCPServers: stub,
      listModels: stub,
      listCredentials: stub,
      listSkills: stub,
      listCoworkerSkills: stub,
      listSafetyRules: stub,
      listSafetyDecisions: stub,
      listTelegramLinks: stub,
      getMe: single,
      setToken: vi.fn(),
    }),
  };
});

vi.mock('../services/safety-admin-client.ts', () => ({
  getSafetyAdminClient: () => ({
    listRules: vi.fn().mockResolvedValue([]),
    listDecisions: vi.fn().mockResolvedValue([]),
  }),
}));

import { RmSettingsShell, slugFromHash } from './settings-shell.js';

interface LocationStub {
  hashAssignments: string[];
  hashGetter: string;
  restore: () => void;
}

function stubHash(initial = '#/manage/coworkers'): LocationStub {
  const stub: LocationStub = {
    hashAssignments: [],
    hashGetter: initial,
    restore: () => {},
  };
  const desc = Object.getOwnPropertyDescriptor(location, 'hash');
  Object.defineProperty(location, 'hash', {
    configurable: true,
    get: () => stub.hashGetter,
    set: (v: string) => {
      stub.hashAssignments.push(v);
      stub.hashGetter = v;
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    },
  });
  stub.restore = () => {
    if (desc) Object.defineProperty(location, 'hash', desc);
  };
  return stub;
}

async function settle(el: RmSettingsShell): Promise<void> {
  for (let i = 0; i < 20; i += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

async function mount(): Promise<RmSettingsShell> {
  const el = document.createElement('rm-settings-shell') as RmSettingsShell;
  document.body.appendChild(el);
  await settle(el);
  return el;
}

describe('slugFromHash', () => {
  it('returns the slug from a v2 manage hash', () => {
    expect(slugFromHash('#/manage/coworkers')).toBe('coworkers');
    expect(slugFromHash('#/manage/mcp-servers')).toBe('mcp-servers');
    expect(slugFromHash('#/manage/safety')).toBe('safety');
  });

  it('collapses sub-paths to the parent slug so the v1.1 page can route', () => {
    expect(slugFromHash('#/manage/skills/abc-skill')).toBe('skills');
  });

  it('falls back to "coworkers" for unknown slugs and stray hashes', () => {
    expect(slugFromHash('#/manage/totally-made-up')).toBe('coworkers');
    expect(slugFromHash('#/')).toBe('coworkers');
    expect(slugFromHash('')).toBe('coworkers');
  });
});

describe('<rm-settings-shell>', () => {
  let loc: LocationStub;

  beforeEach(() => {
    loc = stubHash('#/manage/coworkers');
  });

  afterEach(() => {
    document.querySelectorAll('rm-settings-shell').forEach((el) => el.remove());
    loc.restore();
    vi.clearAllMocks();
  });

  // The 10 entries from the v2-A prompt. Each row is
  // `[slug, expectedTitle, expectedTagInDOM]` — the slot map is the
  // contract we're pinning. expectedTag is `null` for the two
  // coming-soon placeholders + appearance (also coming-soon-shaped
  // but is its own component).
  const ENTRIES: Array<[string, string, string | null]> = [
    ['coworkers',          'Coworkers',          'rm-coworkers-page'],
    ['mcp-servers',        'MCP servers',        'rm-mcp-servers-page'],
    ['skills',             'Skills',             'rm-skills-page'],
    ['models',             'Models',             'rm-models-page'],
    ['credentials',        'Credentials',        'rm-credentials-page'],
    ['safety',             'Safety rules',       'rm-safety-rules-page'],
    ['approval-policies',  'Approval policies',  'rm-approval-policies-page'],
    ['general',            'General',            'rm-coming-soon'],
    ['members',            'Members',            'rm-coming-soon'],
    ['connected-channels', 'Connected channels', 'rm-connected-channels-page'],
    ['appearance',         'Appearance',         'rm-appearance-page'],
  ];

  it('renders one sidebar entry per page slot', async () => {
    const el = await mount();
    const entries = el.querySelectorAll('[data-testid="settings-nav-entry"]');
    expect(entries.length).toBe(ENTRIES.length);
  });

  it('lays out reauth banner above a sidebar+main grid (not as a grid item)', async () => {
    // Regression: connectedCallback used to set `style.display = 'block'`
    // inline, which beat the rendered <style> rule on specificity.
    // The banner then became grid item #1, the sidebar got pushed
    // into the 1fr column, and main wrapped to a new row — the
    // visible result was a sidebar across the top with content
    // below, instead of side-by-side columns.
    const el = await mount();
    const host = el;
    expect(host.style.display).toBe('flex');
    expect(host.style.flexDirection).toBe('column');
    // The sidebar + main MUST be wrapped in a grid container; the
    // banner is a flex sibling above it. The wrapper carries the
    // grid template — if someone deletes it the columns collapse.
    const layout = host.querySelector('.ss-layout');
    expect(layout, '.ss-layout wrapper must exist').not.toBeNull();
    const sidebar = host.querySelector('.ss-nav');
    const main = host.querySelector('.ss-main');
    expect(layout?.contains(sidebar!)).toBe(true);
    expect(layout?.contains(main!)).toBe(true);
  });

  it.each(ENTRIES)(
    '%s entry click navigates to #/manage/%s and renders %s',
    async (slug, _title, expectedTag) => {
      // Start from a slug that is NOT the one being clicked, so the
      // navigation guard (don't re-assign the same hash) doesn't
      // hide the assignment we're trying to observe.
      loc.restore();
      const other = slug === 'coworkers' ? 'general' : 'coworkers';
      loc = stubHash(`#/manage/${other}`);
      const el = await mount();
      const btn = el.querySelector<HTMLButtonElement>(
        `[data-testid="settings-nav-entry"][data-slug="${slug}"]`,
      );
      expect(btn, `nav entry for ${slug}`).not.toBeNull();
      btn!.click();
      await settle(el);
      expect(loc.hashAssignments).toContain(`#/manage/${slug}`);
      if (expectedTag) {
        const pane = el.querySelector(
          '[data-testid="settings-active-pane"]',
        );
        expect(
          pane?.querySelector(expectedTag),
          `expected pane to render <${expectedTag}> for slug=${slug}`,
        ).not.toBeNull();
      }
    },
  );

  it('switching slugs replaces the previous page (no stacked siblings)', async () => {
    // Regression — v1.1 MCPServersPage / CredentialsPage shipped a
    // private `remove(row)` method that shadowed
    // `HTMLElement.prototype.remove()`. Lit's NodePart teardown calls
    // `element.remove()` (no args) to detach the old page on a tab
    // switch; the 1-arg shadow threw "Cannot read properties of
    // undefined (reading 'id')" mid-clear, so the old element stayed
    // in the DOM next to the new one. The fix renamed the methods to
    // `removeServer` / `removeCredential`; pin the contract here so
    // a future reintroduction of `remove(row)` fails this test
    // instead of regressing the visible behaviour.
    loc.restore();
    loc = stubHash('#/manage/mcp-servers');
    const el = await mount();
    const pane = () =>
      el.querySelector('[data-testid="settings-active-pane"]')!;
    expect(pane().children).toHaveLength(1);
    expect(pane().children[0].tagName).toBe('RM-MCP-SERVERS-PAGE');

    // Walk through a few tabs that previously triggered the stack.
    for (const slug of ['skills', 'credentials', 'mcp-servers']) {
      const btn = el.querySelector<HTMLButtonElement>(
        `[data-testid="settings-nav-entry"][data-slug="${slug}"]`,
      );
      btn!.click();
      await settle(el);
      // Exactly one page must remain — the new one. No leftovers.
      expect(
        pane().children,
        `pane should have exactly 1 child after navigating to ${slug}`,
      ).toHaveLength(1);
    }
  });

  it('highlights the active entry with class="active"', async () => {
    loc.restore();
    loc = stubHash('#/manage/skills');
    const el = await mount();
    const active = el.querySelector(
      '[data-testid="settings-nav-entry"][data-slug="skills"]',
    );
    expect(active?.classList.contains('active')).toBe(true);
    const inactive = el.querySelector(
      '[data-testid="settings-nav-entry"][data-slug="coworkers"]',
    );
    expect(inactive?.classList.contains('active')).toBe(false);
  });

  it('renders the active page title in the header for assistive tech', async () => {
    loc.restore();
    loc = stubHash('#/manage/credentials');
    const el = await mount();
    const title = el.querySelector('[data-testid="settings-active-title"]');
    expect(title?.textContent?.trim()).toBe('Credentials');
  });

  it('Back-to-chat button navigates to #/', async () => {
    const el = await mount();
    el.querySelector<HTMLButtonElement>(
      '[data-testid="settings-back"]',
    )!.click();
    expect(loc.hashAssignments).toContain('#/');
  });
});
