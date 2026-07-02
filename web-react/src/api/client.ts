// Ported from web/src/api/client.ts @ cf6b0f1, trimmed to the chat
// surface (spec §10.1). Method names are kept identical to the Lit
// client so the two SPAs stay cross-readable; when a settings page
// lands here, lift its methods from web/ rather than hand-writing.
//
// Thin typed wrapper over the v1 OpenAPI contract. Frontends always
// call through this module rather than hand-writing fetch URLs —
// adding a new endpoint to the yaml (and re-running
// `npm run openapi:gen`) is the only way to grow the client surface.

import type { components, paths } from './generated/types';
import { getStoredToken } from '../lib/oidc-auth';

export type ApiPaths = paths;

export type BackendName = components['schemas']['BackendName'];
export type Coworker = components['schemas']['Coworker'];
export type Conversation = components['schemas']['Conversation'];
export type Message = components['schemas']['Message'];
export type Me = components['schemas']['Me'];
export type Model = components['schemas']['Model'];
export type ApprovalRequest = components['schemas']['ApprovalRequest'];

export type ErrorResponseBody =
  paths['/api/v1/runs/{id}/cancel']['post']['responses']['409']['content']['application/json'];

export class ApiError extends Error {
  readonly status: number;
  readonly body: ErrorResponseBody | null;
  constructor(status: number, body: ErrorResponseBody | null, message: string) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export class ApiClient {
  private token: string | null;

  constructor(
    private readonly baseUrl: string = '',
    token: string | null = null,
  ) {
    this.token = token;
  }

  setToken(token: string | null): void {
    this.token = token;
  }

  private headers(extra?: Record<string, string>): Record<string, string> {
    const h: Record<string, string> = { Accept: 'application/json' };
    if (this.token) h['Authorization'] = `Bearer ${this.token}`;
    if (extra) Object.assign(h, extra);
    return h;
  }

  private async parseError(resp: Response): Promise<ApiError> {
    let body: ErrorResponseBody | null = null;
    try {
      body = (await resp.json()) as ErrorResponseBody;
    } catch {
      // Not JSON — fall through to status-only error.
    }
    const msg = body?.message || `HTTP ${resp.status}`;
    return new ApiError(resp.status, body, msg);
  }

  async getMe(): Promise<Me> {
    const resp = await fetch(`${this.baseUrl}/api/v1/me`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Me;
  }

  async listCoworkers(): Promise<Coworker[]> {
    // Paged endpoint; request the max window and return items so callers
    // keep their array shape (full page-through UI is a follow-up).
    const resp = await fetch(`${this.baseUrl}/api/v1/coworkers?limit=200`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return ((await resp.json()) as components['schemas']['CoworkerPage']).items;
  }

  async listCoworkerConversations(coworkerId: string): Promise<Conversation[]> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(coworkerId)}/conversations?limit=200`,
      { method: 'GET', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return ((await resp.json()) as components['schemas']['ConversationPage'])
      .items;
  }

  /** Create a fresh web-channel conversation for a coworker. The
   *  server auto-creates the `web` binding if one is missing and
   *  invents a `channel_chat_id`, so the SPA does not need to know
   *  anything about channel internals. */
  async createCoworkerConversation(
    coworkerId: string,
    name?: string | null,
  ): Promise<Conversation> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(coworkerId)}/conversations`,
      {
        method: 'POST',
        headers: this.headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ name: name ?? null }),
      },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Conversation;
  }

  async listMessages(conversationId: string): Promise<Message[]> {
    // Cursor-paginated endpoint. Request the max window and return the
    // (oldest-first) items; this shows the newest 200 messages. "Load
    // older" via next_cursor is a follow-up once the chat UI grows a
    // scrollback control.
    const resp = await fetch(
      `${this.baseUrl}/api/v1/conversations/${encodeURIComponent(conversationId)}/messages?limit=200`,
      { method: 'GET', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return ((await resp.json()) as components['schemas']['MessagePage']).items;
  }

  async listModels(): Promise<Model[]> {
    const resp = await fetch(`${this.baseUrl}/api/v1/models`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Model[];
  }
}

/** Shared, lazily-initialised client for components that don't want to
 *  thread one through. The token comes from `oidc-auth` storage. */
let _shared: ApiClient | null = null;
export function getApiClient(): ApiClient {
  if (!_shared) {
    _shared = new ApiClient('', getStoredToken());
    // Keep the shared client in lock-step with the OIDC refresh path.
    window.addEventListener('rm-token-refreshed', (e: Event) => {
      const tok = (e as CustomEvent<string>).detail;
      if (tok) _shared!.setToken(tok);
    });
  }
  return _shared;
}
