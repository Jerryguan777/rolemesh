// ApprovalCard — one HITL tool-approval request in the chat stream
// (spec O.3; behavioral reference web/src/components/approval-card.ts,
// visuals the v16 design-language card). The rich decision surface:
// tool identity, raw params, optional rationale, live countdown,
// Reject-with-note / Approve. After resolution the card stays in place
// forever — the user's scrollable record (§3.6) — with the three
// automatic compactions (tight padding, params → first 2, rationale
// 2-line clamp).
//
// Params: pending collapses only past the Lit threshold (>8 shown as
// 6 + "Show all N" — spec O.3's "ALL entries" was corrected to the
// shipped behavior; the disclosure keeps full info one click away).
// Values NEVER inline-truncate a primitive — a truncated amount is
// dangerous. Rationale: soft 400-char truncate while pending (Lit),
// 2-line clamp when resolved.
//
// The card never sends the approver identity — decisions relay only
// {id, verb, note} (stamped server-side from the WS ticket). Buttons
// stay busy until the `event.approval.resolved` echo lands; the
// countdown hitting zero changes NOTHING until the server's `expired`
// push (the SPA never self-decides).

import { useEffect, useRef, useState } from 'react';
import { Shield } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import type { ApprovalCard as CardData } from '../../../lib/approval-cards';
import { URGENT_MS } from '../../../lib/approval-format';
import { checkLabel } from '../../../lib/safety-catalog';
import { relativeTime } from '../../../lib/relative-time';
import { RejectNoteForm } from './reject-note-form';
import { useNowTick } from './use-countdown';

/** Lit thresholds (§3.3/§3.4). */
const PARAMS_COLLAPSE_THRESHOLD = 8;
const PARAMS_COLLAPSED_COUNT = 6;
/** Resolved compaction (§3.6): params collapse to the first 2. */
const PARAMS_RESOLVED_COUNT = 2;
const RATIONALE_TRUNCATE = 400;

const RESOLVED_LABEL: Record<string, string> = {
  approved: 'Approved',
  rejected: 'Rejected',
  expired: 'Timed out',
  cancelled: 'Cancelled',
};

function paramDisplay(v: unknown): string {
  if (v === null || v === undefined) return 'null';
  if (typeof v === 'boolean' || typeof v === 'number') return String(v);
  if (typeof v === 'string') return `"${v}"`;
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

function atTime(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

/** Live countdown — mounted only while pending so the shared 1 Hz
 *  ticker stops once every visible card is resolved. Display-only. */
function Countdown({ expiresAt }: { expiresAt: string | null }) {
  const now = useNowTick();
  if (!expiresAt) return null;
  const exp = Date.parse(expiresAt);
  if (Number.isNaN(exp)) return null;
  const ms = exp - now;
  const totalSec = Math.max(0, Math.floor(ms / 1000));
  const text =
    ms <= 0 ? 'expired' : totalSec < 60 ? `${totalSec}s left` : `${Math.floor(totalSec / 60)}m left`;
  const urgent = ms < URGENT_MS;
  return (
    <span
      className={`cd${urgent ? ' urgent' : ''}`}
      data-testid="approval-countdown"
      data-urgent={urgent ? 'true' : 'false'}
    >
      {text}
    </span>
  );
}

export function ApprovalCard({
  card,
  busy,
  coworkerName,
  pendingOthers,
  highlighted,
  now,
  onDecide,
  onBackToInbox,
  onClearHighlight,
  onSimulateTimeout,
}: {
  card: CardData;
  busy: boolean;
  coworkerName: string | null;
  /** Other approvals still pending — drives the resolved header's
   *  `← Back to inbox · N more` link. */
  pendingOthers: number;
  highlighted: boolean;
  /** Coarse clock (the list's 30 s tick) for the meta relative time. */
  now: number;
  onDecide: (decision: 'approve' | 'reject', note?: string) => void;
  onBackToInbox: () => void;
  onClearHighlight: () => void;
  /** DEV-only demo affordance; undefined in production builds. */
  onSimulateTimeout?: () => void;
}) {
  const navigate = useNavigate();
  const resolved = card.status !== 'pending';

  const [rejecting, setRejecting] = useState(false);
  const [paramsExpanded, setParamsExpanded] = useState(false);
  const [rationaleExpanded, setRationaleExpanded] = useState(false);

  // §3.6 compaction: the terminal flip resets both disclosures (the
  // reject form dies with the buttons).
  const prevStatus = useRef(card.status);
  useEffect(() => {
    if (prevStatus.current === 'pending' && card.status !== 'pending') {
      setParamsExpanded(false);
      setRationaleExpanded(false);
      setRejecting(false);
    }
    prevStatus.current = card.status;
  }, [card.status]);

  // Inbox jump / see-decision landing: pulse the halo and auto-expand
  // the compactions — the reader asked for full context (§3.6.7).
  const rootRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!highlighted) return;
    setParamsExpanded(true);
    setRationaleExpanded(true);
    rootRef.current?.scrollIntoView({ block: 'center' });
    const t = setTimeout(onClearHighlight, 1900);
    return () => clearTimeout(t);
  }, [highlighted, onClearHighlight]);

  const canAct = !busy && card.status === 'pending';

  // --- params ---
  const entries = Object.entries(card.params ?? {});
  const over = resolved
    ? entries.length > PARAMS_RESOLVED_COUNT
    : entries.length > PARAMS_COLLAPSE_THRESHOLD;
  const shown =
    over && !paramsExpanded
      ? entries.slice(0, resolved ? PARAMS_RESOLVED_COUNT : PARAMS_COLLAPSED_COUNT)
      : entries;

  // --- rationale ---
  const rationale = card.rationale?.trim() || null;
  const rationaleLong = !!rationale && rationale.length > RATIONALE_TRUNCATE;
  const rationaleBody =
    rationale && !resolved && rationaleLong && !rationaleExpanded
      ? `${rationale.slice(0, RATIONALE_TRUNCATE)}…`
      : rationale;

  const metaParts: string[] = [];
  if (coworkerName) metaParts.push(`${coworkerName} coworker`);
  if (card.requestedAt) metaParts.push(relativeTime(card.requestedAt, now));

  const tb = card.triggeredBy;
  const safety = tb && tb.kind === 'safety_rule' ? tb : null;

  return (
    <div
      ref={rootRef}
      className={`apr-card${resolved ? ` resolved ${card.status}` : ''}${highlighted ? ' highlight' : ''}`}
      data-testid="approval-card"
      data-appr-id={card.requestId}
    >
      <div className="apr-head" data-testid="approval-header">
        {resolved ? (
          <>
            <span className="st" data-testid="approval-status">
              {RESOLVED_LABEL[card.status]}
            </span>
            {card.resolvedAt != null ? (
              <span className="at" data-testid="approval-resolved-time">
                at {atTime(card.resolvedAt)}
              </span>
            ) : null}
            {pendingOthers > 0 ? (
              <>
                <span className="sp" />
                <button className="apr-back" data-testid="approval-back-to-inbox" onClick={onBackToInbox}>
                  ← Back to inbox · {pendingOthers} more
                </button>
              </>
            ) : null}
          </>
        ) : (
          <>
            <span className="st" data-testid="approval-status">
              Approval needed
            </span>
            <span>{card.toolName ?? 'A tool'} is waiting on you</span>
            <span className="sp" />
            <Countdown expiresAt={card.expiresAt} />
          </>
        )}
      </div>
      <div className="apr-bd">
        {metaParts.length ? (
          <div className="apr-meta" data-testid="approval-meta">
            {metaParts.join(' · ')}
          </div>
        ) : null}

        {safety ? (
          <div className="apr-safety" data-testid="approval-safety-banner">
            <Shield size={14} style={{ flexShrink: 0, alignSelf: 'center' }} />
            <span>
              Paused by a safety rule — <b>{checkLabel(safety.check_id)}</b>
            </span>
            <a
              data-testid="approval-safety-link"
              onClick={() =>
                navigate(
                  `/manage/safety-log?rule_id=${encodeURIComponent(safety.rule_id)}`,
                )
              }
            >
              view in safety log →
            </a>
          </div>
        ) : null}

        {card.mcpServerName || card.toolName ? (
          <div className="apr-tool" data-testid="approval-tool">
            {[card.mcpServerName, card.toolName].filter(Boolean).join(' · ')}
          </div>
        ) : null}

        {entries.length ? (
          <div className="apr-params" data-testid="approval-params">
            {shown.map(([k, v]) => (
              <div className="prow" key={k} data-testid="approval-param-row">
                <span className="pk">{k}</span>
                <span className="pv">{paramDisplay(v)}</span>
              </div>
            ))}
            {over ? (
              <button
                className="pmore"
                data-testid="approval-params-toggle"
                onClick={() => setParamsExpanded((x) => !x)}
              >
                {paramsExpanded ? '▾ Show fewer' : `Show all ${entries.length} params ▾`}
              </button>
            ) : null}
          </div>
        ) : null}

        {rationale ? (
          <div
            className={`rationale${resolved && !rationaleExpanded ? ' clamp' : ''}`}
            data-testid="approval-rationale"
          >
            <div className="cap">WHY</div>
            <div className="rl-text">{rationaleBody}</div>
            {resolved ? (
              <button
                className="rl-more"
                data-testid="approval-rationale-toggle"
                onClick={() => setRationaleExpanded(true)}
              >
                more ▾
              </button>
            ) : rationaleLong ? (
              <button
                className="rl-more"
                style={{ display: 'inline' }}
                data-testid="approval-rationale-toggle"
                onClick={() => setRationaleExpanded((x) => !x)}
              >
                {rationaleExpanded ? 'less' : 'more'}
              </button>
            ) : null}
          </div>
        ) : null}

        {card.status === 'rejected' && card.note ? (
          <div className="apr-note" data-testid="approval-resolved-note">
            <div className="cap">YOUR REASON</div>
            <div className="rl-text">{card.note}</div>
          </div>
        ) : null}

        {!resolved ? (
          rejecting ? (
            <RejectNoteForm
              busy={busy}
              onCancel={() => setRejecting(false)}
              onReject={(note) => {
                if (!canAct) return;
                onDecide('reject', note);
              }}
            />
          ) : (
            <div className="apr-acts">
              {onSimulateTimeout ? (
                <button className="demo" data-testid="approval-demo-timeout" onClick={onSimulateTimeout}>
                  [demo] simulate timeout
                </button>
              ) : null}
              <button
                className="btn-ghost"
                data-testid="approval-reject"
                disabled={busy}
                onClick={() => {
                  if (canAct) setRejecting(true);
                }}
              >
                Reject
              </button>
              <button
                className="btn-primary"
                data-testid="approval-approve"
                disabled={busy}
                onClick={() => {
                  if (canAct) onDecide('approve');
                }}
              >
                {busy ? '…' : 'Approve'}
              </button>
            </div>
          )
        ) : null}
      </div>
    </div>
  );
}
