// useAuth — the React wrapper over resolve-auth.ts. Owns the three-way
// UI state and the `rm-auth-failed` listener (refresh exhausted, etc.).

import { useEffect, useState } from 'react';
import { getApiClient } from '../api/client';
import { defaultAuthDeps, resolveAuthState } from './resolve-auth';

export type AuthUiState = 'loading' | 'login' | 'authenticated';

export function useAuth(): AuthUiState {
  const [state, setState] = useState<AuthUiState>('loading');

  useEffect(() => {
    let cancelled = false;
    const onAuthFailed = () => setState('login');
    window.addEventListener('rm-auth-failed', onAuthFailed);
    void resolveAuthState(defaultAuthDeps(getApiClient())).then((s) => {
      if (!cancelled) setState(s);
    });
    return () => {
      cancelled = true;
      window.removeEventListener('rm-auth-failed', onAuthFailed);
    };
  }, []);

  return state;
}
