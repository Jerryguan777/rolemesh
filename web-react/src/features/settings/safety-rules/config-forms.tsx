// Per-cfgKind config forms (spec §6.12 / I.3) — one module, one small
// component per kind, all editing the dialog's INTERNAL config shape
// (config-convert.ts translates wire ↔ internal on load/save):
//   pii-entities      → checkbox grid over backend pattern keys
//   secret-plugins    → info note (SecretScannerConfig has no options)
//   threshold         → sensitivity slider
//   host-list         → hosts textarea (+ optional Ports for egress)
//   presidio-routing  → per-type routing table + confidence slider
//   moderation-routing→ per-category routing table (block/warn only)

import type { SafetyCheck, SafetyStage } from '../../../api/client';
import {
  MODERATION_FALLBACK,
  PII_REGEX_FALLBACK,
  PRESIDIO_FALLBACK,
  enumLabel,
  getSchemaEnum,
  normalizeDomainLine,
} from './config-convert';
import { SAF_ACTION_LABEL, supportedActions } from '../../../lib/safety-catalog';

export type Config = Record<string, unknown>;
type OnChange = (next: Config) => void;

export function ConfigForm({
  check,
  cfgKind,
  stage,
  config,
  busy,
  onChange,
}: {
  check: SafetyCheck;
  cfgKind: string;
  stage: SafetyStage;
  config: Config;
  busy: boolean;
  onChange: OnChange;
}) {
  switch (cfgKind) {
    case 'pii-entities':
      return <PiiEntityGrid check={check} config={config} busy={busy} onChange={onChange} />;
    case 'secret-plugins':
      // Backend SecretScannerConfig has action_override only — an info
      // note instead of a broken checkbox grid.
      return (
        <div className="hint">
          This check runs all built-in secret detectors automatically. No
          additional configuration is needed — use the action picker above to
          choose what happens when a secret is found.
        </div>
      );
    case 'threshold':
      return (
        <Threshold
          label="How sensitive should the check be?"
          fallback={0.7}
          config={config}
          busy={busy}
          onChange={onChange}
        />
      );
    case 'host-list':
      return <HostList checkId={check.id} config={config} busy={busy} onChange={onChange} />;
    case 'presidio-routing':
      return (
        <>
          <RoutingTable
            check={check}
            stage={stage}
            noun="type"
            codes={
              getSchemaEnum(check, 'block_codes', 'items').length
                ? getSchemaEnum(check, 'block_codes', 'items')
                : PRESIDIO_FALLBACK
            }
            excludeActions={['allow']}
            config={config}
            busy={busy}
            onChange={onChange}
          />
          <Threshold
            label="Confidence"
            fallback={0.6}
            config={config}
            busy={busy}
            onChange={onChange}
          />
        </>
      );
    case 'moderation-routing':
      // Only block/warn are expressible per-category (the wire has no
      // require_approval_categories field).
      return (
        <RoutingTable
          check={check}
          stage={stage}
          noun="category"
          codes={
            getSchemaEnum(check, 'block_categories', 'items').length
              ? getSchemaEnum(check, 'block_categories', 'items')
              : MODERATION_FALLBACK
          }
          excludeActions={['allow', 'require_approval', 'redact']}
          config={config}
          busy={busy}
          onChange={onChange}
        />
      );
    case 'jailbreak-phrases':
      return <JailbreakPhrases config={config} busy={busy} onChange={onChange} />;
    default:
      return null;
  }
}

function PiiEntityGrid({
  check,
  config,
  busy,
  onChange,
}: {
  check: SafetyCheck;
  config: Config;
  busy: boolean;
  onChange: OnChange;
}) {
  // G7: enum keys from config_schema; static fallback when absent.
  const schemaKeys = getSchemaEnum(check, 'patterns', 'propertyNames');
  const keys = schemaKeys.length ? schemaKeys : PII_REGEX_FALLBACK;
  const selected = new Set((config['_piiKeys'] as string[]) ?? []);
  return (
    <div className="field" style={{ marginBottom: 0 }}>
      <label>What to look for</label>
      <div className="cfg-grid" data-testid="saf-pii-grid">
        {keys.map((backendKey) => (
          <label key={backendKey}>
            <input
              type="checkbox"
              disabled={busy}
              checked={selected.has(backendKey)}
              onChange={(e) => {
                const next = new Set(selected);
                if (e.target.checked) next.add(backendKey);
                else next.delete(backendKey);
                onChange({ ...config, _piiKeys: [...next] });
              }}
            />
            <span>
              {enumLabel(backendKey)} <span className="code">{backendKey}</span>
            </span>
          </label>
        ))}
      </div>
    </div>
  );
}

function Threshold({
  label,
  fallback,
  config,
  busy,
  onChange,
}: {
  label: string;
  fallback: number;
  config: Config;
  busy: boolean;
  onChange: OnChange;
}) {
  const value = (config['threshold'] as number) ?? fallback;
  return (
    <div className="field" style={{ marginBottom: 0, marginTop: 10 }}>
      <label>{label}</label>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <span className="hint" style={{ margin: 0, minWidth: 70 }}>
          Sensitivity
        </span>
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          data-testid="saf-threshold"
          style={{ flex: 1 }}
          disabled={busy}
          value={value}
          onChange={(e) =>
            onChange({ ...config, threshold: parseFloat(e.target.value) })
          }
        />
        <span style={{ fontSize: '0.8125rem', fontWeight: 700, minWidth: 34 }}>
          {value}
        </span>
      </div>
      <div
        className="hint"
        style={{ display: 'flex', justifyContent: 'space-between', marginTop: 2 }}
      >
        <span>stricter (catches more)</span>
        <span>looser (catches less)</span>
      </div>
    </div>
  );
}

function JailbreakPhrases({
  config,
  busy,
  onChange,
}: {
  config: Config;
  busy: boolean;
  onChange: OnChange;
}) {
  const phrases = ((config['phrases'] as string[]) ?? []).join('\n');
  const caseSensitive = (config['case_sensitive'] as boolean) ?? false;
  return (
    <div className="field" style={{ marginBottom: 0 }}>
      <label>
        Custom detection phrases{' '}
        <span style={{ fontWeight: 400, color: 'var(--rm-text-muted)' }}>
          one per line · leave blank to use the built-in list
        </span>
      </label>
      <textarea
        className="mono"
        style={{ minHeight: 72 }}
        data-testid="saf-jailbreak-phrases"
        placeholder={'ignore all previous instructions\npretend you have no restrictions'}
        disabled={busy}
        defaultValue={phrases}
        onChange={(e) => {
          const lines = e.target.value
            .split('\n')
            .map((s) => s.trim())
            .filter(Boolean);
          onChange({ ...config, phrases: lines });
        }}
      />
      <label
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          marginTop: 8,
          fontWeight: 400,
          fontSize: '0.8125rem',
        }}
      >
        <input
          type="checkbox"
          disabled={busy}
          checked={caseSensitive}
          onChange={(e) => onChange({ ...config, case_sensitive: e.target.checked })}
        />
        Case-sensitive matching
      </label>
    </div>
  );
}

function HostList({
  checkId,
  config,
  busy,
  onChange,
}: {
  checkId: string;
  config: Config;
  busy: boolean;
  onChange: OnChange;
}) {
  const isEgress = checkId === 'egress.domain_rule';
  // egress.domain_rule → domain_patterns (+ optional ports);
  // domain_allowlist → allowed_hosts.
  const configKey = isEgress ? 'domain_patterns' : 'allowed_hosts';
  const hosts = ((config[configKey] as string[]) ?? []).join('\n');
  const portsRaw = ((config['ports'] as number[]) ?? []).join(', ');

  const readLines = (raw: string) =>
    raw
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean);

  return (
    <div className="field" style={{ marginBottom: 0 }}>
      <label>
        {isEgress ? 'Allowed domains' : 'Allowed hosts'}{' '}
        <span style={{ fontWeight: 400, color: 'var(--rm-text-muted)' }}>
          one per line · wildcards like *.stripe.com
          {isEgress ? " · we'll clean URLs on save" : ''}
        </span>
      </label>
      <textarea
        className="mono"
        style={{ minHeight: 80 }}
        data-testid="saf-hosts"
        placeholder={'api.stripe.com\n*.internal.acme.com'}
        disabled={busy}
        defaultValue={hosts}
        onChange={(e) => onChange({ ...config, [configKey]: readLines(e.target.value) })}
        onBlur={(e) => {
          // Postel's-Law cleanup: strip schemes/paths, lowercase.
          const lines = readLines(e.target.value).map(normalizeDomainLine);
          e.target.value = lines.join('\n');
          onChange({ ...config, [configKey]: lines });
        }}
      />
      {isEgress ? (
        <>
          <label style={{ marginTop: 8 }}>
            Ports{' '}
            <span style={{ fontWeight: 400, color: 'var(--rm-text-muted)' }}>
              optional · comma-separated · leave blank for any port
            </span>
          </label>
          <input
            type="text"
            data-testid="saf-egress-ports"
            placeholder="443, 8443"
            disabled={busy}
            defaultValue={portsRaw}
            onChange={(e) => {
              const parsed = e.target.value
                .split(',')
                .map((s) => parseInt(s.trim(), 10))
                .filter((n) => !Number.isNaN(n) && n > 0 && n <= 65535);
              const next = { ...config };
              if (parsed.length) next['ports'] = parsed;
              else delete next['ports'];
              onChange(next);
            }}
          />
        </>
      ) : null}
      <div className="hint">
        The coworker can only reach these {isEgress ? 'domains' : 'hosts'}. Any
        other outbound request is blocked.
        {isEgress ? (
          <>
            {' '}
            <code>*.acme.com</code> matches subdomains but not <code>acme.com</code>{' '}
            itself.
          </>
        ) : null}
      </div>
    </div>
  );
}

function RoutingTable({
  check,
  stage,
  noun,
  codes,
  excludeActions,
  config,
  busy,
  onChange,
}: {
  check: SafetyCheck;
  stage: SafetyStage;
  noun: 'type' | 'category';
  codes: string[];
  excludeActions: string[];
  config: Config;
  busy: boolean;
  onChange: OnChange;
}) {
  const routing = (config['routing'] as Record<string, string>) ?? {};
  const options = supportedActions(check, stage).filter(
    (a) => !excludeActions.includes(a),
  );
  const anyRouted = Object.values(routing).some(Boolean);
  return (
    <div className="field" style={{ marginBottom: 0 }}>
      <label>
        Choose an action for each {noun}{' '}
        <span style={{ fontWeight: 400, color: 'var(--rm-text-muted)' }}>
          leave any blank to let it through
        </span>
      </label>
      <div className="route-table" data-testid="saf-routing">
        {codes.map((code) => (
          <div className="rt-row" key={code}>
            <div className="rt-label">
              {enumLabel(code)}
              <span className="rt-code">{code}</span>
            </div>
            <select
              aria-label={`Action for ${enumLabel(code)}`}
              disabled={busy}
              value={routing[code] ?? ''}
              onChange={(e) => {
                const v = e.target.value;
                const next = { ...routing };
                if (v) next[code] = v;
                else delete next[code];
                onChange({ ...config, routing: next });
              }}
            >
              <option value="">— allow these —</option>
              {options.map((a) => (
                <option key={a} value={a}>
                  {SAF_ACTION_LABEL[a]}
                </option>
              ))}
            </select>
          </div>
        ))}
      </div>
      {!anyRouted ? (
        <div className="hint" data-testid="saf-routing-inert" style={{ marginTop: 6 }}>
          <b>Nothing set yet.</b> The check runs but won't do anything. Pick an
          action for at least one {noun} above to make it active.
        </div>
      ) : null}
    </div>
  );
}
