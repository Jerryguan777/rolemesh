// CredentialDialog — per-provider credential capture (spec G.3;
// behavioral reference web/src/components/credential-dialog.ts).
//
// Lives in components/ (D-CR2): three settings siblings mount it — the
// credentials page, the models page's Add-credential/Connect, and the
// coworker wizard's step-3 `+ Add credential` — so it cannot live in
// any one settings slug folder without crossing the §1.1
// sibling-isolation boundary. Self-contained: it owns the PUT and
// invalidates the shared ['credentials'] + ['models'] query keys (the
// Lit `credential-saved` event), so every consumer re-renders and
// unlocks live with no extra wiring. onSaved is an optional hook for
// side effects the mounting page wants (e.g. a toast).
//
// Security invariants (Lit parity): the existing key is NEVER
// pre-filled — the wire type has no secret field — and the plaintext is
// dropped from form state the instant the write completes.

import { useEffect, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { X } from 'lucide-react';
import {
  ApiError,
  getApiClient,
  type CredentialUpsert,
  type ModelProvider,
} from '../api/client';
import { useCredentials } from '../api/queries';
import { BrandMark } from './brand-mark';
import { PROVIDER_SCHEMAS, PROVIDERS, schemaFor } from './provider-schemas';

function defaultExtras(provider: ModelProvider): Record<string, string> {
  const out: Record<string, string> = {};
  for (const e of schemaFor(provider).requiredExtras) {
    if (e.defaultValue !== undefined) out[e.key] = e.defaultValue;
  }
  return out;
}

export function CredentialDialog({
  provider,
  onClose,
  onSaved,
}: {
  /** Locked provider (row pencil, models Connect, wizard). `null` →
   *  render the provider `<select>` (page/models header Add credential). */
  provider: ModelProvider | null;
  onClose: () => void;
  /** Optional side-effect hook after a successful save (e.g. a toast). */
  onSaved?: (provider: ModelProvider) => void;
}) {
  const queryClient = useQueryClient();
  const credentialsQ = useCredentials(true);

  const [picked, setPicked] = useState<ModelProvider>(provider ?? PROVIDERS[0]);
  const current = provider ?? picked;

  const [apiKey, setApiKey] = useState('');
  const [extras, setExtras] = useState<Record<string, string>>(() =>
    defaultExtras(current),
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const schema = schemaFor(current);
  const existing = useMemo(
    () => (credentialsQ.data ?? []).some((c) => c.provider === current),
    [credentialsQ.data, current],
  );

  // ESC closes (unless a write is in flight).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      if (!busy) onClose();
    }
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [busy, onClose]);

  function switchProvider(p: ModelProvider) {
    setPicked(p);
    setExtras(defaultExtras(p));
    setApiKey('');
    setErr(null);
  }

  async function save() {
    if (busy) return;
    if (apiKey.trim() === '') {
      setErr(`${schema.apiKey.label} is required.`);
      return;
    }
    for (const e of schema.requiredExtras) {
      if ((extras[e.key] ?? '').trim() === '') {
        setErr(`${e.label} is required.`);
        return;
      }
    }
    // Required extras always sent; optional only when non-empty (never
    // POST `extras: { api_base: '' }`). Empty map → null.
    const out: Record<string, string> = {};
    for (const e of schema.requiredExtras) out[e.key] = (extras[e.key] ?? '').trim();
    for (const e of schema.optionalExtras) {
      const v = (extras[e.key] ?? '').trim();
      if (v !== '') out[e.key] = v;
    }
    const body: CredentialUpsert = {
      api_key: apiKey.trim(),
      extras: Object.keys(out).length ? out : null,
    };
    setBusy(true);
    setErr(null);
    try {
      await getApiClient().putCredential(current, body);
      // Drop the plaintext immediately.
      setApiKey('');
      setExtras(defaultExtras(current));
      // credential-saved: every consumer (this page, Part F groups, an
      // open wizard step 3) re-renders and unlocks live.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['credentials'] }),
        queryClient.invalidateQueries({ queryKey: ['models'] }),
      ]);
      onSaved?.(current);
      onClose();
    } catch (e) {
      setErr(
        e instanceof ApiError ? (e.body?.message ?? `${e.status}`) : (e as Error).message,
      );
      setBusy(false);
    }
  }

  const keyPlaceholder = existing
    ? '•••••••• (stored — typing replaces it)'
    : schema.apiKey.placeholder;

  return (
    <div
      className="scrim"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div
        className="dlg"
        style={{ width: 480 }}
        role="dialog"
        aria-modal="true"
        aria-label="Credential"
      >
        <div className="dlg-header">
          <div className="hleft">
            <div className="dlg-brand-icon">
              <BrandMark size={16} />
            </div>
            <h2 className="dlg-title">{`Add ${schema.label} credential`}</h2>
          </div>
          <button className="icon-btn" aria-label="Close" disabled={busy} onClick={onClose}>
            <X />
          </button>
        </div>
        <div className="wiz-body" style={{ minHeight: 0 }}>
          <div className="hint" style={{ marginBottom: 12 }}>
            {schema.blurb}
          </div>

          {provider === null ? (
            <div className="field">
              <label htmlFor="cr-provider">Provider</label>
              <select
                id="cr-provider"
                value={picked}
                disabled={busy}
                onChange={(e) => switchProvider(e.target.value as ModelProvider)}
              >
                {PROVIDER_SCHEMAS.map((s) => (
                  <option key={s.provider} value={s.provider}>
                    {s.label}
                  </option>
                ))}
              </select>
            </div>
          ) : null}

          <div className="field">
            <label htmlFor="cr-key">{schema.apiKey.label}</label>
            <input
              id="cr-key"
              type="password"
              autoComplete="new-password"
              spellCheck={false}
              placeholder={keyPlaceholder}
              value={apiKey}
              disabled={busy}
              onChange={(e) => setApiKey(e.target.value)}
            />
            {schema.apiKey.helperText ? (
              <div className="hint">{schema.apiKey.helperText}</div>
            ) : null}
          </div>

          {[...schema.requiredExtras, ...schema.optionalExtras].map((e) => (
            <div className="field" key={e.key}>
              <label htmlFor={`cr-extra-${e.key}`}>{e.label}</label>
              <input
                id={`cr-extra-${e.key}`}
                type="text"
                className="mono"
                spellCheck={false}
                placeholder={e.placeholder ?? ''}
                value={extras[e.key] ?? ''}
                disabled={busy}
                onChange={(ev) =>
                  setExtras((x) => ({ ...x, [e.key]: ev.target.value }))
                }
              />
            </div>
          ))}

          <div className="hint" style={{ marginTop: 8 }}>
            The credential is envelope-encrypted server-side and never displayed back.
          </div>
        </div>
        <div className="wiz-foot">
          {err ? (
            <span className="wiz-err" role="alert">
              {err}
            </span>
          ) : (
            <span />
          )}
          <span style={{ display: 'inline-flex', gap: 8 }}>
            <button className="btn-ghost" disabled={busy} onClick={onClose}>
              Cancel
            </button>
            <button className="btn-primary" disabled={busy} onClick={() => void save()}>
              {busy ? 'Saving…' : 'Save'}
            </button>
          </span>
        </div>
      </div>
    </div>
  );
}
