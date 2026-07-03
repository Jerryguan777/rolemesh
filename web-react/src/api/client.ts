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
export type Backend = components['schemas']['Backend'];
export type BackendList = Backend[];
export type Coworker = components['schemas']['Coworker'];
export type CoworkerCreate = components['schemas']['CoworkerCreate'];
export type CoworkerUpdate = components['schemas']['CoworkerUpdate'];
export type CoworkerMCPBindingCreate =
  components['schemas']['CoworkerMCPBindingCreate'];
export type CoworkerMCPBindingResponse =
  components['schemas']['CoworkerMCPBindingResponse'];
export type CoworkerSkillBinding =
  components['schemas']['CoworkerSkillBinding'];
export type Conversation = components['schemas']['Conversation'];
export type Message = components['schemas']['Message'];
export type Me = components['schemas']['Me'];
export type Model = components['schemas']['Model'];
export type ModelProvider = components['schemas']['ModelProvider'];
export type CredentialResponse = components['schemas']['CredentialResponse'];
export type CredentialUpsert = components['schemas']['CredentialUpsert'];
export type ConditionExpr = components['schemas']['ConditionExpr'];
export type ApprovalPolicy = components['schemas']['ApprovalPolicy'];
export type ApprovalPolicyCreate = components['schemas']['ApprovalPolicyCreate'];
export type ApprovalPolicyUpdate = components['schemas']['ApprovalPolicyUpdate'];
export type SafetyRule = components['schemas']['SafetyRule'];
export type SafetyRuleCreate = components['schemas']['SafetyRuleCreate'];
export type SafetyRuleUpdate = components['schemas']['SafetyRuleUpdate'];
export type SafetyRuleAuditEntry = components['schemas']['SafetyRuleAuditEntry'];
export type SafetyCheck = components['schemas']['SafetyCheck'];
export type SafetyStage = components['schemas']['SafetyStage'];
export type SafetyVerdictAction = components['schemas']['SafetyVerdictAction'];
export type TenantResponse = components['schemas']['TenantResponse'];
export type TenantUpdate = components['schemas']['TenantUpdate'];
export type SafetyDecision = components['schemas']['SafetyDecision'];
export type SafetyDecisionPage = components['schemas']['SafetyDecisionPage'];
export type SafetyFinding = components['schemas']['SafetyFinding'];
export type SafetyFindingSeverity = components['schemas']['SafetyFindingSeverity'];

/** Filter set for the safety-decisions list + CSV export (Part J).
 *  Field names mirror the Lit client; keys serialize to the wire's
 *  snake_case query params. */
export interface SafetyDecisionFilters {
  verdictAction?: SafetyVerdictAction | null;
  coworkerId?: string | null;
  stage?: SafetyStage | null;
  fromTs?: string | null;
  toTs?: string | null;
  checkId?: string | null;
  ruleId?: string | null;
  limit?: number;
  offset?: number;
}
export type MCPServer = components['schemas']['MCPServer'];
export type MCPServerCreate = components['schemas']['MCPServerCreate'];
export type MCPServerUpdate = components['schemas']['MCPServerUpdate'];
export type MCPType = components['schemas']['MCPType'];
export type MCPAuthMode = components['schemas']['MCPAuthMode'];
export type SkillSummary = components['schemas']['SkillSummary'];
export type Skill = components['schemas']['Skill'];
export type SkillCreate = components['schemas']['SkillCreate'];
export type SkillUpdate = components['schemas']['SkillUpdate'];
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

  /** Body-carrying request helper — EVERY JSON mutation goes through
   *  here so the Content-Type can never be forgotten. Browsers default
   *  a string body to `text/plain;charset=UTF-8`, and FastAPI then
   *  validates the whole body as one string → 422 "Input should be a
   *  valid dictionary". Owning the JSON.stringify here makes the bug
   *  class unrepresentable. Throws ApiError on non-2xx. */
  private async fetchJson(
    method: 'POST' | 'PATCH' | 'PUT',
    url: string,
    body: unknown,
  ): Promise<Response> {
    const resp = await fetch(url, {
      method,
      headers: this.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return resp;
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
    const resp = await this.fetchJson(
      'POST',
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(coworkerId)}/conversations`,
      { name: name ?? null },
    );
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

  // ------------------------------------------------------------------
  // Coworker management (Part C). Method names identical to the Lit
  // client (spec §11) — lift, don't rewrite.
  // ------------------------------------------------------------------

  async getBackends(): Promise<BackendList> {
    const resp = await fetch(`${this.baseUrl}/api/v1/backends`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as BackendList;
  }

  async createCoworker(body: CoworkerCreate): Promise<Coworker> {
    const resp = await this.fetchJson(
      'POST',
      `${this.baseUrl}/api/v1/coworkers`,
      body,
    );
    return (await resp.json()) as Coworker;
  }

  /** Patch selected fields on a coworker. Backend treats ABSENT keys
   *  as "leave alone"; explicit nulls follow the per-field rules in
   *  webui/schemas_v1.CoworkerUpdate (note: `model_id: null` to CLEAR
   *  is currently rejected by the handler — omit the key instead). */
  async updateCoworker(id: string, body: CoworkerUpdate): Promise<Coworker> {
    const resp = await this.fetchJson(
      'PATCH',
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(id)}`,
      body,
    );
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

  /** Flip a coworker's visibility to `shared` (tenant-wide). Real
   *  authorization is the ownership escape server-side (a member may
   *  share only what they created). Idempotent. */
  async shareCoworker(id: string): Promise<Coworker> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(id)}/share`,
      { method: 'POST', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Coworker;
  }

  /** Flip a coworker's visibility back to `private`. */
  async unshareCoworker(id: string): Promise<Coworker> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(id)}/unshare`,
      { method: 'POST', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Coworker;
  }

  async listCredentials(): Promise<CredentialResponse[]> {
    const resp = await fetch(`${this.baseUrl}/api/v1/credentials`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as CredentialResponse[];
  }

  async listApprovalPolicies(): Promise<ApprovalPolicy[]> {
    // Paged endpoint; request the max window and return the items so
    // callers keep their array shape (page-through UI is a follow-up).
    const resp = await fetch(
      `${this.baseUrl}/api/v1/approvals/policies?limit=200`,
      { method: 'GET', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return ((await resp.json()) as components['schemas']['ApprovalPolicyPage'])
      .items;
  }

  async createApprovalPolicy(body: ApprovalPolicyCreate): Promise<ApprovalPolicy> {
    const resp = await this.fetchJson(
      'POST',
      `${this.baseUrl}/api/v1/approvals/policies`,
      body,
    );
    return (await resp.json()) as ApprovalPolicy;
  }

  async updateApprovalPolicy(
    id: string,
    body: ApprovalPolicyUpdate,
  ): Promise<ApprovalPolicy> {
    const resp = await this.fetchJson(
      'PATCH',
      `${this.baseUrl}/api/v1/approvals/policies/${encodeURIComponent(id)}`,
      body,
    );
    return (await resp.json()) as ApprovalPolicy;
  }

  async deleteApprovalPolicy(id: string): Promise<void> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/approvals/policies/${encodeURIComponent(id)}`,
      { method: 'DELETE', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
  }

  // ------------------------------------------------------------------
  // Tenant settings (Part K). Owner-only: both routes 403 without
  // `tenant.manage` — the page turns the GET 403 into a friendly
  // full-page notice.
  // ------------------------------------------------------------------

  async getTenant(): Promise<TenantResponse> {
    const resp = await fetch(`${this.baseUrl}/api/v1/tenant`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as TenantResponse;
  }

  /** PATCH — only `name` + `max_concurrent_containers` have a write
   *  path (slug is fixed at creation; plan is a platform concern). */
  async updateTenant(body: TenantUpdate): Promise<TenantResponse> {
    const resp = await this.fetchJson(
      'PATCH',
      `${this.baseUrl}/api/v1/tenant`,
      body,
    );
    return (await resp.json()) as TenantResponse;
  }

  // ------------------------------------------------------------------
  // Safety rules (Part I). Method names identical to the Lit client.
  // ------------------------------------------------------------------

  /** Tenant rules + visible platform-owned rules (source=platform). */
  async listSafetyRules(): Promise<SafetyRule[]> {
    const resp = await fetch(`${this.baseUrl}/api/v1/safety/rules?limit=200`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return ((await resp.json()) as components['schemas']['SafetyRulePage']).items;
  }

  async createSafetyRule(body: SafetyRuleCreate): Promise<SafetyRule> {
    const resp = await this.fetchJson(
      'POST',
      `${this.baseUrl}/api/v1/safety/rules`,
      body,
    );
    return (await resp.json()) as SafetyRule;
  }

  /** PATCH. Scope is immutable — SafetyRuleUpdate has no coworker_id
   *  field; the sanctioned scope-change path is Duplicate. */
  async updateSafetyRule(id: string, body: SafetyRuleUpdate): Promise<SafetyRule> {
    const resp = await this.fetchJson(
      'PATCH',
      `${this.baseUrl}/api/v1/safety/rules/${encodeURIComponent(id)}`,
      body,
    );
    return (await resp.json()) as SafetyRule;
  }

  async deleteSafetyRule(id: string): Promise<void> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/safety/rules/${encodeURIComponent(id)}`,
      { method: 'DELETE', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
  }

  /** Newest-first change history for one rule (404 on unknown id —
   *  RLS-safe existence semantics). */
  async listSafetyRuleAudit(id: string): Promise<SafetyRuleAuditEntry[]> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/safety/rules/${encodeURIComponent(id)}/audit?limit=200`,
      { method: 'GET', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return ((await resp.json()) as components['schemas']['SafetyRuleAuditPage'])
      .items;
  }

  private decisionsQuery(filters?: SafetyDecisionFilters): URLSearchParams {
    const qs = new URLSearchParams();
    if (filters?.verdictAction) qs.set('verdict_action', filters.verdictAction);
    if (filters?.coworkerId) qs.set('coworker_id', filters.coworkerId);
    if (filters?.stage) qs.set('stage', filters.stage);
    if (filters?.fromTs) qs.set('from_ts', filters.fromTs);
    if (filters?.toTs) qs.set('to_ts', filters.toTs);
    if (filters?.checkId) qs.set('check_id', filters.checkId);
    if (filters?.ruleId) qs.set('rule_id', filters.ruleId);
    if (filters?.limit !== undefined) qs.set('limit', String(filters.limit));
    if (filters?.offset !== undefined) qs.set('offset', String(filters.offset));
    return qs;
  }

  /** Safety-decision log page (Part J). Paged envelope carries `total`
   *  in-band — no second count call. */
  async listSafetyDecisions(
    filters?: SafetyDecisionFilters,
  ): Promise<SafetyDecisionPage> {
    const qs = this.decisionsQuery(filters);
    const resp = await fetch(
      `${this.baseUrl}/api/v1/safety/decisions${qs.size ? `?${qs}` : ''}`,
      { method: 'GET', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as SafetyDecisionPage;
  }

  /** CSV export — an authenticated blob fetch (a plain <a href> cannot
   *  carry the bearer token). Carries the SAME filters as the list
   *  (Lit parity); pagination params are excluded — export is bulk. */
  async downloadSafetyDecisionsCsv(
    filters?: SafetyDecisionFilters,
  ): Promise<Blob> {
    const qs = this.decisionsQuery({ ...filters, limit: undefined, offset: undefined });
    const resp = await fetch(
      `${this.baseUrl}/api/v1/safety/decisions.csv${qs.size ? `?${qs}` : ''}`,
      { method: 'GET', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return resp.blob();
  }

  /** Registered check catalog (rule-editor metadata; stable order). */
  async listSafetyChecks(): Promise<SafetyCheck[]> {
    const resp = await fetch(`${this.baseUrl}/api/v1/safety/checks`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as SafetyCheck[];
  }

  /** Upsert (create OR rotate) the credential for one provider.
   *  `PUT /credentials/{provider}` — 200 CredentialResponse (no secret). */
  async putCredential(
    provider: ModelProvider,
    body: CredentialUpsert,
  ): Promise<CredentialResponse> {
    const resp = await this.fetchJson(
      'PUT',
      `${this.baseUrl}/api/v1/credentials/${encodeURIComponent(provider)}`,
      body,
    );
    return (await resp.json()) as CredentialResponse;
  }

  /** `DELETE /credentials/{provider}` (204). A 409 RESOURCE_IN_USE
   *  carries `details.coworker_ids` — surfaced per-row by the page. */
  async deleteCredential(provider: ModelProvider): Promise<void> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/credentials/${encodeURIComponent(provider)}`,
      { method: 'DELETE', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
  }

  async listMCPServers(): Promise<MCPServer[]> {
    const resp = await fetch(`${this.baseUrl}/api/v1/mcp-servers?limit=200`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return ((await resp.json()) as components['schemas']['MCPServerPage']).items;
  }

  async createMCPServer(body: MCPServerCreate): Promise<MCPServer> {
    const resp = await this.fetchJson(
      'POST',
      `${this.baseUrl}/api/v1/mcp-servers`,
      body,
    );
    return (await resp.json()) as MCPServer;
  }

  async updateMCPServer(id: string, body: MCPServerUpdate): Promise<MCPServer> {
    const resp = await this.fetchJson(
      'PATCH',
      `${this.baseUrl}/api/v1/mcp-servers/${encodeURIComponent(id)}`,
      body,
    );
    return (await resp.json()) as MCPServer;
  }

  /** Hard-delete an MCP server. 204 on success; 409 RESOURCE_IN_USE
   *  when the server is still bound to any coworker — the backend is
   *  the authoritative gate (the page's client-side count is advisory
   *  only). The 409 body carries `details.coworker_ids`. */
  async deleteMCPServer(id: string): Promise<void> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/mcp-servers/${encodeURIComponent(id)}`,
      { method: 'DELETE', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
  }

  async listSkills(): Promise<SkillSummary[]> {
    const resp = await fetch(`${this.baseUrl}/api/v1/skills?limit=200`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return ((await resp.json()) as components['schemas']['SkillSummaryPage'])
      .items;
  }

  /** List MCP-server bindings for a coworker (wizard edit pre-fill). */
  async listCoworkerMCPServers(
    coworkerId: string,
  ): Promise<CoworkerMCPBindingResponse[]> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(coworkerId)}/mcp-servers`,
      { method: 'GET', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as CoworkerMCPBindingResponse[];
  }

  async bindCoworkerMCPServer(
    coworkerId: string,
    body: CoworkerMCPBindingCreate,
  ): Promise<CoworkerMCPBindingResponse> {
    const resp = await this.fetchJson(
      'POST',
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(coworkerId)}/mcp-servers`,
      body,
    );
    return (await resp.json()) as CoworkerMCPBindingResponse;
  }

  /** Remove a single MCP-server binding (path takes the MCP SERVER id,
   *  not a separate binding id — the junction is keyed by the pair). */
  async unbindCoworkerMCPServer(
    coworkerId: string,
    mcpServerId: string,
  ): Promise<void> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(coworkerId)}` +
        `/mcp-servers/${encodeURIComponent(mcpServerId)}`,
      { method: 'DELETE', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
  }

  // ------------------------------------------------------------------
  // Skill catalog CRUD + share (Part E). Method names identical to the
  // Lit client (spec §11).
  // ------------------------------------------------------------------

  /** Full skill incl. the `files` map — the dialog fetches this on
   *  edit-open to seed Instructions + the file tree. */
  async getSkill(id: string): Promise<Skill> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/skills/${encodeURIComponent(id)}`,
      { method: 'GET', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Skill;
  }

  async createSkill(body: SkillCreate): Promise<Skill> {
    const resp = await this.fetchJson(
      'POST',
      `${this.baseUrl}/api/v1/skills`,
      body,
    );
    return (await resp.json()) as Skill;
  }

  /** PATCH. `name` is read-only server-side — omit it (edit sends the
   *  full `files` map for atomic replacement per the Lit contract). */
  async updateSkill(id: string, body: SkillUpdate): Promise<Skill> {
    const resp = await this.fetchJson(
      'PATCH',
      `${this.baseUrl}/api/v1/skills/${encodeURIComponent(id)}`,
      body,
    );
    return (await resp.json()) as Skill;
  }

  /** 204; 409 RESOURCE_IN_USE when bound to any coworker. */
  async deleteSkill(id: string): Promise<void> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/skills/${encodeURIComponent(id)}`,
      { method: 'DELETE', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
  }

  async shareSkill(id: string): Promise<Skill> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/skills/${encodeURIComponent(id)}/share`,
      { method: 'POST', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Skill;
  }

  async unshareSkill(id: string): Promise<Skill> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/skills/${encodeURIComponent(id)}/unshare`,
      { method: 'POST', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as Skill;
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
    coworkerId: string,
    skillId: string,
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
    coworkerId: string,
    skillId: string,
  ): Promise<void> {
    const resp = await fetch(
      `${this.baseUrl}/api/v1/coworkers/${encodeURIComponent(coworkerId)}` +
        `/skills/${encodeURIComponent(skillId)}`,
      { method: 'DELETE', headers: this.headers() },
    );
    if (!resp.ok) throw await this.parseError(resp);
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
