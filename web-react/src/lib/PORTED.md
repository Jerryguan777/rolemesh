# Copied-module manifest

v1 policy (spec §11): **copy, don't share.** Each file below is a
byte-for-byte copy of its `web/` source plus a one-line provenance
header. Keep them in sync manually until workspace extraction.

| web-react path | source (web/src/…) | copied @ |
|---|---|---|
| `lib/capabilities.ts` (+test) | `auth/capabilities.ts` | cf6b0f1 |
| `lib/coworker-label.ts` (+test) | `services/coworker-label.ts` | cf6b0f1 |
| `lib/oidc-auth.ts` | `services/oidc-auth.ts` | cf6b0f1; re-synced on this branch (origin-derived redirect_uri fix — both copies changed together) |
| `ws/v1_client.ts` (+test) | `ws/v1_client.ts` | cf6b0f1 |
| `ws/ws-client-base.ts` (+test) | `ws/ws-client-base.ts` | cf6b0f1 |
| `ws/connection-state.ts` (+test) | `ws/connection-state.ts` | cf6b0f1 |
| `lib/models-grouping.ts` (+test) | `services/models-grouping.ts` | 5d3650e |

Ported (adapted, not verbatim — source noted in the file header):

- `api/client.ts` — trimmed to the §10.1 chat surface; method names
  kept identical to `web/src/api/client.ts`.
- `app/legacy-redirects.ts` — the `LEGACY_REDIRECTS` map + hash
  resolution from `web/src/router.ts` (router itself is
  react-router-dom here).
- `lib/conversation-summary.ts` — the `ConversationSummary` interface
  extracted from `web/src/components/sidebar.ts` (no date grouping in
  this design).

Rules for this directory (`lib/`): pure TS only, **no react imports**
(enforced by `scripts/lint-no-admin-chat.mjs` rule 3).
