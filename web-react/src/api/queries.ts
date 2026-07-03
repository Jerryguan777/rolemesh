// TanStack Query hooks over the trimmed v1 client (spec §1.1). Server
// state only — chat stream state lives in the conversation-stream
// hook. Split into queries/<domain>.ts once this file passes ~3
// domains.

import { useQuery } from '@tanstack/react-query';
import { getApiClient, type SafetyDecisionFilters } from './client';

export function useCoworkers() {
  return useQuery({
    queryKey: ['coworkers'],
    queryFn: () => getApiClient().listCoworkers(),
  });
}

export function useModels() {
  return useQuery({
    queryKey: ['models'],
    queryFn: () => getApiClient().listModels(),
  });
}

export function useConversations(agentId: string | null) {
  return useQuery({
    queryKey: ['conversations', agentId],
    queryFn: () => getApiClient().listCoworkerConversations(agentId!),
    enabled: !!agentId,
  });
}

export function useMessages(chatId: string | null) {
  return useQuery({
    queryKey: ['messages', chatId],
    queryFn: () => getApiClient().listMessages(chatId!),
    enabled: !!chatId,
  });
}

// ---- coworker-wizard catalogues (Part C) ----
//
// Credentials / MCP servers / skills degrade to [] on failure (same
// catch-to-empty policy as the Lit wizard's loadCatalogues) — e.g. a
// member without `credential.byok.manage` gets a 403 on the
// credentials read; the wizard then renders those models as locked
// rather than erroring out.

export function useBackends(enabled: boolean) {
  return useQuery({
    queryKey: ['backends'],
    queryFn: () => getApiClient().getBackends(),
    enabled,
  });
}

export function useCredentials(enabled: boolean) {
  return useQuery({
    queryKey: ['credentials'],
    queryFn: () => getApiClient().listCredentials().catch(() => []),
    enabled,
  });
}

export function useMCPServers(enabled: boolean) {
  return useQuery({
    queryKey: ['mcp-servers'],
    queryFn: () => getApiClient().listMCPServers().catch(() => []),
    enabled,
  });
}

// ---- Approval policies page (Part H) ----

/** The tenant's approval policies. Surfaces load failures (page-owned
 *  list, not a degrade-to-empty catalogue read). */
export function useApprovalPolicies() {
  return useQuery({
    queryKey: ['approval-policies'],
    queryFn: () => getApiClient().listApprovalPolicies(),
  });
}

// ---- Safety rules page (Part I) ----

/** Tenant + visible platform rules. Page-owned list — surfaces errors. */
export function useSafetyRules() {
  return useQuery({
    queryKey: ['safety-rules'],
    queryFn: () => getApiClient().listSafetyRules(),
  });
}

/** Registered check catalog (behaviour metadata for the rule editor).
 *  Near-static — cache aggressively. */
export function useSafetyChecks() {
  return useQuery({
    queryKey: ['safety-checks'],
    queryFn: () => getApiClient().listSafetyChecks(),
    staleTime: 5 * 60_000,
  });
}

// ---- General / tenant settings page (Part K) ----

/** Owner-only tenant settings. Errors surface (the page maps a 403 to
 *  the friendly owner-only notice). */
export function useTenant() {
  return useQuery({
    queryKey: ['tenant'],
    queryFn: () => getApiClient().getTenant(),
    retry: false,
  });
}

// ---- Safety log page (Part J) ----

/** One page of the decision log. The filter object is part of the key —
 *  every filter/page change is its own cache entry; `placeholderData`
 *  keeps the previous page rendered while the next loads. */
export function useSafetyDecisions(filters: SafetyDecisionFilters) {
  return useQuery({
    queryKey: ['safety-decisions', filters],
    queryFn: () => getApiClient().listSafetyDecisions(filters),
    placeholderData: (prev) => prev,
  });
}

// ---- MCP server registry page (Part D) ----

/** The registry list (its own key — the wizard's catalogue hook above
 *  swallows errors; this page wants to surface a list-load failure). */
export function useMCPServerRegistry() {
  return useQuery({
    queryKey: ['mcp-servers'],
    queryFn: () => getApiClient().listMCPServers(),
  });
}

/** Client-derived per-server bound-coworker counts (spec D.1, Lit
 *  parity): the backend doesn't surface a count on MCPServer, so we
 *  list coworkers and fan out their binding reads. A failed read
 *  renders as 0 (never blocks). Shares the ['coworkers'] key and the
 *  per-coworker binding keys with the coworker wizard's step-4
 *  pre-fill, so the cache is reused. Returns Map<mcpServerId, count>. */
export function useMCPUsageCounts(enabled: boolean) {
  return useQuery({
    queryKey: ['mcp-usage-counts'],
    enabled,
    queryFn: async () => {
      const api = getApiClient();
      const coworkers = await api.listCoworkers();
      const results = await Promise.allSettled(
        coworkers.map((c) => api.listCoworkerMCPServers(c.id)),
      );
      const counts = new Map<string, number>();
      for (const r of results) {
        if (r.status !== 'fulfilled') continue;
        for (const binding of r.value) {
          const id = binding.mcp_server_id;
          counts.set(id, (counts.get(id) ?? 0) + 1);
        }
      }
      return counts;
    },
  });
}

export function useSkills(enabled: boolean) {
  return useQuery({
    queryKey: ['skills'],
    queryFn: () => getApiClient().listSkills().catch(() => []),
    enabled,
  });
}

// ---- Skills catalog page (Part E) ----
//
// The list carries bound_coworker_count + created_by_user_id, so the
// page needs no fan-out (unlike Part D's MCP usage counts). Same
// ['skills'] key the coworker wizard's step-5 catalogue reads, so a
// created skill appears there with no extra wiring.

export function useSkillRegistry() {
  return useQuery({
    queryKey: ['skills'],
    queryFn: () => getApiClient().listSkills(),
  });
}
