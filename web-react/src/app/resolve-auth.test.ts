import { describe, expect, it, vi } from 'vitest';
import { resolveAuthState, type AuthDeps } from './resolve-auth';
import type { Me } from '../api/client';

const ME: Me = {
  user_id: 'u1',
  tenant_id: 't1',
  role: 'member',
  plane: 'tenant',
  capabilities: ['coworker.use'],
};

function makeDeps(overrides: Partial<AuthDeps> = {}): AuthDeps {
  return {
    api: { setToken: vi.fn(), getMe: vi.fn(async () => ME) },
    fetchAuthConfig: vi.fn(async () => null),
    handleCallback: vi.fn(async () => null),
    getStoredToken: vi.fn(() => null),
    isTokenExpired: vi.fn(() => false),
    storeToken: vi.fn(),
    scheduleRefresh: vi.fn(),
    setMe: vi.fn(),
    getSearch: () => '',
    hasPendingOidcCode: () => false,
    emitTokenRefreshed: vi.fn(),
    ...overrides,
  };
}

describe('resolveAuthState', () => {
  it('legacy branch: no provider → authenticated WITHOUT getMe or refresh', async () => {
    const deps = makeDeps(); // fetchAuthConfig → null = no provider
    await expect(resolveAuthState(deps)).resolves.toBe('authenticated');
    expect(deps.api.getMe).not.toHaveBeenCalled();
    expect(deps.scheduleRefresh).not.toHaveBeenCalled();
    expect(deps.setMe).not.toHaveBeenCalled();
  });

  it('login branch: provider configured, no token → login', async () => {
    const deps = makeDeps({ fetchAuthConfig: vi.fn(async () => ({})) });
    await expect(resolveAuthState(deps)).resolves.toBe('login');
    expect(deps.api.getMe).not.toHaveBeenCalled();
  });

  it('stored-token branch: setToken is applied BEFORE getMe (ordering invariant)', async () => {
    const order: string[] = [];
    const deps = makeDeps({
      getStoredToken: vi.fn(() => 'tok-1'),
      api: {
        setToken: vi.fn(() => order.push('setToken')),
        getMe: vi.fn(async () => {
          order.push('getMe');
          return ME;
        }),
      },
    });
    await expect(resolveAuthState(deps)).resolves.toBe('authenticated');
    expect(order).toEqual(['setToken', 'getMe']);
    expect(deps.storeToken).toHaveBeenCalledWith('tok-1');
    expect(deps.scheduleRefresh).toHaveBeenCalled();
    expect(deps.setMe).toHaveBeenCalledWith(ME);
  });

  it('URL ?token= wins over everything and is persisted', async () => {
    const deps = makeDeps({ getSearch: () => '?token=url-tok&agent_id=a1' });
    await expect(resolveAuthState(deps)).resolves.toBe('authenticated');
    expect(deps.storeToken).toHaveBeenCalledWith('url-tok');
  });

  it('OIDC callback code is exchanged for a token', async () => {
    const deps = makeDeps({
      hasPendingOidcCode: () => true,
      handleCallback: vi.fn(async () => ({ id_token: 'exchanged' })),
    });
    await expect(resolveAuthState(deps)).resolves.toBe('authenticated');
    expect(deps.storeToken).toHaveBeenCalledWith('exchanged');
  });

  it('fails closed to login when getMe rejects on the token branch', async () => {
    const deps = makeDeps({
      getStoredToken: vi.fn(() => 'tok-bad'),
      api: {
        setToken: vi.fn(),
        getMe: vi.fn(async () => {
          throw new Error('401');
        }),
      },
    });
    await expect(resolveAuthState(deps)).resolves.toBe('login');
    expect(deps.setMe).not.toHaveBeenCalled();
  });
});
