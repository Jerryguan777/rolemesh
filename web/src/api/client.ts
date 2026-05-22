// Thin typed wrapper over the v1.1 OpenAPI contract. Intentionally
// minimal — frontends should always call through this module rather
// than hand-writing fetch URLs, so adding a new endpoint to the yaml
// (and re-running `npm run openapi:gen`) is the only way to grow the
// client surface. If you find yourself reaching for `fetch` directly
// from a component, that's a smell: extend this file instead.
//
// Why no fancy runtime path-builder: TS path-template inference adds
// a lot of complexity for marginal value. We hand-write one method
// per endpoint we actually need; the input/output shapes flow from
// `paths` so a yaml change shows up as a type error here.

import type { components, paths } from './generated/types.js';

export type ApiPaths = paths;

type GetResponseBody<
  P extends keyof paths,
  M extends keyof paths[P],
> = paths[P][M] extends {
  responses: { 200: { content: { 'application/json': infer B } } };
}
  ? B
  : never;

export type BackendList = GetResponseBody<'/api/v1/backends', 'get'>;
export type Coworker = components['schemas']['Coworker'];
export type Conversation = components['schemas']['Conversation'];
export type Message = components['schemas']['Message'];
export type Run = components['schemas']['Run'];
export type Me = components['schemas']['Me'];
export type Model = components['schemas']['Model'];
export type ModelProvider = components['schemas']['ModelProvider'];
export type CredentialResponse = components['schemas']['CredentialResponse'];
export type CredentialUpsert = components['schemas']['CredentialUpsert'];
export type MCPServer = components['schemas']['MCPServer'];
export type MCPServerCreate = components['schemas']['MCPServerCreate'];
export type MCPServerUpdate = components['schemas']['MCPServerUpdate'];
export type ApprovalPolicy = components['schemas']['ApprovalPolicy'];
export type ApprovalRequest = components['schemas']['ApprovalRequest'];
export type ApprovalRequestDetail =
  components['schemas']['ApprovalRequestDetail'];
export type ApprovalAuditEntry =
  components['schemas']['ApprovalAuditEntry'];
export type ApprovalDecide = components['schemas']['ApprovalDecide'];
export type ApprovalListScope = 'mine' | 'all';
export type Skill = components['schemas']['Skill'];
export type SkillSummary = components['schemas']['SkillSummary'];
export type SkillCreate = components['schemas']['SkillCreate'];
export type SkillUpdate = components['schemas']['SkillUpdate'];
export type SkillFile = components['schemas']['SkillFile'];
export type SkillFileUpsert = components['schemas']['SkillFileUpsert'];
export type CoworkerSkillBinding =
  components['schemas']['CoworkerSkillBinding'];

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

  async getBackends(): Promise<BackendList> {
    const resp = await fetch(`${this.baseUrl}/api/v1/backends`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as BackendList;
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
    const resp = await fetch(`${this.baseUrl}/api/v1/coworkers`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Coworker[];
  }

  async listCoworkerConversations(coworkerId: string): Promise<Conversation[]> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(coworkerId)}/conversations`,
      { method: 'GET', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Conversation[];
  }

  /** Create a fresh web-channel conversation for a coworker. The
   *  server auto-creates the `web` binding if one is missing and
   *  invents a `channel_chat_id`, so the SPA does not need to know
   *  anything about channel internals (design §3). */
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
    const resp = await fetch(
      `${this.baseUrl}/api/v1/conversations/${encodeURIComponent(conversationId)}/messages`,
      { method: 'GET', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Message[];
  }

  async getRun(runId: string): Promise<Run | null> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/runs/${encodeURIComponent(runId)}`,
      { method: 'GET', headers: this.headers() },
    );
    if (resp.status === 404) return null;
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Run;
  }

  // ------------------------------------------------------------------
  // Models (read-only)
  // ------------------------------------------------------------------

  async listModels(filters?: {
    provider?: ModelProvider | null;
    family?: components['schemas']['ModelFamily'] | null;
  }): Promise<Model[]> {
    const qs = new URLSearchParams();
    if (filters?.provider) qs.set('provider', filters.provider);
    if (filters?.family) qs.set('family', filters.family);
    const url = `${this.baseUrl}/api/v1/models${qs.size ? `?${qs}` : ''}`;
    const resp = await fetch(url, { method: 'GET', headers: this.headers() });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Model[];
  }

  // ------------------------------------------------------------------
  // Tenant credentials (write-only secret; read is metadata)
  // ------------------------------------------------------------------

  async listCredentials(): Promise<CredentialResponse[]> {
    const resp = await fetch(`${this.baseUrl}/api/v1/tenant/credentials`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as CredentialResponse[];
  }

  /** Upsert a tenant credential for ``provider``. The plaintext key
   *  flows server-side into the CredentialVault — never persisted in
   *  the SPA, never logged. The response carries metadata only. */
  async putCredential(
    provider: ModelProvider,
    body: CredentialUpsert,
  ): Promise<CredentialResponse> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/tenant/credentials/${encodeURIComponent(provider)}`,
      {
        method: 'PUT',
        headers: this.headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
      },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as CredentialResponse;
  }

  async deleteCredential(provider: ModelProvider): Promise<void> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/tenant/credentials/${encodeURIComponent(provider)}`,
      { method: 'DELETE', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
  }

  // ------------------------------------------------------------------
  // MCP servers
  // ------------------------------------------------------------------

  async listMCPServers(): Promise<MCPServer[]> {
    const resp = await fetch(`${this.baseUrl}/api/v1/mcp-servers`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as MCPServer[];
  }

  async createMCPServer(body: MCPServerCreate): Promise<MCPServer> {
    const resp = await fetch(`${this.baseUrl}/api/v1/mcp-servers`, {
      method: 'POST',
      headers: this.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as MCPServer;
  }

  async updateMCPServer(
    id: string,
    body: MCPServerUpdate,
  ): Promise<MCPServer> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/mcp-servers/${encodeURIComponent(id)}`,
      {
        method: 'PATCH',
        headers: this.headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
      },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as MCPServer;
  }

  async deleteMCPServer(id: string): Promise<void> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/mcp-servers/${encodeURIComponent(id)}`,
      { method: 'DELETE', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
  }

  // ------------------------------------------------------------------
  // Approvals (design §3 Phase 3)
  // ------------------------------------------------------------------

  /** List approval requests. Default scope is "mine" — caller is in
   *  ``resolved_approvers``. ``scope="all"`` requires admin+ role
   *  (the server gates this with 403). */
  async listApprovals(filters?: {
    scope?: ApprovalListScope;
    status?: string | null;
    coworkerId?: string | null;
  }): Promise<ApprovalRequest[]> {
    const qs = new URLSearchParams();
    if (filters?.scope) qs.set('scope', filters.scope);
    if (filters?.status) qs.set('status', filters.status);
    if (filters?.coworkerId) qs.set('coworker_id', filters.coworkerId);
    const url = `${this.baseUrl}/api/v1/approvals${qs.size ? `?${qs}` : ''}`;
    const resp = await fetch(url, { method: 'GET', headers: this.headers() });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as ApprovalRequest[];
  }

  async getApproval(id: string): Promise<ApprovalRequestDetail> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/approvals/${encodeURIComponent(id)}`,
      { method: 'GET', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as ApprovalRequestDetail;
  }

  async decideApproval(
    id: string,
    body: ApprovalDecide,
  ): Promise<ApprovalRequest> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/approvals/${encodeURIComponent(id)}/decide`,
      {
        method: 'POST',
        headers: this.headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
      },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as ApprovalRequest;
  }

  /** Returns `{ ok: true }` on 202, `{ ok: false, alreadyTerminal: true }`
   *  on 409 `ALREADY_TERMINAL`. Other failures throw `ApiError`. */
  // ------------------------------------------------------------------
  // Skills (design §3 Phase 3)
  // ------------------------------------------------------------------

  async listSkills(): Promise<SkillSummary[]> {
    const resp = await fetch(`${this.baseUrl}/api/v1/skills`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as SkillSummary[];
  }

  async getSkill(id: string): Promise<Skill> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/skills/${encodeURIComponent(id)}`,
      { method: 'GET', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Skill;
  }

  async createSkill(body: SkillCreate): Promise<Skill> {
    const resp = await fetch(`${this.baseUrl}/api/v1/skills`, {
      method: 'POST',
      headers: this.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Skill;
  }

  async updateSkill(id: string, body: SkillUpdate): Promise<Skill> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/skills/${encodeURIComponent(id)}`,
      {
        method: 'PATCH',
        headers: this.headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
      },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Skill;
  }

  async deleteSkill(id: string): Promise<void> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/skills/${encodeURIComponent(id)}`,
      { method: 'DELETE', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
  }

  /** Path segments are NOT URL-encoded — the server's
   *  ``{path:path}`` matcher accepts slashes, and an encoded ``%2F``
   *  would render the wrong path. Callers must validate path shape
   *  upstream (`SKILL_FILE_PATH_RE`) before reaching here. */
  async putSkillFile(
    skillId: string,
    path: string,
    body: SkillFileUpsert,
  ): Promise<SkillFile> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/skills/${encodeURIComponent(skillId)}/files/${path}`,
      {
        method: 'PUT',
        headers: this.headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
      },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as SkillFile;
  }

  async deleteSkillFile(skillId: string, path: string): Promise<void> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/skills/${encodeURIComponent(skillId)}/files/${path}`,
      { method: 'DELETE', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
  }

  async listCoworkerSkills(coworkerId: string): Promise<CoworkerSkillBinding[]> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(coworkerId)}/skills`,
      { method: 'GET', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as CoworkerSkillBinding[];
  }

  async enableCoworkerSkill(
    coworkerId: string, skillId: string,
  ): Promise<CoworkerSkillBinding> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(coworkerId)}` +
      `/skills/${encodeURIComponent(skillId)}`,
      { method: 'POST', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as CoworkerSkillBinding;
  }

  async disableCoworkerSkill(
    coworkerId: string, skillId: string,
  ): Promise<void> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(coworkerId)}` +
      `/skills/${encodeURIComponent(skillId)}`,
      { method: 'DELETE', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
  }

  async cancelRun(
    runId: string,
  ): Promise<{ ok: boolean; alreadyTerminal: boolean }> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/runs/${encodeURIComponent(runId)}/cancel`,
      { method: 'POST', headers: this.headers() },
    );
    if (resp.status === 409) return { ok: false, alreadyTerminal: true };
    if (!resp.ok) throw await this.parseError(resp);
    return { ok: true, alreadyTerminal: false };
  }
}

/** Shared, lazily-initialised client for components that don't want to
 *  thread one through. The token comes from `oidc-auth` storage. */
let _shared: ApiClient | null = null;
export function getApiClient(): ApiClient {
  if (!_shared) {
    _shared = new ApiClient('', sessionStorage.getItem('rm_id_token'));
    // Keep the shared client in lock-step with the OIDC refresh path.
    window.addEventListener('rm-token-refreshed', (e: Event) => {
      const tok = (e as CustomEvent<string>).detail;
      if (tok) _shared!.setToken(tok);
    });
  }
  return _shared;
}
