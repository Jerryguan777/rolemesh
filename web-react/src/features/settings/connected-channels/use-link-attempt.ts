// useLinkAttempt — the Telegram link-attempt state machine (spec M.4;
// behavioral reference web/src/components/connected-channels-page.ts).
//
// Lit decisions carried over verbatim:
//   - BASELINE SET: the identity-id set is captured when the user
//     clicks Connect; success fires only when an id OUTSIDE that set
//     appears. A tenant peer linking their own account — or the
//     user's pre-existing link (multi-bind is legal, decision #13) —
//     never falsely flips this UI.
//   - 409 RESOURCE_NOT_AVAILABLE from the POST is CONFIGURATION, not
//     failure — surfaced as the `noBot` flag, not `error`.
//   - Poll-round failures are swallowed (TanStack keeps the last good
//     data); a transient 5xx during the wait window must not break
//     the flow.
//   - Timers die with the component: the countdown interval is an
//     effect cleanup, the 3 s poll is the query's refetchInterval
//     (both the Lit disconnectedCallback teardown).
//
// Deviations (spec-directed, visual layer): expiry reports through
// `onExpired` (the page toasts — Lit used an inline error line), and
// `justLinked` drives the "✓ Telegram connected." confirmation panel
// Lit didn't render.

import { useEffect, useRef, useState } from 'react';
import { ApiError, getApiClient, type ChannelLinkToken } from '../../../api/client';
import { useChannelLinks } from '../../../api/queries';

function secondsUntil(iso: string): number {
  return Math.max(0, Math.round((Date.parse(iso) - Date.now()) / 1000));
}

export function useLinkAttempt(onExpired: () => void) {
  const [pending, setPending] = useState<ChannelLinkToken | null>(null);
  const [secondsLeft, setSecondsLeft] = useState(0);
  const [noBot, setNoBot] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [justLinked, setJustLinked] = useState(false);

  const linksQ = useChannelLinks(pending !== null);

  const baseline = useRef<Set<string>>(new Set());
  const onExpiredRef = useRef(onExpired);
  onExpiredRef.current = onExpired;

  // Countdown tick (1 s, Lit TICK_INTERVAL_MS). Recomputed from
  // expires_at each round so the display never drifts; hitting zero
  // drops the attempt back to idle.
  useEffect(() => {
    if (!pending) return;
    setSecondsLeft(secondsUntil(pending.expires_at));
    const t = setInterval(() => {
      const left = secondsUntil(pending.expires_at);
      setSecondsLeft(left);
      if (left === 0) {
        setPending(null);
        onExpiredRef.current();
      }
    }, 1000);
    return () => clearInterval(t);
  }, [pending]);

  // Success detection: a fresh id outside the baseline.
  useEffect(() => {
    if (!pending || !linksQ.data) return;
    if (linksQ.data.some((i) => !baseline.current.has(i.id))) {
      setPending(null);
      setJustLinked(true);
    }
  }, [pending, linksQ.data]);

  async function connect() {
    setNoBot(false);
    setError(null);
    setJustLinked(false);
    baseline.current = new Set((linksQ.data ?? []).map((i) => i.id));
    try {
      setPending(await getApiClient().issueTelegramLinkToken());
    } catch (e) {
      if (
        e instanceof ApiError &&
        e.status === 409 &&
        e.body?.code === 'RESOURCE_NOT_AVAILABLE'
      ) {
        setNoBot(true);
        return;
      }
      setError(
        e instanceof ApiError ? (e.body?.message ?? `HTTP ${e.status}`) : (e as Error).message,
      );
    }
  }

  /** Clears the attempt UI only — the one-shot token has no server
   *  revocation path and dies on its own ~10 min TTL. */
  function cancel() {
    setPending(null);
    setError(null);
  }

  return { linksQ, pending, secondsLeft, noBot, error, justLinked, connect, cancel };
}
