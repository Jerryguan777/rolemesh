// useConversationStream — owns one V1WsClient per active conversation
// and reduces its event stream into UI state (spec §6.4 / §10.2).
//
// Live rows (optimistic user sends, finalized assistant turns, inline
// error rows) are kept SEPARATE from the REST message history: the
// chat page renders history + live rows in order. On a genuine
// reconnect the hook re-hydrates truth (§6.2 step 4): it invalidates
// the messages query and clears the live buffer — everything the
// server accepted is in the refetch, so nothing is lost or doubled.
//
// The approval degradation notice (spec §0.6, mandatory in this
// branch) is driven by pendingApprovals: `event.approval.requested`
// adds, `event.approval.resolved` removes. Cards arrive next PR.

import { useCallback, useEffect, useReducer, useRef } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { getStoredToken } from '../../lib/oidc-auth';
import {
  V1WsClient,
  type ConnectionStatus,
  type ServerEvent,
} from '../../ws/v1_client';

export interface LiveMessage {
  kind: 'message';
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
}

export interface ErrorRow {
  kind: 'error';
  code: string;
  message: string;
  timestamp: string;
}

export type StreamRow = LiveMessage | ErrorRow;

interface StreamState {
  rows: StreamRow[];
  /** In-progress assistant text; null when no run is streaming. */
  draft: string | null;
  runActive: boolean;
  /** event.run.progress label ("running", "tool: web_search", …). */
  progress: string | null;
  pendingApprovals: string[];
  status: ConnectionStatus;
}

const INITIAL: StreamState = {
  rows: [],
  draft: null,
  runActive: false,
  progress: null,
  pendingApprovals: [],
  status: 'idle',
};

type Action =
  | { type: 'reset' }
  | { type: 'status'; status: ConnectionStatus }
  | { type: 'user-send'; content: string }
  | { type: 'clear-live' }
  | { type: 'event'; event: ServerEvent };

function now(): string {
  return new Date().toISOString();
}

/** Fold a possibly-non-empty draft into the rows as a finalized
 *  assistant message. */
function finalizeDraft(state: StreamState): StreamRow[] {
  if (!state.draft) return state.rows;
  return [
    ...state.rows,
    { kind: 'message', role: 'assistant', content: state.draft, timestamp: now() },
  ];
}

function reduce(state: StreamState, action: Action): StreamState {
  switch (action.type) {
    case 'reset':
      return INITIAL;
    case 'status':
      return { ...state, status: action.status };
    case 'user-send':
      return {
        ...state,
        rows: [
          ...state.rows,
          { kind: 'message', role: 'user', content: action.content, timestamp: now() },
        ],
        runActive: true,
        progress: null,
      };
    case 'clear-live':
      return { ...state, rows: [] };
    case 'event': {
      const ev = action.event;
      switch (ev.type) {
        case 'event.run.started':
          return { ...state, runActive: true, draft: state.draft ?? '', progress: null };
        case 'event.run.token':
          return { ...state, draft: (state.draft ?? '') + ev.delta };
        case 'event.run.progress':
          // Tolerated at minimum (spec §10.2); we surface it in the
          // status bar. Unknown statuses fall through as raw labels.
          return {
            ...state,
            progress:
              ev.status === 'tool_use' && ev.tool ? `tool: ${ev.tool}` : ev.status,
          };
        case 'event.run.completed':
          return {
            ...state,
            rows: finalizeDraft(state),
            draft: null,
            runActive: false,
            progress: null,
          };
        case 'event.run.error':
          return {
            ...state,
            rows: [
              ...finalizeDraft(state),
              { kind: 'error', code: ev.code, message: ev.message, timestamp: now() },
            ],
            draft: null,
            runActive: false,
            progress: null,
          };
        case 'event.approval.requested':
          return state.pendingApprovals.includes(ev.request_id)
            ? state
            : { ...state, pendingApprovals: [...state.pendingApprovals, ev.request_id] };
        case 'event.approval.resolved':
          return {
            ...state,
            pendingApprovals: state.pendingApprovals.filter(
              (id) => id !== ev.request_id,
            ),
          };
        default:
          // Delegation chips et al. — tolerated, rendered next PR.
          return state;
      }
    }
  }
}

export function useConversationStream(chatId: string | null) {
  const [state, dispatch] = useReducer(reduce, INITIAL);
  const clientRef = useRef<V1WsClient | null>(null);
  const queryClient = useQueryClient();

  useEffect(() => {
    dispatch({ type: 'reset' });
    if (!chatId) return;
    const client = new V1WsClient({
      conversationId: chatId,
      getToken: getStoredToken,
    });
    clientRef.current = client;
    const offEvent = client.onEvent('*', (event) =>
      dispatch({ type: 'event', event }),
    );
    let everOpen = false;
    const offStatus = client.onStatus((status) => {
      dispatch({ type: 'status', status });
      if (status === 'open') {
        if (everOpen) {
          // Reconnect → re-hydrate truth from REST and drop the live
          // buffer (the refetch supersedes it).
          void queryClient.invalidateQueries({ queryKey: ['messages', chatId] });
          dispatch({ type: 'clear-live' });
        }
        everOpen = true;
      }
    });
    void client.connect();
    return () => {
      offEvent();
      offStatus();
      client.disconnect();
      clientRef.current = null;
    };
  }, [chatId, queryClient]);

  const send = useCallback((text: string) => {
    const client = clientRef.current;
    if (!client) return;
    dispatch({ type: 'user-send', content: text });
    client.send(text);
  }, []);

  const stop = useCallback(() => {
    clientRef.current?.stop();
  }, []);

  return { ...state, send, stop };
}
