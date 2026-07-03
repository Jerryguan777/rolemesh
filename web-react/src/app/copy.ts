// Shared UI strings (spec Appendix B). The chat-surface noun lives in
// ONE constant so decision D-1 is a one-line flip.

export const AGENT_NOUN = 'Agents';

export const COPY = {
  pickerTitle: 'RoleMesh',
  tabAssistants: 'Assistants',
  tabAgents: AGENT_NOUN,
  emptyNoAgent: 'Choose an agent to start a conversation.',
  emptyNoAgentCta: 'Browse agents',
  emptyConversation: (name: string) => `Start a conversation with ${name}.`,
  recallTitle: 'Recall conversations',
  recallNoAgent: 'Choose an agent to see its conversations.',
  recallNoConversations: 'No conversations yet — start one below.',
  inputPlaceholder: 'Type the prompt and press return/enter ⮑',
  inputPlaceholderDisabled: 'Choose an agent first',
  footerAgents: 'Assistants / Agents',
  footerNewChat: 'New Chat',
  footerRecall: 'Recall Conversation',
  footerDebug: 'Debug Panel',
  debugTitle: 'Run history',
  debugEmpty: 'No runs yet for this conversation.',
  assistantsPlaceholder: 'No assistants yet.',
  approvalPending:
    'This conversation has a pending approval — decide it in the classic UI.',
} as const;
