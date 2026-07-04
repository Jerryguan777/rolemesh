// ApprovalsInbox — top-bar icon + badge + triage popover (spec O.4;
// behavioral reference web/src/components/approvals-inbox.ts).
// TRIAGE-ONLY: a row navigates to the chat where the decision card
// lives — the inbox never decides (a second decision surface would
// mean a second state shape, validation, and audit trail, and would
// strip the context that makes params + rationale meaningful).
//
// Data: the store's tenant-wide `inboxRows` (REST-fed — the per-
// conversation WS can't see other chats' approvals). Refresh triggers
// mirror the Lit set: popover open, active-conversation switch, tab
// visibility, WS approval activity (the store does that one), plus a
// 30 s slow poll WHILE OPEN only. No standing timer while closed —
// the badge is event-driven, not polled.

import { useEffect, useRef } from 'react';
import { Inbox, Shield } from 'lucide-react';
import type { ApprovalRequest, Coworker, Conversation } from '../../../api/client';
import {
  formatCountdown,
  isUrgent,
  paramsInline,
} from '../../../lib/approval-format';
import { checkLabel } from '../../../lib/safety-catalog';
import { useApprovalStore } from '../../../stores/approval-store';
import { useNowTick } from './use-countdown';

const POLL_MS = 30_000;

function coworkerName(coworkers: readonly Coworker[], id: string | null): string {
  if (!id) return 'A';
  return coworkers.find((c) => c.id === id)?.name ?? 'A';
}

/** The open popover's row list — separate component so useNowTick's
 *  1 Hz ticker runs only while the popover is visible. */
function InboxRows({
  rows,
  coworkers,
  conversations,
  onJump,
  onSimulate,
}: {
  rows: readonly ApprovalRequest[];
  coworkers: readonly Coworker[];
  conversations: readonly Conversation[];
  onJump: (row: ApprovalRequest) => void;
  onSimulate?: () => void;
}) {
  const now = useNowTick();
  const sorted = [...rows].sort(
    (a, b) => Date.parse(a.expires_at) - Date.parse(b.expires_at),
  );
  const soon = sorted.filter((r) => isUrgent(r.expires_at, now)).length;

  return (
    <div className="inbox-pop" role="dialog" aria-label="Approvals" data-testid="inbox-pop">
      <div className="inbox-h">
        Approvals · {sorted.length} to review
        {soon ? ` · ${soon} expiring soon` : ''}
      </div>
      {sorted.length ? (
        sorted.map((r) => {
          const convTitle = conversations.find((c) => c.id === r.conversation_id)?.name;
          const inline = paramsInline(r.params);
          const urgent = isUrgent(r.expires_at, now);
          const tb = r.triggered_by;
          return (
            <div
              className="inbox-row"
              key={r.request_id}
              data-testid="inbox-row"
              data-urgent={urgent ? 'true' : 'false'}
              onClick={() => onJump(r)}
            >
              <div className="r1">
                {coworkerName(coworkers, r.coworker_id ?? null)} coworker{' '}
                <span className="apr-tool" style={{ margin: 0 }}>
                  {r.mcp_server_name}.{r.tool_name}
                </span>
                {tb && tb.kind === 'safety_rule' ? (
                  <span title={checkLabel(tb.check_id)} data-testid="inbox-shield">
                    <Shield size={13} />
                  </span>
                ) : null}
              </div>
              <div className="r2">
                {convTitle ? <>“{convTitle}” · </> : null}
                <span className={urgent ? 'urgent' : ''}>
                  {formatCountdown(r.expires_at, now)}
                </span>
              </div>
              {inline ? <div className="r3">{inline}</div> : null}
              <div style={{ textAlign: 'right', marginTop: 4 }}>
                <button className="btn-ghost" style={{ fontSize: 12, padding: '2px 8px' }}>
                  Open in chat →
                </button>
              </div>
            </div>
          );
        })
      ) : (
        <div
          className="inbox-row"
          style={{ cursor: 'default', color: 'var(--rm-text-muted)' }}
          data-testid="inbox-empty"
        >
          Nothing waiting on you.
        </div>
      )}
      <div className="inbox-f">
        Updates live
        {onSimulate ? (
          <>
            {' · '}
            <button data-testid="inbox-simulate" onClick={onSimulate}>
              + simulate one
            </button>
          </>
        ) : null}
      </div>
    </div>
  );
}

export function ApprovalsInbox({
  coworkers,
  conversations,
  activeChatId,
  onJump,
  onSimulate,
}: {
  coworkers: readonly Coworker[];
  conversations: readonly Conversation[];
  activeChatId: string | null;
  /** Close-jump-highlight: the page switches agent+chat, the card
   *  pulses via the store's highlightId. */
  onJump: (row: ApprovalRequest) => void;
  /** DEV-only demo affordance; undefined in production builds. */
  onSimulate?: () => void;
}) {
  const rows = useApprovalStore((s) => s.inboxRows);
  const open = useApprovalStore((s) => s.inboxOpen);
  const setInboxOpen = useApprovalStore((s) => s.setInboxOpen);
  const refreshInbox = useApprovalStore((s) => s.refreshInbox);

  // Trigger set — mount (badge seed), conversation switch, visibility.
  useEffect(() => {
    void refreshInbox();
  }, [refreshInbox, activeChatId]);
  useEffect(() => {
    function onVis() {
      if (document.visibilityState === 'visible') void refreshInbox();
    }
    document.addEventListener('visibilitychange', onVis);
    return () => document.removeEventListener('visibilitychange', onVis);
  }, [refreshInbox]);

  // 30 s backstop poll while open only.
  useEffect(() => {
    if (!open) return;
    const t = setInterval(() => void refreshInbox(), POLL_MS);
    return () => clearInterval(t);
  }, [open, refreshInbox]);

  // Click-away closes. Attached a tick late: the click that OPENED the
  // popover (e.g. the resolved card's back-to-inbox link, outside this
  // subtree) is still bubbling to document when the effect runs — an
  // immediate listener would close it in the same gesture.
  const rootRef = useRef<HTMLSpanElement | null>(null);
  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setInboxOpen(false);
    }
    const t = setTimeout(() => document.addEventListener('click', onDoc), 0);
    return () => {
      clearTimeout(t);
      document.removeEventListener('click', onDoc);
    };
  }, [open, setInboxOpen]);

  const urgent = rows.some((r) => isUrgent(r.expires_at, Date.now()));

  return (
    <span className="inbox-btn" ref={rootRef}>
      <button
        className="icon-btn"
        title="Approvals inbox"
        aria-haspopup="true"
        data-testid="inbox-btn"
        onClick={() => setInboxOpen(!open)}
      >
        <Inbox />
        {rows.length ? (
          <span className="inbox-badge" data-testid="inbox-badge" data-urgent={urgent ? 'true' : 'false'}>
            {rows.length}
          </span>
        ) : null}
      </button>
      {open ? (
        <InboxRows
          rows={rows}
          coworkers={coworkers}
          conversations={conversations}
          onJump={(r) => {
            setInboxOpen(false);
            onJump(r);
          }}
          onSimulate={onSimulate}
        />
      ) : null}
    </span>
  );
}
