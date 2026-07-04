// MessageList — scroll region + timestamp rail (spec §6.3). Renders
// the REST history, the live stream rows, and the conversation's
// approval cards INTERLEAVED by timestamp (§3.8): messages sort on
// their server timestamp, cards on orderTs (client arrival for live
// pushes, server requested_at on reload) — so a card sits between the
// user message that triggered it and the confirmation that followed.
// Resolved cards render in place forever (the audit record); the §0.6
// degraded-approvals notice this replaced is retired.
//
// Relative times refresh on a single 30s interval for the whole list;
// auto-scroll sticks to the bottom unless the user has scrolled up.

import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { Message } from '../../api/client';
import { Markdown } from '../../components/markdown';
import { BrandMark } from '../../components/brand-mark';
import { relativeTime } from '../../lib/relative-time';
import { useApprovalStore } from '../../stores/approval-store';
import { ApprovalCard } from './approvals/approval-card';
import { MessageItem, TimelineItem } from './message-item';
import type { StreamRow } from './use-conversation-stream';

const SCROLL_STICK_PX = 80;

interface Entry {
  ts: number;
  key: string;
  node: ReactNode;
}

export function MessageList({
  history,
  liveRows,
  draft,
  initials,
  agentName,
  onSimulateTimeout,
}: {
  history: readonly Message[];
  liveRows: readonly StreamRow[];
  draft: string | null;
  initials: string;
  /** Active coworker's display name — the card meta line. */
  agentName: string | null;
  /** DEV-only demo affordance passthrough (per-request id). */
  onSimulateTimeout?: (requestId: string) => void;
}) {
  // 30s tick so relative labels stay fresh without per-row timers.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  const cards = useApprovalStore((s) => s.cards);
  const busyIds = useApprovalStore((s) => s.busyIds);
  const highlightId = useApprovalStore((s) => s.highlightId);
  const decide = useApprovalStore((s) => s.decide);
  const setHighlight = useApprovalStore((s) => s.setHighlight);
  const setInboxOpen = useApprovalStore((s) => s.setInboxOpen);

  const listRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);

  const rowCount = history.length + liveRows.length + cards.length;
  useEffect(() => {
    const el = listRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [rowCount, draft]);

  function onScroll() {
    const el = listRef.current;
    if (!el) return;
    stickRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight <= SCROLL_STICK_PX;
  }

  const pendingCount = cards.filter((c) => c.status === 'pending').length;

  const entries: Entry[] = [
    ...history.map((m): Entry => ({
      ts: m.timestamp ? Date.parse(m.timestamp) : 0,
      key: m.id,
      node: (
        <MessageItem
          role={m.role}
          content={m.content}
          tsLabel={relativeTime(m.timestamp, now)}
          initials={initials}
        />
      ),
    })),
    ...liveRows.map((row, i): Entry => ({
      ts: Date.parse(row.timestamp) || Number.MAX_SAFE_INTEGER,
      key: `live-${i}`,
      node:
        row.kind === 'message' ? (
          <MessageItem
            role={row.role}
            content={row.content}
            tsLabel={relativeTime(row.timestamp, now)}
            initials={initials}
          />
        ) : (
          <TimelineItem tsLabel={relativeTime(row.timestamp, now)}>
            <div className="error-row">
              <span className="code">{row.code}</span> — {row.message}
            </div>
          </TimelineItem>
        ),
    })),
    ...cards.map((c): Entry => ({
      ts: c.orderTs,
      key: c.requestId,
      node: (
        <ApprovalCard
          card={c}
          busy={!!busyIds[c.requestId]}
          coworkerName={agentName}
          pendingOthers={
            c.status === 'pending' ? pendingCount - 1 : pendingCount
          }
          highlighted={highlightId === c.requestId}
          now={now}
          onDecide={(decision, note) => decide(c.requestId, decision, note)}
          onBackToInbox={() => setInboxOpen(true)}
          onClearHighlight={() => setHighlight(null)}
          onSimulateTimeout={
            onSimulateTimeout ? () => onSimulateTimeout(c.requestId) : undefined
          }
        />
      ),
    })),
  ].sort((a, b) => a.ts - b.ts);

  return (
    <div className="msg-list" ref={listRef} onScroll={onScroll}>
      {entries.map((e) => (
        <div key={e.key}>{e.node}</div>
      ))}
      {draft !== null ? (
        <TimelineItem tsLabel="just now">
          <div className="msg-row start">
            <div className="avatar assistant">
              <BrandMark size="100%" />
            </div>
            <div className="bubble assistant">
              <div className="row-line">
                <Markdown text={draft} />
              </div>
            </div>
          </div>
        </TimelineItem>
      ) : null}
    </div>
  );
}
