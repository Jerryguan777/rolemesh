#!/usr/bin/env bash
# End-to-end acceptance for the Keycloak OIDC integration. This is the real
# gate (pytest does not exercise a live IdP). It:
#   1. waits for the realm to be served,
#   2. fetches an id_token per test user via ROPG,
#   3. calls the protected GET /api/v1/me and asserts the role + tenant claims
#      mapped correctly,
#   4. asserts identity-layer cross-tenant separation (t1 users share a tenant;
#      t2 differs),
#   5. asserts DATA-layer (RLS) isolation: owner@t1 creates a coworker, owner@t1
#      lists it, owner@t2 cannot see it and gets 404 fetching it by id,
#   6. asserts an access_token is REJECTED (proves the id_token contract).
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
API="${ROLEMESH_BASE_URL}/api/v1"

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

# api <method> <token> <path> [json-body]  -> prints "<http_code>\n<body>"
# Uses -s (not -f) so we can assert on status codes including 401/404.
api() {
  local method="$1" tok="$2" path="$3" body="${4:-}"
  if [ -n "$body" ]; then
    curl -s -w '\n%{http_code}' -X "$method" \
      -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
      -d "$body" "${API}${path}"
  else
    curl -s -w '\n%{http_code}' -X "$method" \
      -H "Authorization: Bearer $tok" "${API}${path}"
  fi
}
code() { printf '%s' "$1" | tail -n1; }      # last line = http code
json() { printf '%s' "$1" | sed '$d'; }      # everything but last line = body

echo "==> 1. Waiting for Keycloak realm '${REALM}' ..."
for i in $(seq 1 60); do
  if curl -sf "$DISCOVERY_URL" >/dev/null 2>&1; then break; fi
  [ "$i" = 60 ] && fail "realm discovery never came up at ${DISCOVERY_URL}"
  sleep 2
done
pass "discovery reachable at ${DISCOVERY_URL}"

echo "==> 2. Fetch id_tokens (ROPG)"
ID_OWNER_T1=$(token owner@t1 id)
ID_MEMBER_T1=$(token member@t1 id)
ID_OWNER_T2=$(token owner@t2 id)
[ -n "$ID_OWNER_T1" ] && [ "$ID_OWNER_T1" != null ] || fail "no id_token for owner@t1 (is directAccessGrants enabled?)"
pass "id_tokens issued for all three users"

echo "==> 3. Claim mapping via GET /api/v1/me"
ME_OWNER_T1=$(json "$(api GET "$ID_OWNER_T1" /me)")
ME_MEMBER_T1=$(json "$(api GET "$ID_MEMBER_T1" /me)")
ME_OWNER_T2=$(json "$(api GET "$ID_OWNER_T2" /me)")

[ "$(jq -r .role <<<"$ME_OWNER_T1")"  = owner  ] || fail "owner@t1 role != owner (claim mapping broken)"
[ "$(jq -r .role <<<"$ME_MEMBER_T1")" = member ] || fail "member@t1 role != member"
[ "$(jq -r .role <<<"$ME_OWNER_T2")"  = owner  ] || fail "owner@t2 role != owner"
pass "role claim mapped: owner@t1=owner, member@t1=member, owner@t2=owner"

T1=$(jq -r .tenant_id <<<"$ME_OWNER_T1")
T1_MEMBER=$(jq -r .tenant_id <<<"$ME_MEMBER_T1")
T2=$(jq -r .tenant_id <<<"$ME_OWNER_T2")
[ -n "$T1" ] && [ "$T1" != null ] || fail "owner@t1 tenant_id empty (tenant claim mapping broken)"

echo "==> 4. Identity-layer cross-tenant separation"
[ "$T1" = "$T1_MEMBER" ] || fail "t1 users in different tenants ($T1 vs $T1_MEMBER)"
pass "owner@t1 and member@t1 share tenant $T1"
[ "$T1" != "$T2" ] || fail "t1 and t2 collapsed into one tenant ($T1)"
pass "owner@t2 is a separate tenant $T2"

echo "==> 5. Data-layer (RLS) isolation via /api/v1/coworkers"
# Unique-ish folder per run so a re-run doesn't collide on UNIQUE(tenant_id,folder).
FOLDER="e2e-iso-$$"
# agent_backend is a required CoworkerCreate field (Literal["claude","pi"]) and
# the schema is extra="forbid", so name+folder alone returns 422.
CREATE=$(api POST "$ID_OWNER_T1" /coworkers "{\"name\":\"e2e-iso\",\"folder\":\"${FOLDER}\",\"agent_backend\":\"claude\"}")
[ "$(code "$CREATE")" = 201 ] || fail "owner@t1 create coworker -> $(code "$CREATE") (want 201); body: $(json "$CREATE")"
CW_ID=$(jq -r .id <<<"$(json "$CREATE")")
pass "owner@t1 created coworker $CW_ID"

# Ensure cleanup even if a later assertion fails.
cleanup() { api DELETE "$ID_OWNER_T1" "/coworkers/${CW_ID}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

LIST_T1=$(api GET "$ID_OWNER_T1" /coworkers)
[ "$(code "$LIST_T1")" = 200 ] || fail "owner@t1 list coworkers -> $(code "$LIST_T1") (want 200)"
jq -e --arg id "$CW_ID" '.items[]? | select(.id==$id)' <<<"$(json "$LIST_T1")" >/dev/null \
  || fail "owner@t1 cannot see its own coworker $CW_ID in the list"
pass "owner@t1 sees its coworker in GET /coworkers (tenant data reachable, not empty)"

LIST_T2=$(api GET "$ID_OWNER_T2" /coworkers)
[ "$(code "$LIST_T2")" = 200 ] || fail "owner@t2 list coworkers -> $(code "$LIST_T2") (want 200)"
if jq -e --arg id "$CW_ID" '.items[]? | select(.id==$id)' <<<"$(json "$LIST_T2")" >/dev/null; then
  fail "CROSS-TENANT LEAK: owner@t2 sees owner@t1's coworker $CW_ID"
fi
pass "owner@t2 does NOT see owner@t1's coworker (RLS cross-tenant isolation holds)"

GET_T2=$(api GET "$ID_OWNER_T2" "/coworkers/${CW_ID}")
[ "$(code "$GET_T2")" = 404 ] || fail "owner@t2 GET t1's coworker by id -> $(code "$GET_T2") (want 404 BOLA)"
pass "owner@t2 GET t1's coworker by id -> 404 (direct-object BOLA blocked)"

echo "==> 6. Negative: access_token must be rejected (id_token-only contract)"
ACCESS_T1=$(token owner@t1 access)
RESP=$(api GET "$ACCESS_T1" /me)
[ "$(code "$RESP")" = 401 ] || fail "access_token unexpectedly accepted ($(code "$RESP"); want 401)"
pass "access_token rejected with 401 as expected"

echo
printf '\033[32mAll checks passed.\033[0m RoleMesh is authenticating against Keycloak in oidc mode.\n'
