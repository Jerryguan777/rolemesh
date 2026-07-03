import { describe, expect, it } from 'vitest';
import type { SafetyCheck } from '../../../api/client';
import {
  buildBackendConfig,
  getSchemaEnum,
  normalizeConfigFromBackend,
  normalizeDomainLine,
  parseBackend400,
  sanityCheck,
  validateBeforeSave,
} from './config-convert';

function check(over: Partial<SafetyCheck> & Pick<SafetyCheck, 'id'>): SafetyCheck {
  return {
    version: '1',
    stages: [],
    cost_class: 'cheap',
    action_model: 'fixed',
    natural_actions: {},
    supported_actions: {},
    supported_codes: [],
    config_schema: null,
    ...over,
  } as SafetyCheck;
}

describe('config round-trip (wire ↔ internal)', () => {
  it('pii.regex: patterns dict → _piiKeys and back', () => {
    const cfg: Record<string, unknown> = {
      patterns: { SSN: true, CREDIT_CARD: true, EMAIL: false },
    };
    normalizeConfigFromBackend('pii.regex', cfg);
    expect(cfg['_piiKeys']).toEqual(['SSN', 'CREDIT_CARD']);
    expect(cfg['patterns']).toBeUndefined();
    const back = buildBackendConfig('pii.regex', cfg);
    expect(back['patterns']).toEqual({ SSN: true, CREDIT_CARD: true });
    expect(back['_piiKeys']).toBeUndefined();
  });

  it('presidio: block/redact codes → routing map + threshold and back', () => {
    const cfg: Record<string, unknown> = {
      block_codes: ['PII.SSN'],
      redact_codes: ['PII.EMAIL'],
      score_threshold: 0.55,
      language: 'en',
    };
    normalizeConfigFromBackend('presidio.pii', cfg);
    expect(cfg['routing']).toEqual({ 'PII.SSN': 'block', 'PII.EMAIL': 'redact' });
    expect(cfg['threshold']).toBe(0.55);
    expect(cfg['block_codes']).toBeUndefined();
    const back = buildBackendConfig('presidio.pii', cfg);
    expect(back['block_codes']).toEqual(['PII.SSN']);
    expect(back['redact_codes']).toEqual(['PII.EMAIL']);
    expect(back['score_threshold']).toBe(0.55);
    expect(back['routing']).toBeUndefined();
  });

  it('moderation: block/warn categories → routing map and back', () => {
    const cfg: Record<string, unknown> = {
      block_categories: ['MODERATION.HATE'],
      warn_categories: ['MODERATION.SEXUAL'],
    };
    normalizeConfigFromBackend('openai_moderation', cfg);
    expect(cfg['routing']).toEqual({
      'MODERATION.HATE': 'block',
      'MODERATION.SEXUAL': 'warn',
    });
    const back = buildBackendConfig('openai_moderation', cfg);
    expect(back['block_categories']).toEqual(['MODERATION.HATE']);
    expect(back['warn_categories']).toEqual(['MODERATION.SEXUAL']);
  });

  it('leaves unknown checks untouched', () => {
    const cfg: Record<string, unknown> = { phrases: ['x'] };
    normalizeConfigFromBackend('llm_guard.jailbreak', cfg);
    expect(cfg).toEqual({ phrases: ['x'] });
    expect(buildBackendConfig('llm_guard.jailbreak', cfg)).toEqual({ phrases: ['x'] });
  });
});

describe('normalizeDomainLine (Postel cleanup)', () => {
  it('strips scheme + path, lowercases', () => {
    expect(normalizeDomainLine('https://API.Stripe.com/v1/charges')).toBe(
      'api.stripe.com',
    );
    expect(normalizeDomainLine('http://a.com/')).toBe('a.com');
  });
  it('keeps wildcards and bare hosts as-is', () => {
    expect(normalizeDomainLine('*.acme.com')).toBe('*.acme.com');
  });
});

describe('getSchemaEnum', () => {
  it('reads array-item enums', () => {
    const c = check({
      id: 'presidio.pii',
      config_schema: {
        properties: {
          block_codes: { items: { enum: ['PII.SSN', 'PII.EMAIL'] } },
        },
      },
    });
    expect(getSchemaEnum(c, 'block_codes', 'items')).toEqual(['PII.SSN', 'PII.EMAIL']);
  });
  it('reads propertyNames enums and falls back to [] when absent', () => {
    const c = check({
      id: 'pii.regex',
      config_schema: {
        properties: { patterns: { propertyNames: { enum: ['SSN'] } } },
      },
    });
    expect(getSchemaEnum(c, 'patterns', 'propertyNames')).toEqual(['SSN']);
    expect(getSchemaEnum(check({ id: 'x' }), 'patterns', 'propertyNames')).toEqual([]);
    expect(getSchemaEnum(null, 'patterns', 'items')).toEqual([]);
  });
});

describe('sanityCheck (schema-gated)', () => {
  const schema = { type: 'object' };
  it('flags empty pii.regex patterns / empty host lists', () => {
    expect(
      sanityCheck(check({ id: 'pii.regex', config_schema: schema }), { patterns: {} }),
    ).toHaveLength(1);
    expect(
      sanityCheck(check({ id: 'domain_allowlist', config_schema: schema }), {
        allowed_hosts: [],
      }),
    ).toHaveLength(1);
    expect(
      sanityCheck(check({ id: 'egress.domain_rule', config_schema: schema }), {}),
    ).toHaveLength(1);
  });
  it('flags an inert presidio/moderation config', () => {
    expect(
      sanityCheck(check({ id: 'presidio.pii', config_schema: schema }), {
        block_codes: [],
        redact_codes: [],
      }),
    ).toHaveLength(1);
  });
  it('skips entirely when the check declares no config_schema', () => {
    expect(sanityCheck(check({ id: 'pii.regex' }), { patterns: {} })).toEqual([]);
  });
});

describe('validateBeforeSave — Ajv layer', () => {
  it('surfaces a schema violation with the field path', () => {
    const c = check({
      id: 'llm_guard.toxicity',
      config_schema: {
        type: 'object',
        properties: { threshold: { type: 'number', maximum: 1 } },
      },
    });
    const errs = validateBeforeSave(c, { threshold: 2 });
    expect(errs.length).toBeGreaterThan(0);
    expect(errs[0].fieldId).toBe('threshold');
  });
  it('passes a valid config', () => {
    const c = check({
      id: 'llm_guard.toxicity',
      config_schema: {
        type: 'object',
        properties: { threshold: { type: 'number', maximum: 1 } },
      },
    });
    expect(validateBeforeSave(c, { threshold: 0.7 })).toEqual([]);
  });
});

describe('parseBackend400', () => {
  it('translates a FastAPI detail array to friendly messages', () => {
    const errs = parseBackend400({
      detail: [
        { type: 'extra_forbidden', loc: ['body', 'config', 'bogus'], msg: 'extra' },
        { type: 'missing', loc: ['body', 'check_id'], msg: 'required' },
      ],
    });
    expect(errs).toHaveLength(2);
    expect(errs[0].message).toMatch(/Unknown field 'config.bogus'/);
    expect(errs[1].message).toMatch(/Required field 'check_id' is missing/);
  });
  it('returns [] for non-detail shapes', () => {
    expect(parseBackend400({ message: 'nope' })).toEqual([]);
    expect(parseBackend400(null)).toEqual([]);
  });
});
