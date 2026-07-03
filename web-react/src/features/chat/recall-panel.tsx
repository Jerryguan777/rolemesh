// RecallPanel — the conversation-history surface (spec §5.2): a right
// aside listing the ACTIVE agent's conversations, newest first by
// created_at (the wire Conversation has no updated_at). This is where
// "history is bound to the agent" becomes visible: switching agents
// re-scopes the list in place.
//
// Preview source: the wire carries no preview — rows paint
// immediately with `conversation.name` (or a placeholder) and each
// missing preview is filled asynchronously from the conversation's
// first user message WITHOUT awaiting (the Lit loadConversationPreviews
// pattern). An explicit `name` is never clobbered. Fetches are capped
// to the newest rows so a 200-conversation agent doesn't fan out 200
// message reads on open.

import { useEffect, useRef, useState } from 'react';
import { ArrowRight, X } from 'lucide-react';
import type { Conversation, Coworker } from '../../api/client';
import { getApiClient } from '../../api/client';
import {
  previewFromMessages,
  sortNewestFirst,
  summaryFromConversation,
} from '../../lib/conversation-summary';
import { relativeTime } from '../../lib/relative-time';
import { COPY } from '../../app/copy';

const PREVIEW_FETCH_CAP = 30;

export function RecallPanel({
  agent,
  conversations,
  activeChatId,
  onOpenConversation,
  onClose,
}: {
  agent: Coworker | null;
  conversations: readonly Conversation[];
  activeChatId: string | null;
  onOpenConversation: (chatId: string) => void;
  onClose: () => void;
}) {
  // Preview cache keyed by conversation id. A ref-backed Map survives
  // re-renders and agent switches without refetching what we have; the
  // state counter just triggers paint as previews land.
  const previewsRef = useRef(new Map<string, string>());
  const [, setPreviewTick] = useState(0);

  const items = sortNewestFirst(conversations.map(summaryFromConversation));

  useEffect(() => {
    let cancelled = false;
    const missing = items
      .filter((it) => !it.preview && !previewsRef.current.has(it.chatId))
      .slice(0, PREVIEW_FETCH_CAP);
    for (const it of missing) {
      // Deliberately not awaited: paint first, fill previews as they
      // land. A failure leaves the placeholder — never an error state.
      void getApiClient()
        .listMessages(it.chatId)
        .then((messages) => {
          if (cancelled) return;
          const preview = previewFromMessages(messages);
          previewsRef.current.set(it.chatId, preview ?? '');
          setPreviewTick((n) => n + 1);
        })
        .catch(() => {
          if (!cancelled) previewsRef.current.set(it.chatId, '');
        });
    }
    return () => {
      cancelled = true;
    };
    // items identity changes every render; key the effect by the ids.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items.map((i) => i.chatId).join(','), agent?.id]);

  return (
    <aside className="aside-panel recall" aria-label="Recall conversations">
      <div className="aside-header">
        <div>
          <h3 className="aside-title">{COPY.recallTitle}</h3>
          <div className="aside-sub">{agent?.name ?? ''}</div>
        </div>
        <button className="icon-btn" aria-label="Close" onClick={onClose}>
          <X />
        </button>
      </div>
      <div className="aside-body">
        {!agent ? (
          <p className="aside-empty">{COPY.recallNoAgent}</p>
        ) : items.length === 0 ? (
          <p className="aside-empty">{COPY.recallNoConversations}</p>
        ) : (
          <div className="conv-list">
            {items.map((it) => {
              const preview =
                it.preview ?? previewsRef.current.get(it.chatId) ?? null;
              return (
                <div
                  key={it.chatId}
                  className={`conv-item${it.chatId === activeChatId ? ' active' : ''}`}
                  onClick={() => onOpenConversation(it.chatId)}
                >
                  <span className="cts">{relativeTime(it.createdAt)}</span>
                  <span className={`preview${preview ? '' : ' placeholder'}`}>
                    {preview || 'New conversation'}
                  </span>
                  <button
                    className="continue"
                    onClick={(e) => {
                      e.stopPropagation();
                      onOpenConversation(it.chatId);
                    }}
                  >
                    Continue
                    <ArrowRight />
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </aside>
  );
}
