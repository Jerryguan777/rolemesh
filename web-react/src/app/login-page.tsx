// OIDC login page — React port of web/src/components/login-page.ts.
// Reads the IdP config from the backend and starts the PKCE redirect;
// the /oauth2/callback bridge page + useAuth() handle the return leg.

import { useEffect, useState } from 'react';
import { BrandMark } from '../components/brand-mark';
import {
  fetchAuthConfig,
  startLogin,
  type AuthConfig,
} from '../lib/oidc-auth';
import './login-page.css';

export function LoginPage() {
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchAuthConfig().then((cfg) => {
      if (cancelled) return;
      setConfig(cfg);
      if (!cfg) setError('Failed to load authentication configuration');
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleLogin() {
    if (!config) return;
    try {
      await startLogin(config);
    } catch (e) {
      setError(`Login failed: ${e}`);
    }
  }

  return (
    <div className="login-root">
      <div className="login-card">
        <BrandMark size={48} />
        <h1>RoleMesh</h1>
        <p>Sign in to continue</p>
        <button onClick={handleLogin} disabled={loading || !config}>
          {loading ? 'Loading...' : 'Sign in with SSO'}
        </button>
        {error ? <div className="login-error">{error}</div> : null}
      </div>
    </div>
  );
}
