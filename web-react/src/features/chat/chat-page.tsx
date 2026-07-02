// ChatPage — owns the bootstrap flow (spec §6.2) and composes the
// chat surface: MessageList + StatusBar + MessageInput + the right
// asides + the agent picker.
//
// States (spec §6.1): no `agent_id` → no-agent empty state (the only
// CTA opens the picker — unlike the Lit app we do NOT default to the
// first coworker); agent set but chat unresolved → bootstrapping
// skeleton; resolved with 0 messages → per-agent greeting; else the
// message list.
//
// Bootstrap: with an agent and no `chat_id`, pick the coworker's most
// recent conversation (created_at sort) or POST a fresh one, then
// replaceState the resolved chat_id into the URL BEFORE the message
// surface mounts — the WS client reads chat_id on first paint (same
// ordering constraint chat-shell.ts documents).

import { useEffect, useMemo, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { getApiClient, type Coworker } from '../../api/client';
import { useConversations, useCoworkers, useMessages, useModels } from '../../api/queries';
import { currentMe } from '../../lib/capabilities';
import { modelsByIdMap } from '../../lib/coworker-label';
import { COPY } from '../../app/copy';
import { AgentPickerModal } from './agent-picker/agent-picker-modal';
import { EmptyState } from './empty-state';
import { MessageInput } from './message-input';
import { MessageList } from './message-list';
import { RecallPanel } from './recall-panel';
import { RunHistoryAside } from './run-history-aside';
import { StatusBar } from './status-bar';
import { useChatParams } from './use-chat-params';
import { useConversationStream } from './use-conversation-stream';
import './chat.css';

type Aside = 'recall' | 'debug' | null;

function initialsOf(name: string | null | undefined): string {
  if (!name) return '?';
  const words = name.trim().split(/\s+/);
  const letters = words.slice(0, 2).map((w) => w[0] ?? '');
  return (letters.join('') || name.slice(0, 2)).slice(0, 2);
}

export function ChatPage() {
  const { agentId, chatId, setParams } = useChatParams();
  const queryClient = useQueryClient();

  const coworkersQ = useCoworkers();
  const modelsQ = useModels();
  const conversationsQ = useConversations(agentId);
  const messagesQ = useMessages(chatId);
  const stream = useConversationStream(chatId);

  const [aside, setAside] = useState<Aside>(null);
  const [pickerOpen, setPickerOpen] = useState(false);

  const activeAgent: Coworker | null = useMemo(() => {
    if (!agentId || !coworkersQ.data) return null;
    return coworkersQ.data.find((c) => c.id === agentId) ?? null;
  }, [agentId, coworkersQ.data]);

  const modelsById = useMemo(
    () => modelsByIdMap(modelsQ.data ?? []),
    [modelsQ.data],
  );

  // --- bootstrap: resolve a default chat_id for the active agent ---
  const bootRef = useRef<string | null>(null);
  useEffect(() => {
    if (!agentId || chatId) return;
    if (!activeAgent || !conversationsQ.data) return;
    if (bootRef.current === agentId) return;
    bootRef.current = agentId;
    void (async () => {
      try {
        const sorted = [...conversationsQ.data].sort((a, b) =>
          a.created_at < b.created_at ? 1 : -1,
        );
        let id = sorted[0]?.id;
        if (!id) {
          const fresh = await getApiClient().createCoworkerConversation(agentId);
          id = fresh.id;
          await queryClient.invalidateQueries({ queryKey: ['conversations', agentId] });
        }
        // replaceState (not push) — the resolved chat_id must not add a
        // dead intermediate history entry.
        setParams({ chatId: id }, { replace: true });
      } catch (err) {
        console.warn('chat bootstrap: conversation resolve failed', err);
      } finally {
        bootRef.current = null;
      }
    })();
  }, [agentId, chatId, activeAgent, conversationsQ.data, queryClient, setParams]);

  // ESC: close the modal first, then the open aside (spec §5.2).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return;
      if (pickerOpen) setPickerOpen(false);
      else setAside(null);
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [pickerOpen]);

  function selectAgent(id: string) {
    setPickerOpen(false);
    if (id === agentId) return;
    setParams({ agentId: id, chatId: null });
  }

  async function newChat() {
    if (!activeAgent) {
      setPickerOpen(true);
      return;
    }
    try {
      const fresh = await getApiClient().createCoworkerConversation(activeAgent.id);
      await queryClient.invalidateQueries({ queryKey: ['conversations', activeAgent.id] });
      setParams({ chatId: fresh.id });
    } catch (err) {
      console.warn('new chat failed', err);
    }
  }

  // --- render state (spec §6.1) ---
  const noAgent = !agentId || (coworkersQ.data !== undefined && !activeAgent);
  const bootstrapping =
    !noAgent && (!coworkersQ.data || !chatId || messagesQ.data === undefined);
  const history = messagesQ.data ?? [];
  const isEmpty =
    !bootstrapping &&
    history.length === 0 &&
    stream.rows.length === 0 &&
    stream.draft === null;

  const initials = initialsOf(currentMe()?.name);

  return (
    <div className="chat-shell">
      <div className="chat-container">
        <div className="msg-root">
          {noAgent ? (
            <EmptyState
              info={COPY.emptyNoAgent}
              cta={{ label: COPY.emptyNoAgentCta, onClick: () => setPickerOpen(true) }}
            />
          ) : bootstrapping ? (
            // Brief skeleton — deliberately quiet; do not flash the
            // empty state while resolving (spec §6.1).
            <div className="msg-list" aria-busy="true" />
          ) : isEmpty ? (
            <EmptyState info={COPY.emptyConversation(activeAgent!.name)} />
          ) : (
            <MessageList
              history={history}
              liveRows={stream.rows}
              draft={stream.draft}
              pendingApprovals={stream.pendingApprovals}
              initials={initials}
            />
          )}
        </div>

        <div className="message-box">
          <StatusBar
            agent={activeAgent}
            runActive={stream.runActive}
            progress={stream.progress}
            wsStatus={stream.status}
            hasChat={!!chatId}
            onStop={stream.stop}
          />
          <MessageInput
            disabled={noAgent}
            onSend={stream.send}
            onOpenPicker={() => setPickerOpen(true)}
            onNewChat={() => void newChat()}
            onToggleRecall={() => setAside(aside === 'recall' ? null : 'recall')}
            onToggleDebug={() => setAside(aside === 'debug' ? null : 'debug')}
          />
        </div>
      </div>

      {aside === 'recall' ? (
        <RecallPanel
          agent={activeAgent}
          conversations={conversationsQ.data ?? []}
          activeChatId={chatId}
          onOpenConversation={(id) => setParams({ chatId: id })}
          onClose={() => setAside(null)}
        />
      ) : null}
      {aside === 'debug' ? <RunHistoryAside onClose={() => setAside(null)} /> : null}

      {pickerOpen ? (
        <AgentPickerModal
          coworkers={coworkersQ.data ?? []}
          modelsById={modelsById}
          activeAgentId={agentId}
          onSelect={selectAgent}
          onClose={() => setPickerOpen(false)}
        />
      ) : null}
    </div>
  );
}
