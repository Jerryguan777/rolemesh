// Copied from web/src/services/models-grouping.ts @ 5d3650e; keep in sync manually until workspace extraction.
// Provider-grouped model catalog projection.
//
// Single source of truth for "which providers have a credential and
// which models live under each" — consumed by both <rm-models-page>
// (read-only catalog) and the v2-B coworker wizard (model picker
// with inline credential unlock).
//
// Rules (v2-B locked):
//   - Groups are sorted by provider name (alphabetical).
//   - Models inside a group are sorted by `model_id` (alphabetical).
//   - When a `backend` is supplied, models whose `provider` is not
//     in `backend.supported_providers` are dropped entirely. If
//     `backend.supported_model_families` is non-null, only models
//     whose `model_family` is in that list survive. A `null` family
//     list means "any family the provider offers" (Pi today).
//   - Inactive models (`is_active=false`) are *kept* — the wizard
//     wants to surface them as disabled rather than hide them so an
//     operator can see what their tenant has access to. Callers that
//     want them hidden can post-filter on `is_active`.
//   - Empty provider groups (no models surviving the backend filter)
//     are dropped. They are not useful to surface.

import type {
  Backend,
  CredentialResponse,
  Model,
  ModelProvider,
} from '../api/client.js';

export interface ProviderGroup {
  provider: ModelProvider;
  hasCredential: boolean;
  /** ISO timestamp from the credential's `updated_at`, or null when
   *  the tenant has no credential for this provider. */
  credentialUpdatedAt: string | null;
  models: Model[];
}

export function groupModelsByProvider(
  models: readonly Model[],
  credentials: readonly CredentialResponse[],
  backend?: Backend | null,
): ProviderGroup[] {
  const credByProvider = new Map<ModelProvider, CredentialResponse>();
  for (const c of credentials) credByProvider.set(c.provider, c);

  const allowedProviders = backend
    ? new Set<ModelProvider>(backend.supported_providers)
    : null;
  // null `supported_model_families` means "any family" per OpenAPI.
  const allowedFamilies = backend?.supported_model_families
    ? new Set(backend.supported_model_families)
    : null;

  const buckets = new Map<ModelProvider, Model[]>();
  for (const m of models) {
    if (allowedProviders && !allowedProviders.has(m.provider)) continue;
    if (allowedFamilies && !allowedFamilies.has(m.model_family)) continue;
    let bucket = buckets.get(m.provider);
    if (!bucket) {
      bucket = [];
      buckets.set(m.provider, bucket);
    }
    bucket.push(m);
  }

  const providers = [...buckets.keys()].sort();
  return providers.map((provider) => {
    const cred = credByProvider.get(provider) ?? null;
    const bucket = buckets.get(provider)!;
    bucket.sort((a, b) => a.model_id.localeCompare(b.model_id));
    return {
      provider,
      hasCredential: cred !== null,
      credentialUpdatedAt: cred?.updated_at ?? null,
      models: bucket,
    };
  });
}
