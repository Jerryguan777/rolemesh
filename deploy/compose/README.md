# RoleMesh compose stacks

The base stack (`compose.yaml`) runs RoleMesh with `AUTH_MODE=external` (the
default). An **optional** override (`compose.keycloak.yaml`) layers a Keycloak
OIDC IdP on top and switches auth to `AUTH_MODE=oidc` for end-to-end OIDC
testing (the foundation for promptfoo BOLA/BFLA/RBAC red-teaming).

The override is purely additive — leave it out and you get the original
external-auth stack unchanged.

---

## Keycloak / OIDC stack (dev & test only)

### What it provides

| Item | Value |
|------|-------|
| Realm | `rolemesh` |
| Client (confidential) | `rolemesh-web` |
| Client secret | `rolemesh-web-dev-secret` |
| Keycloak admin console | http://localhost:8081 (admin / admin) |
| Discovery (host) | http://localhost:8081/realms/rolemesh/.well-known/openid-configuration |
| Discovery (in-container) | http://keycloak:8080/realms/rolemesh/.well-known/openid-configuration |
| WebUI | http://localhost:8080 |

### Test users

All passwords are `Passw0rd!`. `tenant_id` is an opaque external id; RoleMesh
JIT-creates a local tenant `oidc-<tenant_id>` on first login, so `t1` and `t2`
become two isolated tenants with no pre-seeding.

| Username | Password | tenant_id | role | Purpose |
|----------|----------|-----------|------|---------|
| `owner@t1`  | `Passw0rd!` | `t1` | `owner`  | tenant 1 admin |
| `member@t1` | `Passw0rd!` | `t1` | `member` | tenant 1 low-priv (BFLA/RBAC tests) |
| `owner@t2`  | `Passw0rd!` | `t2` | `owner`  | tenant 2 (cross-tenant / BOLA tests) |

> `role` must be exactly `owner` / `admin` / `member`. RoleMesh's adapter
> rejects any other value and silently falls back to `member`. The
> platform-plane `platform_admin` role is **not** mappable via OIDC by design.

### Claim mapping (the make-or-break contract)

RoleMesh reads two **flat, top-level String claims** from the **ID token**. The
realm ships a User-Attribute protocol mapper (attached to the `rolemesh-web`
client) for each:

| User attribute (Keycloak) | Token claim | RoleMesh env | Consumed by |
|---------------------------|-------------|--------------|-------------|
| `tenant_id` | `tenant_id` | `OIDC_CLAIM_TENANT_ID=tenant_id` | `map_tenant_id` → tenant binding |
| `role`      | `role`      | `OIDC_CLAIM_ROLE=role`           | `map_role` → owner/admin/member |

Both mappers have **Add to ID token = on** because RoleMesh validates the
**id_token** (not the access_token — see "Token contract" below).

### Start it

From the repo root (the base `.env` keys — `WS_TICKET_SECRET`,
`ROLEMESH_HOST_DATA_DIR`, `DOCKER_GID`, `EGRESS_TOKEN_SECRET` — are still
required by the base stack):

```bash
docker compose --env-file .env \
  -f deploy/compose/compose.yaml \
  -f deploy/compose/compose.keycloak.yaml up -d
```

Wait ~20s for the realm import (`docker logs rolemesh-keycloak | grep Imported`),
then run the acceptance check:

```bash
deploy/compose/keycloak/e2e-verify.sh
```

It fetches a token per user, hits the protected `GET /api/v1/me`, asserts the
role/tenant claims mapped (t1 users share a tenant, t2 is separate), then
proves data-layer isolation: `owner@t1` creates a coworker, `owner@t1` sees it
in `GET /api/v1/coworkers`, `owner@t2` does not (and gets 404 fetching it by
id). Finally it asserts an access_token is rejected.

### Log in as a user (browser)

Open http://localhost:8080, click login, authenticate as e.g. `owner@t1` /
`Passw0rd!`. The SPA runs the PKCE flow against Keycloak and lands you in
tenant `oidc-t1` as an owner.

### Get a token programmatically (for promptfoo)

```bash
# Prints an id_token (NOT an access_token — see contract below).
deploy/compose/keycloak/get-token.sh owner@t1

# Use it as a bearer:
TOKEN=$(deploy/compose/keycloak/get-token.sh owner@t1)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/v1/me
```

Raw form (what a promptfoo custom provider does — note `scope=openid` and that
we read `.id_token`):

```bash
curl -s http://localhost:8081/realms/rolemesh/protocol/openid-connect/token \
  -d grant_type=password -d scope=openid \
  -d client_id=rolemesh-web -d client_secret=rolemesh-web-dev-secret \
  -d username=owner@t1 -d password='Passw0rd!' | jq -r .id_token
```

### 🔑 Token contract: use the **id_token**, not the access_token

RoleMesh's `OIDCAuthProvider.authenticate()` validates the bearer as an
**id_token** with `aud == client_id` (`rolemesh-web`). A Keycloak access_token
defaults to `aud=["account"]` and is rejected with **401**. Always send the
`id_token`. (This is consistent with RoleMesh's design: the IdP is trusted for
authentication only; all authorization is internal — see
`docs/6-auth-architecture.md`.)

### Switch back to external auth

Just drop the override file:

```bash
docker compose --env-file .env -f deploy/compose/compose.yaml up -d
```

---

## Gotchas baked into this stack

These are pre-solved in `compose.keycloak.yaml` / `rolemesh-realm.json`; listed
so you don't undo them:

- **Image is pinned to the exact patch `keycloak:25.0.6`** (not the floating
  `:25.0`). The hostname env vars use **v2 syntax** (`KC_HOSTNAME` = a full URL
  + `KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true`), which is the default on 25.0.6 /
  26.x. The older v1 options (`KC_HOSTNAME_PORT`,
  `KC_HOSTNAME_STRICT_BACKCHANNEL`) are silently ignored here — do not
  reintroduce them. Keep the tag and the syntax in lockstep when bumping.
- **`OIDC_DISCOVERY_URL` must stay `http://keycloak:8080/...`** (the in-network
  name). `KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true` makes Keycloak return
  backchannel endpoints (`jwks_uri`, `token_endpoint`) on the request host, so
  containers fetch keys from a reachable address while `iss` stays the fixed
  frontend URL. Pointing discovery at `localhost:8081` to "match iss" would
  make `jwks_uri` unreachable from the containers and break all validation.
- **Keycloak is on host port 8081** (8080 belongs to the WebUI).
- **`redirect_uri` points at the WebUI** (`localhost:8080/oauth2/callback`),
  **discovery points at Keycloak** — don't swap them.
- **`OIDC_COOKIE_SECURE=false`** for plain-HTTP dev, or the browser drops the
  refresh cookie.
- The client has **Direct Access Grants enabled** (ROPG) and **S256 PKCE
  forced**; the realm token lifespan is **30 min** so a long red-team run
  doesn't 401 mid-suite. All dev-only.
