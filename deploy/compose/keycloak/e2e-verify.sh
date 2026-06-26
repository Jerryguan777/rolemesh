#!/usr/bin/env bash
# End-to-end acceptance for the Keycloak OIDC integration. This is the real
# gate (pytest does not exercise a live IdP). It:
#   1. waits for the realm to be served,
#   2. fetches an id_token per test user via ROPG,
#   3. calls the protected GET /api/v1/auth/me and asserts the role + tenant
#      claims mapped correctly,
#   4. asserts cross-tenant isolation (t1 users share a tenant; t2 differs),
#   5. asserts an access_token is REJECTED (proves the id_token contract).
#
# Exit 0 = all green. Requires curl + jq. Run after `docker compose ... up -d`.
#
# Env overrides: KC_BASE_URL, ROLEMESH_BASE_URL, REALM, CLIENT_ID, CLIENT_SECRET
set -euo pipefail

KC_BASE_URL="${KC_BASE_URL:-http://localhost:8081}"
ROLEMESH_BASE_URL="${ROLEMESH_BASE_URL:-http://localhost:8080}"
REALM="${REALM:-rolemesh}"
CLIENT_ID="${CLIENT_ID:-rolemesh-web}"
CLIENT_SECRET="${CLIENT_SECRET:-rolemesh-web-dev-secret}"
PASSWORD="${PASSWORD:-Passw0rd!}"

TOKEN_URL="${KC_BASE_URL}/realms/${REALM}/protocol/openid-connect/token"
DISCOVERY_URL="${KC_BASE_URL}/realms/${REALM}/.well-known/openid-configuration"
ME_URL="${ROLEMESH_BASE_URL}/api/v1/auth/me"

pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }

# token <username> <id|access>  -> prints the requested token
token() {
  local user="$1" kind="$2"
  curl -sf "$TOKEN_URL" \
    -d grant_type=password -d scope=openid \
    -d client_id="$CLIENT_ID" -d client_secret="$CLIENT_SECRET" \
    -d username="$user" -d password="$PASSWORD" \
    | jq -r ".${kind}_token"
}

# me <id_token> -> prints the /me JSON (or fails the curl)
me() { curl -sf -H "Authorization: Bearer $1" "$ME_URL"; }

echo "==> 1. Waiting for Keycloak realm '${REALM}' ..."
for i in $(seq 1 60); do
  if curl -sf "$DISCOVERY_URL" >/dev/null 2>&1; then break; fi
  [ "$i" = 60 ] && fail "realm discovery never came up at ${DISCOVERY_URL}"
  sleep 2
done
pass "discovery reachable at ${DISCOVERY_URL}"

echo "==> 2/3. Tokens + claim mapping via GET /api/v1/auth/me"
OWNER_T1=$(me "$(token owner@t1 id)")
MEMBER_T1=$(me "$(token member@t1 id)")
OWNER_T2=$(me "$(token owner@t2 id)")

r_owner_t1=$(jq -r .role <<<"$OWNER_T1")
r_member_t1=$(jq -r .role <<<"$MEMBER_T1")
r_owner_t2=$(jq -r .role <<<"$OWNER_T2")
[ "$r_owner_t1"  = owner  ] || fail "owner@t1 role=$r_owner_t1 (want owner)"
[ "$r_member_t1" = member ] || fail "member@t1 role=$r_member_t1 (want member)"
[ "$r_owner_t2"  = owner  ] || fail "owner@t2 role=$r_owner_t2 (want owner)"
pass "role claim mapped: owner@t1=owner, member@t1=member, owner@t2=owner"

t_owner_t1=$(jq -r .tenant_id <<<"$OWNER_T1")
t_member_t1=$(jq -r .tenant_id <<<"$MEMBER_T1")
t_owner_t2=$(jq -r .tenant_id <<<"$OWNER_T2")
[ -n "$t_owner_t1" ] && [ "$t_owner_t1" != null ] || fail "owner@t1 has empty tenant_id (claim mapping broken)"

echo "==> 4. Cross-tenant isolation"
[ "$t_owner_t1" = "$t_member_t1" ] || fail "t1 users in different tenants ($t_owner_t1 vs $t_member_t1)"
pass "owner@t1 and member@t1 share tenant $t_owner_t1"
[ "$t_owner_t1" != "$t_owner_t2" ] || fail "t1 and t2 collapsed into one tenant ($t_owner_t1)"
pass "owner@t2 is a separate tenant $t_owner_t2 (cross-tenant boundary holds)"

echo "==> 5. Negative: access_token must be rejected (id_token-only contract)"
ACCESS_T1=$(token owner@t1 access)
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $ACCESS_T1" "$ME_URL")
[ "$code" = 401 ] || fail "access_token unexpectedly accepted (HTTP $code; want 401)"
pass "access_token rejected with 401 as expected"

echo
printf '\033[32mAll checks passed.\033[0m RoleMesh is authenticating against Keycloak in oidc mode.\n'
