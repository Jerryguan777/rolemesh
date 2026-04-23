/**
 * Client for /api/admin/safety/* and /api/admin/tenants/{tid}/safety/*
 *
 * Auth: Bearer header (admin endpoints do not accept ?token= query
 * params like the chat endpoints). Token is sourced from the shared
 * OIDC helper; 401s silently refresh once.
 */
import { getStoredToken, refreshTokenSilent } from './oidc-auth.js';

export type SafetyStage =
  | 'input_prompt'
  | 'pre_tool_call'
  | 'post_tool_result'
  | 'model_output'
  | 'pre_compaction';

export type SafetyVerdictAction =
  | 'allow'
  | 'block'
  | 'redact'
  | 'warn'
  | 'require_approval';

export interface SafetyCheckMeta {
  id: string;
  version: string;
  stages: SafetyStage[];
  cost_class: 'cheap' | 'slow';
  supported_codes: string[];
  config_schema: Record<string, unknown> | null;
}

export interface SafetyRule {
  id: string;
  tenant_id: string;
  coworker_id: string | null;
  stage: SafetyStage;
  check_id: string;
  config: Record<string, unknown>;
  priority: number;
  enabled: boolean;
  description: string;
  created_at: string;
  updated_at: string;
}

export interface SafetyRuleCreateBody {
  stage: SafetyStage;
  check_id: string;
  config: Record<string, unknown>;
  coworker_id?: string | null;
  priority?: number;
  enabled?: boolean;
  description?: string;
}

export interface SafetyRuleUpdateBody {
  stage?: SafetyStage;
  check_id?: string;
  config?: Record<string, unknown>;
  priority?: number;
  enabled?: boolean;
  description?: string;
}

export interface SafetyFinding {
  code: string;
  severity: 'info' | 'low' | 'medium' | 'high' | 'critical';
  message: string;
  metadata?: Record<string, unknown>;
}

export interface SafetyDecision {
  id: string;
  tenant_id: string;
  coworker_id: string | null;
  conversation_id: string | null;
  job_id: string | null;
  stage: SafetyStage;
  verdict_action: SafetyVerdictAction;
  triggered_rule_ids: string[];
  findings: SafetyFinding[];
  context_digest: string;
  context_summary: string;
  approval_context?: Record<string, unknown> | null;
  created_at: string;
}

export interface DecisionsPage {
  total: number;
  items: SafetyDecision[];
}

export interface SafetyRuleAuditEntry {
  id: string;
  rule_id: string;
  tenant_id: string;
  actor_user_id: string | null;
  action: 'created' | 'updated' | 'deleted';
  before_state: Record<string, unknown> | null;
  after_state: Record<string, unknown> | null;
  note: string | null;
  created_at: string;
}

// ---- Internals --------------------------------------------------------

async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const attempt = async (token: string): Promise<Response> => {
    const headers = new Headers(init.headers);
    headers.set('Authorization', `Bearer ${token}`);
    if (init.body && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }
    return fetch(path, { ...init, headers });
  };
  const token = getStoredToken();
  if (!token) throw new Error('not authenticated');
  let res = await attempt(token);
  if (res.status === 401) {
    const refreshed = await refreshTokenSilent();
    if (refreshed) res = await attempt(refreshed);
  }
  return res;
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = '';
    try {
      const body = (await res.json()) as { detail?: unknown };
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch {
      detail = res.statusText;
    }
    throw new Error(`HTTP ${res.status}: ${detail}`);
  }
  return (await res.json()) as T;
}

function buildQuery(params: Record<string, string | number | undefined | null>): string {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    usp.append(k, String(v));
  }
  const s = usp.toString();
  return s ? `?${s}` : '';
}

// ---- Public API -------------------------------------------------------

let _cachedTenantId: string | null = null;

// Invalidate the cache when auth dies (exhausted refresh, session
// expired, explicit logout). Without this, the next sign-in in the
// same tab would see the previous user's tenant_id from module state
// — a silent cross-tenant leak in the admin UI. ``rm-auth-failed`` is
// emitted from the OIDC helper on unrecoverable auth errors (see
// app.ts listener). Safe to wire at module scope: this file is
// imported once per page load, and ``window.addEventListener`` is
// idempotent across HMR.
if (typeof window !== 'undefined') {
  window.addEventListener('rm-auth-failed', () => {
    _cachedTenantId = null;
  });
}

/** Returns the current admin's tenant_id. Cached for the session. */
export async function getTenantId(): Promise<string> {
  if (_cachedTenantId) return _cachedTenantId;
  const res = await apiFetch('/api/admin/tenant');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const body = (await res.json()) as { id: string };
  _cachedTenantId = body.id;
  return body.id;
}

/** Exported so tests + explicit logout flows can wipe the cache. */
export function clearTenantIdCache(): void {
  _cachedTenantId = null;
}

export async function listChecks(): Promise<SafetyCheckMeta[]> {
  const res = await apiFetch('/api/admin/safety/checks');
  return jsonOrThrow<SafetyCheckMeta[]>(res);
}

export async function listRules(filters: {
  coworker_id?: string;
  stage?: SafetyStage;
  enabled?: boolean;
} = {}): Promise<SafetyRule[]> {
  const qs = buildQuery({
    coworker_id: filters.coworker_id,
    stage: filters.stage,
    enabled: filters.enabled === undefined ? undefined : String(filters.enabled),
  });
  const res = await apiFetch(`/api/admin/safety/rules${qs}`);
  return jsonOrThrow<SafetyRule[]>(res);
}

export async function createRule(body: SafetyRuleCreateBody): Promise<SafetyRule> {
  const res = await apiFetch('/api/admin/safety/rules', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  return jsonOrThrow<SafetyRule>(res);
}

export async function updateRule(
  ruleId: string,
  body: SafetyRuleUpdateBody,
): Promise<SafetyRule> {
  const res = await apiFetch(`/api/admin/safety/rules/${encodeURIComponent(ruleId)}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
  return jsonOrThrow<SafetyRule>(res);
}

export async function deleteRule(ruleId: string): Promise<void> {
  const res = await apiFetch(`/api/admin/safety/rules/${encodeURIComponent(ruleId)}`, {
    method: 'DELETE',
  });
  if (!res.ok && res.status !== 204) {
    throw new Error(`HTTP ${res.status}`);
  }
}

export async function listRuleAudit(
  tenantId: string,
  ruleId: string,
  limit = 200,
): Promise<SafetyRuleAuditEntry[]> {
  const res = await apiFetch(
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/safety/rules/${encodeURIComponent(ruleId)}/audit?limit=${limit}`,
  );
  return jsonOrThrow<SafetyRuleAuditEntry[]>(res);
}

export async function listDecisions(
  tenantId: string,
  filters: {
    verdict_action?: SafetyVerdictAction;
    coworker_id?: string;
    stage?: SafetyStage;
    from_ts?: string;
    to_ts?: string;
    limit?: number;
    offset?: number;
  } = {},
): Promise<DecisionsPage> {
  const qs = buildQuery({
    verdict_action: filters.verdict_action,
    coworker_id: filters.coworker_id,
    stage: filters.stage,
    from_ts: filters.from_ts,
    to_ts: filters.to_ts,
    limit: filters.limit,
    offset: filters.offset,
  });
  const res = await apiFetch(
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/safety/decisions${qs}`,
  );
  return jsonOrThrow<DecisionsPage>(res);
}

export async function getDecision(
  tenantId: string,
  decisionId: string,
): Promise<SafetyDecision> {
  const res = await apiFetch(
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/safety/decisions/${encodeURIComponent(decisionId)}`,
  );
  return jsonOrThrow<SafetyDecision>(res);
}

/** Returns a URL the browser can open to download the CSV with
 * the caller's current bearer token. Used by "Export CSV" buttons —
 * the Authorization header can't be set on <a href> so we inline
 * the token via the standard header-in-URL fallback. */
export function decisionsCsvUrl(
  tenantId: string,
  filters: {
    verdict_action?: SafetyVerdictAction;
    coworker_id?: string;
    stage?: SafetyStage;
    from_ts?: string;
    to_ts?: string;
  } = {},
): string | null {
  // CSV endpoint requires Bearer header — browsers can't attach one
  // on a plain <a href> click. Callers should use apiFetch + blob
  // instead; keep this function as a convenience for constructing
  // the path half.
  const qs = buildQuery({
    verdict_action: filters.verdict_action,
    coworker_id: filters.coworker_id,
    stage: filters.stage,
    from_ts: filters.from_ts,
    to_ts: filters.to_ts,
  });
  return `/api/admin/tenants/${encodeURIComponent(tenantId)}/safety/decisions.csv${qs}`;
}

export async function downloadDecisionsCsv(
  tenantId: string,
  filters: Parameters<typeof decisionsCsvUrl>[1] = {},
): Promise<Blob> {
  const url = decisionsCsvUrl(tenantId, filters);
  if (!url) throw new Error('csv url unavailable');
  const res = await apiFetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.blob();
}

// Simple coworker list wrapper — decisions UI filters by coworker_id,
// but we don't want the full agent admin API surface here. Keep this
// to a shape-only typed helper so the page can pick names.
export interface CoworkerSummary {
  id: string;
  name: string;
}

export async function listCoworkers(): Promise<CoworkerSummary[]> {
  const res = await apiFetch('/api/admin/agents');
  if (!res.ok) return [];
  const body = (await res.json()) as Array<{ id: string; name: string }>;
  return body.map((c) => ({ id: c.id, name: c.name }));
}
