// OIDC PKCE login flow utilities.
//
// Usage:
//   1. fetchAuthConfig() — query backend for IdP config
//   2. startLogin(config) — generate PKCE pair, redirect to IdP
//   3. (IdP redirects back, our /oauth2/callback page stores code)
//   4. handleCallback() — exchange code for token via backend
//   5. getStoredToken() — retrieve token for API calls

const STORAGE_TOKEN = 'rm_id_token';
const STORAGE_VERIFIER = 'rm_pkce_verifier';
const STORAGE_STATE = 'rm_pkce_state';

export interface AuthConfig {
  provider: string;
  issuer: string;
  authorization_endpoint: string;
  client_id: string;
  redirect_uri: string;
  scope: string;
  audience: string;
}

export interface ExchangeResponse {
  id_token: string;
  access_token?: string;
  expires_in?: number;
  user: {
    id: string;
    tenant_id: string;
    name: string;
    email: string | null;
    role: string;
  };
}

// ---------- PKCE primitives ----------

function base64UrlEncode(bytes: Uint8Array): string {
  let str = '';
  for (const b of bytes) str += String.fromCharCode(b);
  return btoa(str).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function generateCodeVerifier(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return base64UrlEncode(bytes);
}

export async function generateCodeChallenge(verifier: string): Promise<string> {
  const data = new TextEncoder().encode(verifier);
  const digest = await crypto.subtle.digest('SHA-256', data);
  return base64UrlEncode(new Uint8Array(digest));
}

export function generateState(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return base64UrlEncode(bytes);
}

// ---------- Token storage ----------

export function getStoredToken(): string | null {
  return sessionStorage.getItem(STORAGE_TOKEN);
}

export function storeToken(token: string): void {
  sessionStorage.setItem(STORAGE_TOKEN, token);
}

export function clearToken(): void {
  sessionStorage.removeItem(STORAGE_TOKEN);
  sessionStorage.removeItem(STORAGE_VERIFIER);
  sessionStorage.removeItem(STORAGE_STATE);
}

export function isTokenExpired(token: string): boolean {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return true;
    const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')));
    const exp = payload.exp;
    if (typeof exp !== 'number') return true;
    return Date.now() / 1000 >= exp - 30; // 30s clock skew margin
  } catch {
    return true;
  }
}

// ---------- Login flow ----------

export async function fetchAuthConfig(): Promise<AuthConfig | null> {
  try {
    const res = await fetch('/api/auth/config');
    if (!res.ok) return null;
    return (await res.json()) as AuthConfig;
  } catch {
    return null;
  }
}

export async function startLogin(config: AuthConfig): Promise<void> {
  const verifier = generateCodeVerifier();
  const challenge = await generateCodeChallenge(verifier);
  const state = generateState();
  sessionStorage.setItem(STORAGE_VERIFIER, verifier);
  sessionStorage.setItem(STORAGE_STATE, state);

  const params = new URLSearchParams({
    response_type: 'code',
    client_id: config.client_id,
    redirect_uri: config.redirect_uri,
    scope: config.scope,
    state,
    code_challenge: challenge,
    code_challenge_method: 'S256',
  });
  if (config.audience && config.audience !== config.client_id) {
    params.set('audience', config.audience);
  }
  location.href = `${config.authorization_endpoint}?${params.toString()}`;
}

export async function handleCallback(): Promise<ExchangeResponse | null> {
  // Code is stored by /oauth2/callback page
  const code = sessionStorage.getItem('oidc_code');
  const verifier = sessionStorage.getItem(STORAGE_VERIFIER);
  if (!code || !verifier) return null;

  sessionStorage.removeItem('oidc_code');
  sessionStorage.removeItem('oidc_state');
  sessionStorage.removeItem(STORAGE_VERIFIER);

  const res = await fetch('/api/auth/exchange', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, code_verifier: verifier }),
  });
  if (!res.ok) return null;
  const data = (await res.json()) as ExchangeResponse;
  storeToken(data.id_token);
  return data;
}

export function logout(): void {
  clearToken();
  location.reload();
}
