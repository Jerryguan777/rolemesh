// Auth bootstrap — port of the `resolveToken()` / `resolveAuth()`
// state machine from web/src/app.ts (spec §10.3). Kept as a plain
// module (no react) so the three documented invariants are unit-
// testable; the `useAuth()` hook is a thin wrapper.
//
// Invariants preserved from the Lit code:
//   1. ORDERING — storeToken + api.setToken BEFORE the first getMe(),
//      or the first request goes out with no Authorization header →
//      401 → false redirect to login.
//   2. LEGACY BRANCH — no auth provider configured → authenticate
//      WITHOUT a token, WITHOUT getMe, WITHOUT a refresh scheduler
//      (chat-only deployments must not dead-end on a getMe 401).
//   3. REFRESH PROPAGATION — scheduleRefresh pushes the new token via
//      the `rm-token-refreshed` event; the shared ApiClient subscribes
//      in api/client.ts, and the WS client reads getStoredToken() per
//      ticket mint, which refreshTokenSilent keeps current.

import type { ApiClient, Me } from '../api/client';
import { setMe } from '../lib/capabilities';
import {
  fetchAuthConfig,
  getStoredToken,
  handleCallback,
  isTokenExpired,
  scheduleRefresh,
  storeToken,
} from '../lib/oidc-auth';

export type ResolvedAuth = 'login' | 'authenticated';

export interface AuthDeps {
  api: Pick<ApiClient, 'setToken' | 'getMe'>;
  fetchAuthConfig: () => Promise<unknown | null>;
  handleCallback: () => Promise<{ id_token: string } | null>;
  getStoredToken: () => string | null;
  isTokenExpired: (token: string) => boolean;
  storeToken: (token: string) => void;
  scheduleRefresh: (token: string, onRefreshed: (t: string) => void) => void;
  setMe: (me: Me | null) => void;
  /** location.search provider (test seam). */
  getSearch: () => string;
  hasPendingOidcCode: () => boolean;
  emitTokenRefreshed: (token: string) => void;
}

export function defaultAuthDeps(api: Pick<ApiClient, 'setToken' | 'getMe'>): AuthDeps {
  return {
    api,
    fetchAuthConfig,
    handleCallback,
    getStoredToken,
    isTokenExpired,
    storeToken,
    scheduleRefresh,
    setMe,
    getSearch: () => location.search,
    hasPendingOidcCode: () => sessionStorage.getItem('oidc_code') !== null,
    emitTokenRefreshed: (token) =>
      window.dispatchEvent(new CustomEvent('rm-token-refreshed', { detail: token })),
  };
}

type TokenOutcome =
  | { kind: 'token'; token: string }
  | { kind: 'legacy' }
  | { kind: 'login' };

async function resolveTokenOutcome(deps: AuthDeps): Promise<TokenOutcome> {
  // 1. Token in URL query params (backward compat / SaaS-passed).
  const urlToken = new URLSearchParams(deps.getSearch()).get('token');
  if (urlToken && !deps.isTokenExpired(urlToken)) {
    return { kind: 'token', token: urlToken };
  }

  // 2. OIDC callback: code stored by the /oauth2/callback bridge page.
  if (deps.hasPendingOidcCode()) {
    const exchanged = await deps.handleCallback();
    if (exchanged) return { kind: 'token', token: exchanged.id_token };
  }

  // 3. Stored token from a previous session.
  const stored = deps.getStoredToken();
  if (stored && !deps.isTokenExpired(stored)) {
    return { kind: 'token', token: stored };
  }

  // 4. OIDC configured but no token → show the login page.
  const config = await deps.fetchAuthConfig();
  if (config) return { kind: 'login' };

  // 5. Legacy / no auth provider configured → chat-only deployment.
  return { kind: 'legacy' };
}

export async function resolveAuthState(deps: AuthDeps): Promise<ResolvedAuth> {
  const outcome = await resolveTokenOutcome(deps);

  if (outcome.kind === 'login') return 'login';

  if (outcome.kind === 'token') {
    // Invariant 1: apply the resolved bearer BEFORE getMe and persist
    // it for the session (the URL / OIDC-callback branches otherwise
    // leave the token only in a local var).
    deps.storeToken(outcome.token);
    deps.api.setToken(outcome.token);
    deps.scheduleRefresh(outcome.token, deps.emitTokenRefreshed);

    // Populate the Me cache before any shell mounts (atomic bootstrap:
    // setMe writes a plain module variable, invisible to reactivity, so
    // the authenticated flip must come after this resolves).
    try {
      const me = await deps.api.getMe();
      deps.setMe(me);
    } catch (err) {
      console.error('failed to load /me', err);
      return 'login'; // fail closed; leave the cache unset
    }
  }
  // Invariant 2: the legacy branch reaches here without getMe/refresh.
  return 'authenticated';
}
