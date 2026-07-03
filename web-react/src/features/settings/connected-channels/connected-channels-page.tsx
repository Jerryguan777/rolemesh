// ConnectedChannelsPage — personal Telegram identity linking (spec
// Part M; behavioral reference the SHIPPED Lit connected-channels-page
// — unlike General/Members this page has real parity to hold).
//
// Scope: the USER'S OWN IM identity links (/api/v1/me/channel-links),
// so approvals and coworker messages push to them. Coworker↔channel
// bot bindings are a different resource and not on this page. The
// Part L member chips ({channel} linked) reflect THIS page's outcomes;
// the write path is the gateway's token redemption, never a form.
//
// The attempt state machine (baseline set, 3 s poll, countdown,
// 409-as-configuration) lives in use-link-attempt.ts. D-M3
// (user-approved): disconnect gets the designed confirm dialog with
// the "what stays" copy — the shipped Lit one-click is superseded;
// losing HITL push is consequential and the reassurance is real.

import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { ArrowLeft } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import {
  ApiError,
  getApiClient,
  type ChannelLinkIdentity,
} from '../../../api/client';
import { ConfirmDialog } from '../../../components/confirm-dialog';
import { useLinkAttempt } from './use-link-attempt';
import './connected-channels.css';

function mmss(s: number): string {
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

export function ConnectedChannelsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  function showToast(msg: string) {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast(msg);
    toastTimer.current = setTimeout(() => setToast(null), 3000);
  }
  useEffect(
    () => () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    },
    [],
  );

  const attempt = useLinkAttempt(() => showToast('Link code expired'));
  const { linksQ, pending } = attempt;
  const identities = linksQ.data ?? [];

  const [removing, setRemoving] = useState<ChannelLinkIdentity | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);
  const [removeErr, setRemoveErr] = useState<string | null>(null);

  async function confirmDisconnect() {
    if (!removing || removeBusy) return;
    setRemoveBusy(true);
    setRemoveErr(null);
    try {
      await getApiClient().unlinkChannelIdentity(removing.id);
      await queryClient.invalidateQueries({ queryKey: ['channel-links'] });
      showToast('Telegram account disconnected');
      setRemoving(null);
    } catch (e) {
      setRemoveErr(
        e instanceof ApiError ? (e.body?.message ?? `HTTP ${e.status}`) : (e as Error).message,
      );
    } finally {
      setRemoveBusy(false);
    }
  }

  async function copyToken() {
    if (!pending) return;
    try {
      await navigator.clipboard.writeText(pending.token);
      showToast('Code copied');
    } catch {
      // Clipboard may be denied — the code is on screen, selectable.
    }
  }

  return (
    <div className="page">
      <div>
        <button className="back-link" onClick={() => navigate('/')}>
          <ArrowLeft />
          Back to chat
        </button>
      </div>
      <div className="page-head">
        <div>
          <h1 className="page-title">Connected channels</h1>
          <div className="page-sub" style={{ maxWidth: 640 }}>
            Link your Telegram account to get coworker messages and approval requests
            there. Approvals reach you on Telegram if connected, and always in the web
            app&rsquo;s chat panel and inbox.
          </div>
        </div>
        {/* Rendered only once the list has loaded — the baseline set
            is captured from it on click, so connecting before the
            first GET resolves would false-flip on pre-existing links
            (Lit prevents this by not rendering the button until
            refresh() completes). */}
        {pending || attempt.noBot || !linksQ.data ? null : (
          <button
            className="btn-primary"
            data-testid="cc-connect"
            onClick={() => void attempt.connect()}
          >
            Connect Telegram
          </button>
        )}
      </div>

      <div className="grid-scroll" style={{ paddingTop: 12 }}>
        {attempt.noBot ? (
          <div className="cc-panel" data-testid="cc-no-bot">
            <b>Configure a Telegram bot first</b>
            <div className="hint" style={{ marginTop: 6 }}>
              This workspace has no Telegram bot connected yet. An owner or admin needs
              to bind one before members can link their accounts.
            </div>
          </div>
        ) : null}

        {attempt.justLinked ? (
          <div className="cc-panel" data-testid="cc-ok">
            <span className="cc-ok">✓ Telegram connected.</span>
            <div className="hint" style={{ marginTop: 4 }}>
              Approval requests and coworker messages will now reach this account.
            </div>
          </div>
        ) : null}

        {pending ? (
          <div className="cc-panel" data-testid="cc-pending">
            <b>Waiting for Telegram…</b>
            <div className="hint" style={{ margin: '6px 0 12px' }}>
              Open the link below (or send the code to the bot with{' '}
              <span style={{ fontFamily: 'ui-monospace, Menlo, monospace' }}>/start</span>
              ). This code works once and expires in{' '}
              <span className="cc-count" data-testid="cc-countdown">
                {mmss(attempt.secondsLeft)}
              </span>
              .
            </div>
            {pending.deep_link ? (
              <div style={{ marginBottom: 10 }}>
                <a
                  className="btn-primary"
                  style={{ textDecoration: 'none', display: 'inline-flex' }}
                  href={pending.deep_link}
                  target="_blank"
                  rel="noopener noreferrer"
                  data-testid="cc-deep-link"
                >
                  Open in Telegram
                </a>
              </div>
            ) : null}
            <div>
              <span className="cc-code" data-testid="cc-token">
                {pending.token}
              </span>
              <button
                className="btn-ghost"
                style={{ marginLeft: 8 }}
                data-testid="cc-copy"
                onClick={() => void copyToken()}
              >
                Copy
              </button>
            </div>
            <div style={{ marginTop: 12 }}>
              <button className="btn-ghost" data-testid="cc-cancel" onClick={attempt.cancel}>
                Cancel
              </button>
            </div>
          </div>
        ) : null}

        {attempt.error ? (
          <div className="row-error" role="alert" style={{ marginBottom: 12 }}>
            {attempt.error}
          </div>
        ) : null}

        {linksQ.isLoading ? (
          <div className="page-sub">Loading…</div>
        ) : linksQ.isError && !identities.length ? (
          <div className="row-error">Failed to load links — retry from the sidebar.</div>
        ) : identities.length ? (
          identities.map((i) => (
            <div className="model-row" key={i.id} data-testid={`cc-row-${i.id}`}>
              <span>
                <div className="m-name" style={{ textTransform: 'capitalize' }}>
                  {i.platform}
                </div>
                <div className="m-sub">
                  id {i.channel_id}
                  {i.created_at
                    ? ` · linked ${new Date(i.created_at).toLocaleDateString()}`
                    : ''}
                </div>
              </span>
              <span className="m-fill" />
              <button
                className="btn-ghost"
                data-testid="cc-disconnect"
                onClick={() => {
                  setRemoveErr(null);
                  setRemoving(i);
                }}
              >
                Disconnect
              </button>
            </div>
          ))
        ) : (
          <div className="hint" data-testid="cc-empty">
            No Telegram account linked yet. Connect one to get approvals pushed to you —
            they always remain available in the web app either way.
          </div>
        )}
      </div>

      {removing ? (
        <ConfirmDialog
          title="Disconnect this Telegram account?"
          confirmLabel="Disconnect"
          busyLabel="Disconnecting…"
          busy={removeBusy}
          onConfirm={() => void confirmDisconnect()}
          onCancel={() => {
            if (!removeBusy) setRemoving(null);
          }}
        >
          <p>
            Approval pushes and coworker messages to{' '}
            <b>id {removing.channel_id}</b> stop immediately.
          </p>
          <p>
            What stays: pending approvals remain in the web app&rsquo;s inbox, and you
            can reconnect this account at any time.
          </p>
          {removeErr ? (
            <p className="row-error" role="alert">
              {removeErr}
            </p>
          ) : null}
        </ConfirmDialog>
      ) : null}

      {toast ? (
        <div className="toast" role="status">
          {toast}
        </div>
      ) : null}
    </div>
  );
}
