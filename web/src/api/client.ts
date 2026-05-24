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
export type Backend = components['schemas']['Backend'];
export type BackendName = components['schemas']['BackendName'];
export type ModelFamily = components['schemas']['ModelFamily'];
export type CoworkerCreate = components['schemas']['CoworkerCreate'];
export type CoworkerUpdate = components['schemas']['CoworkerUpdate'];
export type CoworkerMCPBindingCreate =
  components['schemas']['CoworkerMCPBindingCreate'];
export type CoworkerMCPBindingResponse =
  components['schemas']['CoworkerMCPBindingResponse'];
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
export type SafetyRule = components['schemas']['SafetyRule'];
export type SafetyCheck = components['schemas']['SafetyCheck'];
export type SafetyDecision = components['schemas']['SafetyDecision'];
export type SafetyDecisionPage =
  components['schemas']['SafetyDecisionPage'];
export type SafetyRuleAuditEntry =
  components['schemas']['SafetyRuleAuditEntry'];
export type SafetyStage = components['schemas']['SafetyStage'];
export type SafetyVerdictAction =
  components['schemas']['SafetyVerdictAction'];
export type SafetyFinding = components['schemas']['SafetyFinding'];

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

  async createCoworker(body: CoworkerCreate): Promise<Coworker> {
    const resp = await fetch(`${this.baseUrl}/api/v1/coworkers`, {
      method: 'POST',
      headers: this.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Coworker;
  }

  async bindCoworkerMCPServer(
    coworkerId: string,
    body: CoworkerMCPBindingCreate,
  ): Promise<CoworkerMCPBindingResponse> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(coworkerId)}/mcp-servers`,
      {
        method: 'POST',
        headers: this.headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
      },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as CoworkerMCPBindingResponse;
  }

  async listCoworkers(): Promise<Coworker[]> {
    const resp = await fetch(`${this.baseUrl}/api/v1/coworkers`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Coworker[];
  }

  /** Patch selected fields on a coworker. Backend treats ABSENT keys
   *  as "leave alone"; explicit nulls follow the per-field rules in
   *  webui/schemas_v1.CoworkerUpdate. */
  async updateCoworker(id: string, body: CoworkerUpdate): Promise<Coworker> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(id)}`,
      {
        method: 'PATCH',
        headers: this.headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
      },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Coworker;
  }

  /** Hard-delete a coworker. Backend uses DB ON DELETE CASCADE to drop
   *  conversations / runs / messages — there is no soft-delete path. */
  async deleteCoworker(id: string): Promise<void> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(id)}`,
      { method: 'DELETE', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
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

  // ------------------------------------------------------------------
  // Safety (design §3 Phase 4 — GET-only on v1)
  //
  // Writes (POST/PATCH/DELETE /safety/rules) stay on the admin
  // surface; see services/safety-admin-client.ts for that path. The
  // lint guard in scripts/lint-no-admin-chat.mjs allowlists the three
  // safety-related files so write paths can keep using admin.
  // ------------------------------------------------------------------

  async listSafetyRules(filters?: {
    coworkerId?: string | null;
    stage?: SafetyStage | null;
    enabled?: boolean | null;
  }): Promise<SafetyRule[]> {
    const qs = new URLSearchParams();
    if (filters?.coworkerId) qs.set('coworker_id', filters.coworkerId);
    if (filters?.stage) qs.set('stage', filters.stage);
    if (filters?.enabled !== undefined && filters.enabled !== null) {
      qs.set('enabled', String(filters.enabled));
    }
    const url = `${this.baseUrl}/api/v1/safety/rules${qs.size ? `?${qs}` : ''}`;
    const resp = await fetch(url, { method: 'GET', headers: this.headers() });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as SafetyRule[];
  }

  async getSafetyRule(id: string): Promise<SafetyRule> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/safety/rules/${encodeURIComponent(id)}`,
      { method: 'GET', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as SafetyRule;
  }

  async listSafetyRuleAudit(
    ruleId: string,
    limit = 200,
  ): Promise<SafetyRuleAuditEntry[]> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/safety/rules/${encodeURIComponent(ruleId)}/audit?limit=${limit}`,
      { method: 'GET', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as SafetyRuleAuditEntry[];
  }

  async listSafetyChecks(): Promise<SafetyCheck[]> {
    const resp = await fetch(`${this.baseUrl}/api/v1/safety/checks`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as SafetyCheck[];
  }

  async listSafetyDecisions(filters?: {
    verdictAction?: SafetyVerdictAction | null;
    coworkerId?: string | null;
    stage?: SafetyStage | null;
    fromTs?: string | null;
    toTs?: string | null;
    limit?: number;
    offset?: number;
  }): Promise<SafetyDecisionPage> {
    const qs = new URLSearchParams();
    if (filters?.verdictAction) qs.set('verdict_action', filters.verdictAction);
    if (filters?.coworkerId) qs.set('coworker_id', filters.coworkerId);
    if (filters?.stage) qs.set('stage', filters.stage);
    if (filters?.fromTs) qs.set('from_ts', filters.fromTs);
    if (filters?.toTs) qs.set('to_ts', filters.toTs);
    if (filters?.limit !== undefined) qs.set('limit', String(filters.limit));
    if (filters?.offset !== undefined) qs.set('offset', String(filters.offset));
    const url = `${this.baseUrl}/api/v1/safety/decisions${qs.size ? `?${qs}` : ''}`;
    const resp = await fetch(url, { method: 'GET', headers: this.headers() });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as SafetyDecisionPage;
  }

  async getSafetyDecision(id: string): Promise<SafetyDecision> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/safety/decisions/${encodeURIComponent(id)}`,
      { method: 'GET', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as SafetyDecision;
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
