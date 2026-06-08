// @vitest-environment happy-dom
//
// <rm-app> — the auth state machine. The contract under test is the
// ATOMIC bootstrap (spec §7.2): authState must NOT flip to 'authenticated'
// until the Me cache is populated, so no sub-shell ever mounts while
// currentMe() is null. We drive the real capabilities module (no mock) so
// the setMe/currentMe wiring is exercised end-to-end; we mock only the two
// true boundaries — the api client's getMe and the oidc-auth services that
// decide which token branch resolveAuth takes.
//
// Shell-isolation trick: <rm-chat-shell> ALSO calls api.getMe() on mount,
// which would pollute a "getMe called N times" spy. settings-shell and
// activity-shell do NOT call getMe, so tests that need to count <rm-app>'s
// own getMe calls (or assert ZERO, for D2) route to #/manage or #/activity
// instead of the chat hash. That keeps the spy a faithful witness to
// <rm-app>'s behaviour, not the child shell's.

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';
import type { Me } from './api/client.js';

// --- boundary mocks (hoisted) ----------------------------------------------

const getMeSpy = vi.fn();
const scheduleRefreshSpy = vi.fn();

// Permissive client stub: the spied getMe plus no-op list/get methods so a
// slotted shell that incidentally mounts doesn't throw on unmocked calls.
vi.mock('./api/client.js', async () => {
  const actual = await vi.importActual<typeof import('./api/client.js')>(
    './api/client.js',
  );
  const empty = vi.fn().mockResolvedValue([]);
  const single = vi.fn().mockResolvedValue(null);
  return {
    ...actual,
    getApiClient: () => ({
      getMe: getMeSpy,
      listCoworkers: empty,
      listModels: empty,
      listCredentials: empty,
      listSkills: empty,
      listMCPServers: empty,
      listSafetyRules: empty,
      listSafetyDecisions: single,
      listTelegramLinks: empty,
      setToken: vi.fn(),
    }),
  };
});

// oidc-auth: the four token-source functions + the refresh scheduler. Each
// test sets these to steer resolveToken down a specific branch. Defaults
// (set in beforeEach) put us on the legacy / no-auth path.
const fetchAuthConfigSpy = vi.fn();
const getStoredTokenSpy = vi.fn();
const handleCallbackSpy = vi.fn();
const isTokenExpiredSpy = vi.fn();

vi.mock('./services/oidc-auth.js', async () => {
  const actual = await vi.importActual<
    typeof import('./services/oidc-auth.js')
  >('./services/oidc-auth.js');
  return {
    ...actual,
    fetchAuthConfig: (...a: unknown[]) => fetchAuthConfigSpy(...a),
    getStoredToken: (...a: unknown[]) => getStoredTokenSpy(...a),
    handleCallback: (...a: unknown[]) => handleCallbackSpy(...a),
    isTokenExpired: (...a: unknown[]) => isTokenExpiredSpy(...a),
    scheduleRefresh: (...a: unknown[]) => scheduleRefreshSpy(...a),
  };
});

// Real capabilities module — NOT mocked, so the cache wiring is tested.
import { currentMe, setMe } from './auth/capabilities.js';
// Import the element module ONCE; this registers <rm-app> + the shells.
import './app.js';
import type { RmApp } from './app.js';

const ME: Me = {
  user_id: 'u-alice',
  tenant_id: 't1',
  name: 'Alice',
  email: 'alice@example.com',
  role: 'admin',
  plane: 'tenant',
  capabilities: ['coworker.create', 'mcp.configure'],
};

/** Flush microtasks + Lit's update queue a bounded number of times so an
 *  awaited getMe and the subsequent re-render settle. */
async function settle(el: RmApp, rounds = 30): Promise<void> {
  for (let i = 0; i < rounds; i += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

function mount(hash = '#/manage/coworkers'): RmApp {
  location.hash = hash;
  const el = document.createElement('rm-app') as RmApp;
  document.body.appendChild(el);
  return el;
}

/** True if any of the three sub-shell elements is present in the subtree. */
function anyShell(el: RmApp): boolean {
  return !!el.querySelector(
    'rm-chat-shell, rm-settings-shell, rm-activity-shell',
  );
}

beforeEach(() => {
  setMe(null); // reset the real cache between tests
  getMeSpy.mockReset();
  scheduleRefreshSpy.mockReset();
  fetchAuthConfigSpy.mockReset().mockResolvedValue(null);
  getStoredTokenSpy.mockReset().mockReturnValue(null);
  handleCallbackSpy.mockReset().mockResolvedValue(null);
  isTokenExpiredSpy.mockReset().mockReturnValue(false);
  sessionStorage.clear();
  location.hash = '';
});

afterEach(() => {
  document.querySelectorAll('rm-app').forEach((el) => el.remove());
});

describe('<rm-app> atomic auth+me bootstrap (§7.2)', () => {
  it('shows Loading and mounts NO sub-shell while getMe is pending, then mounts the shell with currentMe() populated', async () => {
    // Stored-token branch with a getMe that resolves only when we release it.
    getStoredTokenSpy.mockReturnValue('tok-123');
    let release!: (me: Me) => void;
    getMeSpy.mockReturnValue(
      new Promise<Me>((res) => {
        release = res;
      }),
    );

    const el = mount('#/manage/coworkers');
    await settle(el);

    // While getMe is in flight: Loading text, no sub-shell, cache empty.
    expect(el.textContent).toContain('Loading...');
    expect(anyShell(el)).toBe(false);
    expect(currentMe()).toBeNull();

    // Resolve getMe; the shell must mount AND see a populated cache.
    let seenAtShellMount: Me | null = 'sentinel' as unknown as Me | null;
    // Capture currentMe() on the first render that produces a shell.
    release(ME);
    await settle(el);
    if (anyShell(el)) seenAtShellMount = currentMe();

    expect(anyShell(el)).toBe(true);
    expect(el.querySelector('rm-settings-shell')).not.toBeNull();
    expect(seenAtShellMount).toEqual(ME);
    expect(currentMe()).toEqual(ME);
  });

  it('drives getMe exactly once before any shell mounts on the stored-token branch', async () => {
    getStoredTokenSpy.mockReturnValue('tok-123');
    getMeSpy.mockResolvedValue(ME);

    const el = mount('#/manage/coworkers'); // settings-shell never calls getMe
    await settle(el);

    expect(getMeSpy).toHaveBeenCalledTimes(1);
    expect(scheduleRefreshSpy).toHaveBeenCalledWith('tok-123', expect.anything());
    expect(el.querySelector('rm-settings-shell')).not.toBeNull();
  });

  it('url ?token= branch authenticates and loads me', async () => {
    // happy-dom can set the query string via the full URL on location.
    history.replaceState(null, '', '/?token=url-tok#/manage/coworkers');
    getMeSpy.mockResolvedValue(ME);

    const el = mount('#/manage/coworkers');
    await settle(el);

    expect(getMeSpy).toHaveBeenCalledTimes(1);
    expect(currentMe()).toEqual(ME);
    expect(el.querySelector('rm-settings-shell')).not.toBeNull();
    // cleanup the search string so it doesn't bleed into later tests
    history.replaceState(null, '', '/');
  });
});

describe('<rm-app> getMe failure → login (fail closed)', () => {
  it('ends in login state with no shell and an unpopulated cache', async () => {
    getStoredTokenSpy.mockReturnValue('tok-123');
    getMeSpy.mockRejectedValue(new Error('401'));

    const el = mount('#/manage/coworkers');
    await settle(el);

    expect(el.querySelector('rm-login-page')).not.toBeNull();
    expect(anyShell(el)).toBe(false);
    expect(currentMe()).toBeNull();
  });
});

describe('<rm-app> OIDC-configured-but-no-token → login', () => {
  it('renders the login page without calling getMe', async () => {
    getStoredTokenSpy.mockReturnValue(null);
    fetchAuthConfigSpy.mockResolvedValue({ provider: 'oidc' });

    const el = mount('#/manage/coworkers');
    await settle(el);

    expect(el.querySelector('rm-login-page')).not.toBeNull();
    expect(getMeSpy).not.toHaveBeenCalled();
    expect(anyShell(el)).toBe(false);
  });
});

describe('<rm-app> legacy / no-auth branch (D2)', () => {
  it('authenticates WITHOUT calling getMe and WITHOUT scheduling a refresh', async () => {
    // No url token, no stored token, no OIDC config => legacy outcome.
    // Route to #/activity so the mounted shell (activity) never calls getMe
    // itself — the spy then faithfully reflects <rm-app>'s zero calls.
    getStoredTokenSpy.mockReturnValue(null);
    fetchAuthConfigSpy.mockResolvedValue(null);

    const el = mount('#/activity/safety');
    await settle(el);

    // Authenticated chat-only deployment: a shell mounts...
    expect(el.querySelector('rm-activity-shell')).not.toBeNull();
    expect(el.textContent).not.toContain('Loading...');
    expect(el.querySelector('rm-login-page')).toBeNull();
    // ...but NO getMe and NO refresh scheduler were involved (D2).
    expect(getMeSpy).not.toHaveBeenCalled();
    expect(scheduleRefreshSpy).not.toHaveBeenCalled();
    // And the cache stays empty (legacy has no Me).
    expect(currentMe()).toBeNull();
  });

  it('chat-only deployment lands on the chat shell at the root hash', async () => {
    getStoredTokenSpy.mockReturnValue(null);
    fetchAuthConfigSpy.mockResolvedValue(null);

    const el = mount('#/'); // root => chat shell
    await settle(el);

    expect(el.querySelector('rm-chat-shell')).not.toBeNull();
    expect(scheduleRefreshSpy).not.toHaveBeenCalled();
  });
});
