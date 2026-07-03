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
