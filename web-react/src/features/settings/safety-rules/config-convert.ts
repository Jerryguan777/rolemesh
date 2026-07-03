// Config format converters (backend ↔ internal display) + schema-enum
// helpers + validation, ported from web/src/components/safety-rule-dialog.ts
// @ feat/webui-react. Pure module so the wire round-trip is unit-testable.
//
// The wire stores per-check shapes (pii.regex `patterns` dict, presidio
// `block_codes`/`redact_codes`/`score_threshold`, moderation
// `block_categories`/`warn_categories`); the dialog edits a unified
// internal shape (`_piiKeys` list, `routing` map, `threshold`). The v10
// spec's I.6.3 "unified routing map" describes THIS internal form, not
// the wire — safSentence deliberately handles both.

import Ajv, { type ValidateFunction } from 'ajv';
import type { SafetyCheck } from '../../../api/client';

// ---- backend ↔ internal conversion ----

/** Convert backend-stored config → internal UI format on load (in place). */
export function normalizeConfigFromBackend(
  checkId: string,
  cfg: Record<string, unknown>,
): void {
  if (checkId === 'pii.regex') {
    // Backend: { patterns: { SSN: true, CREDIT_CARD: true, ... } }
    // Internal: selected backend keys as cfg['_piiKeys'] (string[])
    const patterns = (cfg['patterns'] as Record<string, boolean> | undefined) ?? {};
    cfg['_piiKeys'] = Object.keys(patterns).filter((k) => patterns[k]);
    delete cfg['patterns'];
  } else if (checkId === 'presidio.pii') {
    // Backend: { block_codes: [...], redact_codes: [...], score_threshold: 0.4 }
    // Internal: { routing: { CODE: 'block'|'redact' }, threshold: 0.4 }
    const blockCodes = (cfg['block_codes'] as string[]) ?? [];
    const redactCodes = (cfg['redact_codes'] as string[]) ?? [];
    const routing: Record<string, string> = {};
    for (const c of blockCodes) routing[c] = 'block';
    for (const c of redactCodes) routing[c] = 'redact';
    cfg['routing'] = routing;
    cfg['threshold'] = cfg['score_threshold'] ?? 0.4;
    delete cfg['block_codes'];
    delete cfg['redact_codes'];
    delete cfg['score_threshold'];
    delete cfg['language'];
  } else if (checkId === 'openai_moderation') {
    // Backend: { block_categories: [...], warn_categories: [...] }
    // Internal: { routing: { category: 'block'|'warn' } }
    const blockCats = (cfg['block_categories'] as string[]) ?? [];
    const warnCats = (cfg['warn_categories'] as string[]) ?? [];
    const routing: Record<string, string> = {};
    for (const c of blockCats) routing[c] = 'block';
    for (const c of warnCats) routing[c] = 'warn';
    cfg['routing'] = routing;
    delete cfg['block_categories'];
    delete cfg['warn_categories'];
  }
  // secret_scanner: backend only has action_override — no conversion.
}

/** Convert internal UI format → backend config on save. */
export function buildBackendConfig(
  checkId: string,
  cfg: Record<string, unknown>,
): Record<string, unknown> {
  const out = { ...cfg };
  if (checkId === 'pii.regex') {
    const keys = (out['_piiKeys'] as string[]) ?? [];
    delete out['_piiKeys'];
    const patterns: Record<string, boolean> = {};
    for (const k of keys) patterns[k] = true;
    out['patterns'] = patterns;
  } else if (checkId === 'presidio.pii') {
    const routing = (out['routing'] as Record<string, string>) ?? {};
    delete out['routing'];
    const threshold = out['threshold'];
    delete out['threshold'];
    out['block_codes'] = Object.entries(routing)
      .filter(([, a]) => a === 'block')
      .map(([c]) => c);
    out['redact_codes'] = Object.entries(routing)
      .filter(([, a]) => a === 'redact')
      .map(([c]) => c);
    out['score_threshold'] = threshold ?? 0.4;
  } else if (checkId === 'openai_moderation') {
    const routing = (out['routing'] as Record<string, string>) ?? {};
    delete out['routing'];
    out['block_categories'] = Object.entries(routing)
      .filter(([, a]) => a === 'block')
      .map(([c]) => c);
    out['warn_categories'] = Object.entries(routing)
      .filter(([, a]) => a === 'warn')
      .map(([c]) => c);
  }
  return out;
}

// ---- G7: schema-driven enum rendering (spec §6.12.5) ----

// Human-readable labels for known enum values. Unknown values fall
// through to raw-value display (reverse-drift property: backend adds a
// new enum → frontend renders it immediately with the raw code).
const ENUM_LABELS: Record<string, string> = {
  // pii.regex pattern keys (uppercase, from _CONFIG_KEY_TO_CODE)
  SSN: 'US Social Security numbers',
  CREDIT_CARD: 'Credit card numbers',
  EMAIL: 'Email addresses',
  PHONE_US: 'Phone numbers (US)',
  IP_ADDRESS: 'IP addresses',
  // presidio.pii stable codes (PresidioPIICode enum)
  'PII.SSN': 'US Social Security numbers',
  'PII.CREDIT_CARD': 'Credit card numbers',
  'PII.EMAIL': 'Email addresses',
  'PII.PHONE': 'Phone numbers',
  'PII.IP_ADDRESS': 'IP addresses',
  'PII.PERSON_NAME': "People's names",
  'PII.LOCATION': 'Locations',
  'PII.DATE_TIME': 'Dates and times',
  'PII.URL': 'URLs',
  'PII.IBAN': 'IBAN bank account numbers',
  'PII.US_BANK_NUMBER': 'US bank account numbers',
  'PII.US_DRIVER_LICENSE': 'US driver license numbers',
  'PII.US_PASSPORT': 'US passport numbers',
  'PII.MEDICAL_LICENSE': 'Medical license numbers',
  // openai_moderation stable codes (ModerationCode enum)
  'MODERATION.HARASSMENT': 'Harassment',
  'MODERATION.HATE': 'Hate speech',
  'MODERATION.VIOLENCE': 'Violence',
  'MODERATION.SEXUAL': 'Sexual content',
  'MODERATION.SELF_HARM': 'Self-harm',
  'MODERATION.ILLICIT': 'Illicit content',
};

export function enumLabel(v: string): string {
  return ENUM_LABELS[v] ?? v;
}

/** Read enum values from a check's config_schema field. `kind` is
 *  'items' for array-item enums or 'propertyNames' for dict-key enums.
 *  [] when absent — caller falls back to the hardcoded lists. */
export function getSchemaEnum(
  check: SafetyCheck | null,
  fieldPath: string,
  kind: 'items' | 'propertyNames',
): string[] {
  const schema = check?.config_schema as Record<string, unknown> | null | undefined;
  if (!schema || typeof schema !== 'object') return [];
  const props = (schema['properties'] as Record<string, unknown> | undefined) ?? {};
  const field = props[fieldPath] as Record<string, unknown> | undefined;
  if (!field) return [];
  const node = (field[kind] as Record<string, unknown> | undefined) ?? {};
  return Array.isArray(node['enum']) ? (node['enum'] as string[]) : [];
}

// Fallback entity lists for when config_schema is absent. Backend stable
// codes, NOT library entity names (verified against PresidioPIICode /
// ModerationCode enums).
export const PII_REGEX_FALLBACK: string[] = [
  'SSN', 'CREDIT_CARD', 'EMAIL', 'PHONE_US', 'IP_ADDRESS',
];
export const PRESIDIO_FALLBACK: string[] = [
  'PII.SSN', 'PII.CREDIT_CARD', 'PII.EMAIL', 'PII.PHONE', 'PII.IP_ADDRESS',
  'PII.PERSON_NAME', 'PII.LOCATION', 'PII.DATE_TIME', 'PII.URL',
  'PII.IBAN', 'PII.US_BANK_NUMBER', 'PII.US_DRIVER_LICENSE',
  'PII.US_PASSPORT', 'PII.MEDICAL_LICENSE',
];
export const MODERATION_FALLBACK: string[] = [
  'MODERATION.HARASSMENT', 'MODERATION.HATE', 'MODERATION.VIOLENCE',
  'MODERATION.SEXUAL', 'MODERATION.SELF_HARM', 'MODERATION.ILLICIT',
];

// ---- G4: client-side validation (§6.12.3 / §6.18) ----

export interface SaveError {
  fieldId?: string;
  message: string;
}

// Module-level Ajv + compiled-validator cache (compiled once per schema).
const _ajv = new Ajv({ allErrors: true, strict: false });
const _schemaValidators = new Map<string, ValidateFunction>();

function getSchemaValidator(check: SafetyCheck): ValidateFunction | null {
  const schema = check.config_schema as Record<string, unknown> | null | undefined;
  if (!schema || typeof schema !== 'object') return null;
  let v = _schemaValidators.get(check.id);
  if (!v) {
    v = _ajv.compile(schema);
    _schemaValidators.set(check.id, v);
  }
  return v;
}

/** Layer 1a — Ajv validation against config_schema. */
export function validateWithSchema(
  check: SafetyCheck | null,
  config: Record<string, unknown>,
): SaveError[] {
  if (!check) return [];
  const validate = getSchemaValidator(check);
  if (!validate) return [];
  if (validate(config)) return [];
  return (validate.errors ?? []).map((e) => {
    const path = e.instancePath?.replace(/^\//, '') ?? '';
    return { fieldId: path || undefined, message: e.message ?? 'Invalid value' };
  });
}

/** Layer 1b — hand-coded sanity checks for constraints JSON Schema can't
 *  express. Only runs when config_schema is present (without one the
 *  backend hasn't declared a shape, so {} may be intentional). */
export function sanityCheck(
  check: SafetyCheck | null,
  config: Record<string, unknown>,
): SaveError[] {
  if (!check?.config_schema) return [];
  const errs: SaveError[] = [];
  if (check.id === 'pii.regex') {
    const patterns = config['patterns'] as Record<string, boolean> | undefined;
    if (!patterns || Object.keys(patterns).length === 0) {
      errs.push({
        fieldId: 'saf-config',
        message: 'Pick at least one type of personal data to look for.',
      });
    }
  } else if (check.id === 'presidio.pii') {
    const bc = (config['block_codes'] as string[]) ?? [];
    const rc = (config['redact_codes'] as string[]) ?? [];
    if (bc.length === 0 && rc.length === 0) {
      errs.push({
        fieldId: 'saf-config',
        message: 'Set an action for at least one entity type.',
      });
    }
  } else if (check.id === 'openai_moderation') {
    const bl = (config['block_categories'] as string[]) ?? [];
    const wl = (config['warn_categories'] as string[]) ?? [];
    if (bl.length === 0 && wl.length === 0) {
      errs.push({
        fieldId: 'saf-config',
        message: 'Set an action for at least one category.',
      });
    }
  } else if (check.id === 'domain_allowlist') {
    const hosts = (config['allowed_hosts'] as string[]) ?? [];
    if (hosts.length === 0) {
      errs.push({ fieldId: 'saf-hosts', message: 'Add at least one host.' });
    }
  } else if (check.id === 'egress.domain_rule') {
    const patterns = (config['domain_patterns'] as string[]) ?? [];
    if (patterns.length === 0) {
      errs.push({ fieldId: 'saf-hosts', message: 'Add at least one domain pattern.' });
    }
  }
  return errs;
}

/** Combined pre-save validation (Layer 1). */
export function validateBeforeSave(
  check: SafetyCheck | null,
  config: Record<string, unknown>,
): SaveError[] {
  return [...validateWithSchema(check, config), ...sanityCheck(check, config)];
}

/** Layer 2 — translate a FastAPI 4xx `{ detail: [...] }` body into
 *  friendly messages. Empty when the shape is unexpected. */
export function parseBackend400(body: unknown): SaveError[] {
  if (!body || typeof body !== 'object') return [];
  const detail = (body as Record<string, unknown>)['detail'];
  if (!Array.isArray(detail)) return [];
  return detail
    .filter((e): e is Record<string, unknown> => !!e && typeof e === 'object')
    .map((e) => ({
      fieldId: undefined,
      message: translateFastApiError({
        type: e['type'] as string | undefined,
        loc: e['loc'] as unknown[] | undefined,
        msg: e['msg'] as string | undefined,
      }),
    }));
}

function translateFastApiError(err: {
  type?: string;
  loc?: unknown[];
  msg?: string;
}): string {
  const loc = Array.isArray(err.loc)
    ? err.loc.filter((s) => s !== 'body').join('.')
    : '';
  const field = loc ? `(field: ${loc})` : '';
  switch (err.type) {
    case 'extra_forbidden':
      return `Unknown field${loc ? ` '${loc}'` : ''} — this check doesn't accept that setting.`;
    case 'missing':
      return `Required field${loc ? ` '${loc}'` : ''} is missing.`;
    case 'int_parsing':
    case 'float_parsing':
      return `Field${loc ? ` '${loc}'` : ''} must be a number.`;
    case 'enum':
      return `Field${loc ? ` '${loc}'` : ''} has an invalid value. ${err.msg ?? ''} ${field}`.trim();
    default:
      return err.msg
        ? `${err.msg} ${field}`.trim()
        : `The server rejected this field ${field}.`.trim();
  }
}

/** Postel's-Law host normalizer: strip scheme + trailing path, lowercase.
 *  Truly-invalid lines are left as-is so schema validation surfaces a
 *  precise error rather than silently mangling them. */
export function normalizeDomainLine(raw: string): string {
  let s = raw.trim().toLowerCase();
  s = s.replace(/^https?:\/\//, '');
  const slash = s.indexOf('/');
  if (slash !== -1) s = s.slice(0, slash);
  return s;
}
