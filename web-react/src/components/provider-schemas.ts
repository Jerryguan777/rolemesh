// Copied from web/src/components/credential-dialog.ts @ feat/webui-react;
// keep in sync manually until workspace extraction. Pure data table
// (spec G.3): per-provider label, blurb, api-key field, and required /
// optional extras. Adding a provider is a data-only edit — same
// principle as the MCP AUTH_MODES table (D-M2).
//
// Lives in components/ (not features/settings/credentials/) because
// THREE settings siblings consume the credential dialog it feeds — the
// credentials page, the models page, and the coworker wizard — and the
// §1.1 sibling-isolation rule forbids one settings page importing
// another. Graduating the shared surface here mirrors confirm-dialog
// (D-CR2). PROVIDERS order is alphabetical, matching the v8 prototype's
// fixed page rows.

import type { ModelProvider } from '../api/client';

export interface ProviderSchema {
  provider: ModelProvider;
  label: string;
  /** Help text under the dialog title. */
  blurb: string;
  /** The single `api_key`-shaped field (lands in the top-level
   *  `api_key` per OpenAPI). */
  apiKey: { label: string; placeholder: string; helperText?: string };
  /** Required extras — each lands in `extras[key]`; must be non-empty. */
  requiredExtras: {
    key: string;
    label: string;
    placeholder?: string;
    defaultValue?: string;
  }[];
  /** Optional extras — sent only when non-empty. */
  optionalExtras: { key: string; label: string; placeholder?: string }[];
}

export const PROVIDER_SCHEMAS: ProviderSchema[] = [
  {
    provider: 'anthropic',
    label: 'Anthropic',
    blurb: 'Anthropic Claude API key. Used directly by the Claude proxy.',
    apiKey: { label: 'API key', placeholder: 'sk-ant-…' },
    requiredExtras: [],
    optionalExtras: [],
  },
  {
    provider: 'bedrock',
    label: 'AWS Bedrock',
    blurb:
      'Bedrock long-term API key + region. The credential proxy authenticates to Bedrock with this key as a Bearer token.',
    apiKey: {
      label: 'Bedrock API key',
      placeholder: 'ABSK…',
      helperText:
        "Console → Bedrock → API keys → Generate long-term API key. Stored as the credential's primary key; the region lands in extras.",
    },
    requiredExtras: [
      { key: 'region', label: 'Region', placeholder: 'us-east-1', defaultValue: 'us-east-1' },
    ],
    optionalExtras: [],
  },
  {
    provider: 'google',
    label: 'Google',
    blurb: 'Google AI Studio / Gemini API key.',
    apiKey: { label: 'API key', placeholder: 'AI…' },
    requiredExtras: [],
    optionalExtras: [],
  },
  {
    provider: 'openai',
    label: 'OpenAI',
    blurb: 'OpenAI / compatible API. Override the base URL for self-hosted gateways.',
    apiKey: { label: 'API key', placeholder: 'sk-…' },
    requiredExtras: [],
    optionalExtras: [
      {
        key: 'api_base',
        label: 'API base URL (optional)',
        placeholder: 'https://api.openai.com/v1',
      },
    ],
  },
];

/** Fixed page rows (Lit `PROVIDERS` const) — the whole point of the
 *  page is showing providers that are NOT yet configured, so this is a
 *  constant, not derived from what exists. Alphabetical (prototype). */
export const PROVIDERS: ModelProvider[] = ['anthropic', 'bedrock', 'google', 'openai'];

export function schemaFor(provider: ModelProvider): ProviderSchema {
  const s = PROVIDER_SCHEMAS.find((x) => x.provider === provider);
  if (!s) throw new Error(`unknown provider: ${provider}`);
  return s;
}
