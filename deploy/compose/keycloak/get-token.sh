#!/usr/bin/env bash
# Fetch an id_token for a RoleMesh test user from Keycloak via the Resource
# Owner Password Grant (ROPG). This is what a promptfoo custom provider uses to
# authenticate programmatically.
#
# IMPORTANT: prints the id_token, NOT the access_token. RoleMesh validates the
# bearer as an id_token (aud == client_id); a Keycloak access_token has
# aud=["account"] and would be rejected with 401.
#
# Usage:
#   ./get-token.sh                 # owner@t1 by default
#   ./get-token.sh member@t1
#   ./get-token.sh owner@t2 'Passw0rd!'
#
# Env overrides: KC_BASE_URL, REALM, CLIENT_ID, CLIENT_SECRET
set -euo pipefail

USER_NAME="${1:-owner@t1}"
PASSWORD="${2:-Passw0rd!}"
KC_BASE_URL="${KC_BASE_URL:-http://localhost:8081}"
REALM="${REALM:-rolemesh}"
CLIENT_ID="${CLIENT_ID:-rolemesh-web}"
CLIENT_SECRET="${CLIENT_SECRET:-rolemesh-web-dev-secret}"

curl -sf "${KC_BASE_URL}/realms/${REALM}/protocol/openid-connect/token" \
  -d grant_type=password \
  -d scope=openid \
  -d client_id="${CLIENT_ID}" \
  -d client_secret="${CLIENT_SECRET}" \
  -d username="${USER_NAME}" \
  -d password="${PASSWORD}" \
  | jq -r .id_token
