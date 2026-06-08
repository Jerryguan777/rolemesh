// @vitest-environment happy-dom
// <rm-platform-shell> — pins the platform-plane rail + route map (RBAC
// UI spec §4). With a platform_admin `me` (all 4 platform caps):
//   - all four nav entries render, in rail order
//   - the active `tenants` slug routes to <rm-platform-tenants-page>
//   - the other three slugs route to <rm-coming-soon>
//   - capability gating: an entry whose capability is absent disappears
//
// We seed the REAL setMe()/hasCapability() from capabilities.ts (no
// mock) — the production code keeps NO role->capability table, so we
// feed the exact wire capability list a platform_admin receives and
// assert which entries survive the gate. The slotted tenants-page reaches
// for the api client on mount; we stub that boundary so the unit test
// doesn't fan out to an unmocked fetch.

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';

vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  return {
    ...actual,
    getApiClient: () => ({
      listTenants: vi.fn().mockResolvedValue([]),
    }),
  };
});

import { RmPlatformShell, slugFromHash } from './platform-shell.js';
import { setMe } from '../auth/capabilities.js';
import type { Me } from '../api/client.js';

interface LocationStub {
  hashAssignments: string[];
  hashGetter: string;
  restore: () => void;
}

function stubHash(initial = '#/platform/tenants'): LocationStub {
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

async function settle(el: RmPlatformShell): Promise<void> {
  for (let i = 0; i < 20; i += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

async function mount(): Promise<RmPlatformShell> {
  const el = document.createElement('rm-platform-shell') as RmPlatformShell;
  document.body.appendChild(el);
  await settle(el);
  return el;
}

// The four platform-only capabilities a platform_admin receives from
// `GET /api/v1/me` (mirrors permissions.py:_PLATFORM_ONLY_ACTIONS). TEST
// INPUT ONLY — production keeps no role->capability map.
const PLATFORM_CAPS = [
  'platform.tenant.manage',
  'model.manage',
  'credential.pool.manage',
  'safety.platform.manage',
];

function makePlatformMe(capabilities: string[] = PLATFORM_CAPS): Me {
  return {
    user_id: 'pa-1',
    tenant_id: '__platform__',
    name: 'Platform Admin',
    email: 'admin@platform.test',
    role: 'platform_admin',
    plane: 'platform',
    capabilities,
  };
}

function visibleSlugs(el: RmPlatformShell): string[] {
  return Array.from(
    el.querySelectorAll('[data-testid="platform-nav-entry"]'),
  ).map((b) => b.getAttribute('data-slug') ?? '');
}

describe('platform-shell slugFromHash', () => {
  it('returns the slug from a platform hash', () => {
    expect(slugFromHash('#/platform/tenants')).toBe('tenants');
    expect(slugFromHash('#/platform/models')).toBe('models');
    expect(slugFromHash('#/platform/credentials')).toBe('credentials');
    expect(slugFromHash('#/platform/safety')).toBe('safety');
  });

  it('collapses sub-paths to the parent slug', () => {
    expect(slugFromHash('#/platform/tenants/t-123')).toBe('tenants');
  });

  it('falls back to "tenants" for unknown slugs and stray hashes', () => {
    expect(slugFromHash('#/platform/made-up')).toBe('tenants');
    expect(slugFromHash('#/manage/safety')).toBe('tenants');
    expect(slugFromHash('')).toBe('tenants');
  });
});

describe('<rm-platform-shell>', () => {
  let loc: LocationStub;

  beforeEach(() => {
    loc = stubHash('#/platform/tenants');
    setMe(makePlatformMe());
  });

  afterEach(() => {
    document.querySelectorAll('rm-platform-shell').forEach((el) => el.remove());
    loc.restore();
    setMe(null);
    vi.clearAllMocks();
  });

  it('renders all four platform nav entries in rail order for a platform_admin', async () => {
    const el = await mount();
    expect(visibleSlugs(el)).toEqual([
      'tenants',
      'models',
      'credentials',
      'safety',
    ]);
  });

  it('routes the active "tenants" slug to <rm-platform-tenants-page>', async () => {
    const el = await mount();
    const pane = el.querySelector('[data-testid="platform-active-pane"]');
    expect(pane?.querySelector('rm-platform-tenants-page')).not.toBeNull();
    // ...and NOT a coming-soon stub.
    expect(pane?.querySelector('rm-coming-soon')).toBeNull();
  });

  // The three deferred slugs each route to a <rm-coming-soon> stub
  // (spec §5.8). Pin each individually so a future mis-wire (e.g.
  // pointing 'models' at a real page) fails here.
  const STUB_SLUGS: Array<[string, string]> = [
    ['models', 'Models management'],
    ['credentials', 'Credential pool'],
    ['safety', 'Platform safety rules'],
  ];

  it.each(STUB_SLUGS)(
    '%s slug routes to <rm-coming-soon> labelled "%s"',
    async (slug, label) => {
      loc.restore();
      loc = stubHash(`#/platform/${slug}`);
      const el = await mount();
      const pane = el.querySelector('[data-testid="platform-active-pane"]');
      const stub = pane?.querySelector('rm-coming-soon');
      expect(stub, `expected <rm-coming-soon> for ${slug}`).not.toBeNull();
      expect((stub as { label?: string } | null)?.label).toBe(label);
      // The real tenants page must NOT have mounted on a stub slug.
      expect(pane?.querySelector('rm-platform-tenants-page')).toBeNull();
    },
  );

  it('clicking a nav entry navigates to #/platform/<slug>', async () => {
    const el = await mount();
    const btn = el.querySelector<HTMLButtonElement>(
      '[data-testid="platform-nav-entry"][data-slug="models"]',
    );
    btn!.click();
    await settle(el);
    expect(loc.hashAssignments).toContain('#/platform/models');
  });

  it('highlights the active entry with class="active"', async () => {
    loc.restore();
    loc = stubHash('#/platform/credentials');
    const el = await mount();
    const active = el.querySelector(
      '[data-testid="platform-nav-entry"][data-slug="credentials"]',
    );
    expect(active?.classList.contains('active')).toBe(true);
    const inactive = el.querySelector(
      '[data-testid="platform-nav-entry"][data-slug="tenants"]',
    );
    expect(inactive?.classList.contains('active')).toBe(false);
  });

  it('hides an entry whose capability the user lacks (partial-capability degrade)', async () => {
    // A hypothetical future platform role with only tenant.manage. The
    // gate must drop the other three entries — driven by the wire caps,
    // NOT by the role name 'platform_admin'.
    setMe(makePlatformMe(['platform.tenant.manage']));
    const el = await mount();
    expect(visibleSlugs(el)).toEqual(['tenants']);
  });

  it('renders <rm-access-denied> in the pane when URL-jumping to a gated slug', async () => {
    setMe(makePlatformMe(['platform.tenant.manage']));
    loc.restore();
    loc = stubHash('#/platform/models');
    const el = await mount();
    const pane = el.querySelector('[data-testid="platform-active-pane"]');
    const denied = pane?.querySelector('rm-access-denied');
    expect(denied, 'access-denied should render in the pane').not.toBeNull();
    expect((denied as { capability?: string } | null)?.capability).toBe(
      'model.manage',
    );
    // The rail still shows what the user CAN see (no silent redirect).
    expect(visibleSlugs(el)).toEqual(['tenants']);
  });

  it('renders a loading fallback when me is not yet cached', async () => {
    setMe(null);
    const el = await mount();
    expect(
      el.querySelector('[data-testid="platform-loading"]'),
    ).not.toBeNull();
    expect(
      el.querySelectorAll('[data-testid="platform-nav-entry"]'),
    ).toHaveLength(0);
  });
});
