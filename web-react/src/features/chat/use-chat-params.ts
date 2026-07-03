// useChatParams — decision D-10: `?agent_id` / `?chat_id` live in the
// REAL location.search (not inside the hash), so deep links and
// bookmarks stay interchangeable with the Lit SPA. react-router owns
// only the hash; this hook owns the search string via history
// push/replaceState plus a custom event so all subscribers re-render.

import { useCallback, useSyncExternalStore } from 'react';

const PARAMS_EVENT = 'rm-chat-params-changed';

export interface ChatParams {
  agentId: string | null;
  chatId: string | null;
}

function readSearch(): string {
  return location.search;
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener('popstate', onChange);
  window.addEventListener(PARAMS_EVENT, onChange);
  return () => {
    window.removeEventListener('popstate', onChange);
    window.removeEventListener(PARAMS_EVENT, onChange);
  };
}

export interface SetChatParams {
  /** undefined → leave alone; null → delete; string → set. */
  agentId?: string | null;
  chatId?: string | null;
}

export function useChatParams(): ChatParams & {
  setParams: (next: SetChatParams, opts?: { replace?: boolean }) => void;
} {
  const search = useSyncExternalStore(subscribe, readSearch);
  const params = new URLSearchParams(search);

  const setParams = useCallback(
    (next: SetChatParams, opts?: { replace?: boolean }) => {
      const p = new URLSearchParams(location.search);
      if (next.agentId !== undefined) {
        if (next.agentId === null) p.delete('agent_id');
        else p.set('agent_id', next.agentId);
      }
      if (next.chatId !== undefined) {
        if (next.chatId === null) p.delete('chat_id');
        else p.set('chat_id', next.chatId);
      }
      const qs = p.toString();
      const url = `${location.pathname}${qs ? `?${qs}` : ''}${location.hash || '#/'}`;
      try {
        if (opts?.replace) history.replaceState(null, '', url);
        else history.pushState(null, '', url);
      } catch (err) {
        // Some environments refuse cross-path state writes; the missing
        // URL update is recoverable (same degradation chat-shell.ts
        // documents for its replaceState).
        console.warn('useChatParams: history state write rejected', err);
      }
      window.dispatchEvent(new Event(PARAMS_EVENT));
    },
    [],
  );

  return {
    agentId: params.get('agent_id'),
    chatId: params.get('chat_id'),
    setParams,
  };
}
