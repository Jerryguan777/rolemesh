// MessageList — scroll region + timestamp rail (spec §6.3). Renders
// the REST history followed by the live stream rows (optimistic
// sends, finalized turns, inline error rows), the in-progress draft,
// and the approval degradation notice. Relative times refresh on a
// single 30s interval for the whole list; auto-scroll sticks to the
// bottom unless the user has scrolled up (>80px from bottom).

import { useEffect, useRef, useState } from 'react';
import type { Message } from '../../api/client';
import { Markdown } from '../../components/markdown';
import { BrandMark } from '../../components/brand-mark';
import { relativeTime } from '../../lib/relative-time';
import { COPY } from '../../app/copy';
import { MessageItem, TimelineItem } from './message-item';
import type { StreamRow } from './use-conversation-stream';

const SCROLL_STICK_PX = 80;

export function MessageList({
  history,
  liveRows,
  draft,
  pendingApprovals,
  initials,
}: {
  history: readonly Message[];
  liveRows: readonly StreamRow[];
  draft: string | null;
  pendingApprovals: readonly string[];
  initials: string;
}) {
  // 30s tick so relative labels stay fresh without per-row timers.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  const listRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);

  const rowCount = history.length + liveRows.length;
  useEffect(() => {
    const el = listRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [rowCount, draft, pendingApprovals.length]);

  function onScroll() {
    const el = listRef.current;
    if (!el) return;
    stickRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight <= SCROLL_STICK_PX;
  }

  return (
    <div className="msg-list" ref={listRef} onScroll={onScroll}>
      {history.map((m) => (
        <MessageItem
          key={m.id}
          role={m.role}
          content={m.content}
          tsLabel={relativeTime(m.timestamp, now)}
          initials={initials}
        />
      ))}
      {liveRows.map((row, i) =>
        row.kind === 'message' ? (
          <MessageItem
            key={`live-${i}`}
            role={row.role}
            content={row.content}
            tsLabel={relativeTime(row.timestamp, now)}
            initials={initials}
          />
        ) : (
          <TimelineItem key={`live-${i}`} tsLabel={relativeTime(row.timestamp, now)}>
            <div className="error-row">
              <span className="code">{row.code}</span> — {row.message}
            </div>
          </TimelineItem>
        ),
      )}
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
      {pendingApprovals.length > 0 ? (
        <TimelineItem tsLabel="pending">
          <div className="approval-notice" role="status">
            {COPY.approvalPending}
          </div>
        </TimelineItem>
      ) : null}
    </div>
  );
}
