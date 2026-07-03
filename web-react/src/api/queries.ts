// TanStack Query hooks over the trimmed v1 client (spec §1.1). Server
// state only — chat stream state lives in the conversation-stream
// hook. Split into queries/<domain>.ts once this file passes ~3
// domains.

import { useQuery } from '@tanstack/react-query';
import { getApiClient } from './client';

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

export function useSkills(enabled: boolean) {
  return useQuery({
    queryKey: ['skills'],
    queryFn: () => getApiClient().listSkills().catch(() => []),
    enabled,
  });
}
