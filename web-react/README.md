# rolemesh-webui-react

Second SPA for RoleMesh — React 19 / Vite 8 / Tailwind 3, built against
the **same** OpenAPI contract (`contracts/openapi.yaml`) and v1 WS
protocol as the Lit SPA in `web/`. The Lit SPA stays the default; this
one is selected at deployment time:

```bash
WEB_UI_DIST=web-react/dist rolemesh-webui   # serve the React SPA
```

Spec: `rolemesh-webui-react-spec-handoff-v3.md` (chat prototype:
`rolemesh-webui-react-chat-prototype-v3.html`).

## Dev

```bash
npm install
npm run dev          # :5174, proxies /api + /oauth2 to :8080 (web/ dev uses :5173)
npm test             # vitest
npm run typecheck
npm run openapi:gen  # regenerate src/api/generated/types.ts after contract changes
```

Lint guardrails (run in CI):

```bash
npm run lint:tokens-only    # colors only via --rm-* tokens (styles/tokens.css)
npm run lint:flat-route     # settings routes stay flat under #/manage/{slug}
npm run lint:no-admin-chat  # no /api/admin/, chat<->settings isolation, lib/ react-free
```

## Layout (spec §1.1, feature-first)

- `src/app/` — assembly layer: router, providers, auth bootstrap, nav table, copy
- `src/api/` — typed v1 client + codegen output + TanStack Query hooks
- `src/ws/`, `src/lib/` — modules copied from `web/` (see `src/lib/PORTED.md`);
  `lib/` must never import react
- `src/features/chat/` — the chat surface (picker under `agent-picker/`)
- `src/features/settings/` — one folder per nav slug; stubs in this branch

## Scope (branch `feat/webui-react-chat`)

Phases 1–4 + OIDC login. Deferred to the next PR: HITL approval cards +
the safety-log page (a pending approval currently renders an inline
notice pointing at the classic UI). All settings entries are stubs.
