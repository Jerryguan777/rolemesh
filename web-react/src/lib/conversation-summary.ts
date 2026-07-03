// ConversationSummary — extracted from web/src/components/sidebar.ts
// @ cf6b0f1 (spec §11). The recall panel needs the row shape only; the
// Lit sidebar's date grouping does not exist in this design.

import type { Conversation, Message } from '../api/client';

/** UI-side conversation row used by the recall panel. Distinct from
 *  the v1 `Conversation` REST shape — the panel needs an id, a
 *  preview line and a sort key. `createdAt` is the sort key: the wire
 *  `Conversation` has no `updated_at`, so "most recent" means "most
 *  recently created" (same constraint chat-shell.ts documents). */
export interface ConversationSummary {
  chatId: string;
  /** `conversation.name` when set; otherwise filled asynchronously
   *  from the first user message (loadConversationPreviews pattern). */
  preview: string | null;
  createdAt: string;
}

export function summaryFromConversation(c: Conversation): ConversationSummary {
  return {
    chatId: c.id,
    preview: c.name ?? null,
    createdAt: c.created_at,
  };
}

/** Newest-first by `created_at` (ISO-8601; lexicographic compare
 *  matches chronological order). */
export function sortNewestFirst(
  items: readonly ConversationSummary[],
): ConversationSummary[] {
  return [...items].sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1));
}

/** Extract the preview text from a conversation's message list: the
 *  first user message, or null when there is none yet. */
export function previewFromMessages(messages: readonly Message[]): string | null {
  const firstUser = messages.find((m) => m.role === 'user');
  return firstUser ? firstUser.content : null;
}
