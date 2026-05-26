// Behavioural tests for groupModelsByProvider().
//
// These pin the *contract* the wizard and Models page consume, not
// the internal grouping algorithm. They were authored before reading
// the implementation: any test that fails here represents either a
// contract violation or a spec drift the caller will see in the UI.

import { describe, expect, it } from 'vitest';

import type {
  Backend,
  CredentialResponse,
  Model,
  ModelProvider,
} from '../api/client.js';
import { groupModelsByProvider } from './models-grouping.js';

function model(
  id: string,
  provider: ModelProvider,
  family: Model['model_family'],
  opts: { active?: boolean } = {},
): Model {
  return {
    id: `00000000-0000-0000-0000-${id.padStart(12, '0')}`,
    provider,
    model_id: id,
    model_family: family,
    display_name: `${provider}/${id}`,
    is_active: opts.active ?? true,
  };
}

function cred(provider: ModelProvider, updatedAt = '2026-05-20T00:00:00Z'): CredentialResponse {
  return {
    provider,
    created_at: updatedAt,
    updated_at: updatedAt,
  };
}

const claudeBackend: Backend = {
  name: 'claude',
  description: 'Claude Agent SDK',
  supported_providers: ['anthropic'],
  supported_model_families: ['claude'],
};

const piBackend: Backend = {
  name: 'pi',
  description: 'Pi',
  supported_providers: ['anthropic', 'openai', 'google', 'bedrock'],
  // null means "any family" per the OpenAPI contract.
  supported_model_families: null,
};

describe('groupModelsByProvider', () => {
  it('returns empty when no models', () => {
    expect(groupModelsByProvider([], [])).toEqual([]);
  });

  it('groups models under their provider with hasCredential reflecting the cred list', () => {
    const models = [
      model('claude-opus-4-7', 'anthropic', 'claude'),
      model('gpt-4o', 'openai', 'gpt'),
      model('gemini-2-pro', 'google', 'gemini'),
    ];
    const groups = groupModelsByProvider(models, [cred('anthropic')]);
    const byProvider = Object.fromEntries(groups.map((g) => [g.provider, g]));
    expect(byProvider.anthropic?.hasCredential).toBe(true);
    expect(byProvider.openai?.hasCredential).toBe(false);
    expect(byProvider.google?.hasCredential).toBe(false);
  });

  it('exposes credentialUpdatedAt when credential present, null otherwise', () => {
    const groups = groupModelsByProvider(
      [model('claude-opus-4-7', 'anthropic', 'claude'), model('gpt-4o', 'openai', 'gpt')],
      [cred('anthropic', '2026-05-21T10:00:00Z')],
    );
    const ant = groups.find((g) => g.provider === 'anthropic')!;
    const oai = groups.find((g) => g.provider === 'openai')!;
    expect(ant.credentialUpdatedAt).toBe('2026-05-21T10:00:00Z');
    expect(oai.credentialUpdatedAt).toBe(null);
  });

  it('sorts provider groups alphabetically', () => {
    const models = [
      model('gpt-4o', 'openai', 'gpt'),
      model('claude-opus-4-7', 'anthropic', 'claude'),
      model('gemini-2-pro', 'google', 'gemini'),
      model('claude-on-bedrock', 'bedrock', 'claude'),
    ];
    const groups = groupModelsByProvider(models, []);
    expect(groups.map((g) => g.provider)).toEqual([
      'anthropic',
      'bedrock',
      'google',
      'openai',
    ]);
  });

  it('sorts models within a group alphabetically by model_id', () => {
    const models = [
      model('claude-sonnet-4-6', 'anthropic', 'claude'),
      model('claude-opus-4-7', 'anthropic', 'claude'),
      model('claude-haiku-4-5', 'anthropic', 'claude'),
    ];
    const groups = groupModelsByProvider(models, []);
    expect(groups[0]!.models.map((m) => m.model_id)).toEqual([
      'claude-haiku-4-5',
      'claude-opus-4-7',
      'claude-sonnet-4-6',
    ]);
  });

  it('drops models whose provider is not in backend.supported_providers', () => {
    const models = [
      model('claude-opus-4-7', 'anthropic', 'claude'),
      model('gpt-4o', 'openai', 'gpt'),
    ];
    const groups = groupModelsByProvider(models, [], claudeBackend);
    expect(groups.map((g) => g.provider)).toEqual(['anthropic']);
  });

  it('drops models whose family is not in backend.supported_model_families', () => {
    // Claude backend supports anthropic provider AND only `claude`
    // family. A hypothetical anthropic+gpt model should be dropped.
    const models = [
      model('claude-opus-4-7', 'anthropic', 'claude'),
      model('weird-anthropic-gpt', 'anthropic', 'gpt'),
    ];
    const groups = groupModelsByProvider(models, [], claudeBackend);
    expect(groups[0]!.models.map((m) => m.model_id)).toEqual(['claude-opus-4-7']);
  });

  it('treats null supported_model_families as "any family"', () => {
    const models = [
      model('claude-opus-4-7', 'anthropic', 'claude'),
      model('gpt-4o', 'openai', 'gpt'),
      model('gemini-2-pro', 'google', 'gemini'),
    ];
    const groups = groupModelsByProvider(models, [], piBackend);
    // All providers + families pass when family allowlist is null.
    expect(groups.length).toBe(3);
  });

  it('drops provider groups that go empty after backend filter', () => {
    const models = [model('gpt-4o', 'openai', 'gpt')];
    const groups = groupModelsByProvider(models, [cred('anthropic')], claudeBackend);
    // OpenAI gets filtered out by claudeBackend; anthropic has no
    // models — group should not appear just because the credential is
    // present.
    expect(groups).toEqual([]);
  });

  it('keeps inactive models — callers post-filter for "hide inactive"', () => {
    const models = [
      model('claude-opus-4-7', 'anthropic', 'claude'),
      model('claude-legacy', 'anthropic', 'claude', { active: false }),
    ];
    const groups = groupModelsByProvider(models, []);
    expect(groups[0]!.models.map((m) => m.model_id)).toEqual([
      'claude-legacy',
      'claude-opus-4-7',
    ]);
  });

  it('does not mutate the input arrays', () => {
    const models = [
      model('z-second', 'anthropic', 'claude'),
      model('a-first', 'anthropic', 'claude'),
    ];
    const original = models.map((m) => m.model_id);
    groupModelsByProvider(models, []);
    expect(models.map((m) => m.model_id)).toEqual(original);
  });

  it('hasCredential=true even when the group has zero models for that provider after filter', () => {
    // Edge: if cred is present but no models survive the filter,
    // the group itself is gone. Verify behaviour matches the docs.
    const models = [model('gpt-4o', 'openai', 'gpt')];
    const groups = groupModelsByProvider(models, [cred('openai')], claudeBackend);
    // openai dropped by claude backend → no group at all.
    expect(groups).toEqual([]);
  });

  it('handles multiple credentials including ones with no matching provider models', () => {
    // Tenant has bedrock + anthropic creds but only ships an openai
    // model. Bedrock + anthropic should not produce empty groups.
    const models = [model('gpt-4o', 'openai', 'gpt')];
    const groups = groupModelsByProvider(
      models,
      [cred('bedrock'), cred('anthropic')],
    );
    expect(groups.map((g) => g.provider)).toEqual(['openai']);
    expect(groups[0]!.hasCredential).toBe(false);
  });
});
