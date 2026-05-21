#!/usr/bin/env bash
# Phase 0 smoke — Verifies the four INV foundations end-to-end:
#   INV-2  IPC dataclass deserialization ignores unknown fields.
#   INV-3  Container cleanup_orphans honors the image whitelist.
#   INV-4  Audit actor resolution returns 503 on ownerless bootstrap.
#   INV-5  SKILL.md filename constant is the single source of truth.
# Plus a sanity check that the BOOTSTRAP_USERS multi-user fast-path
# (§5.2.1) works end-to-end.
#
# Local-only: no external LLM calls. Pre-reqs:
#   - docker (for testcontainer + foreign-container check)
#   - `uv` (for running pytest) — installed in the project root.
#
# Usage:
#   bash scripts/smoke_bootstrap.sh
#
# Exit code:
#   0 — every check passed
#   non-zero — at least one check failed (the offender name is printed).

set -u

cd "$(dirname "$0")/.."

PASS=0
FAIL=0
FAILED_CHECKS=()

_pass() { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
_fail() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); FAILED_CHECKS+=("$1"); }

run_pytest_pinned_file() {
    local label="$1"
    local file="$2"
    if uv run pytest "$file" -q >/tmp/smoke_bootstrap_$$.out 2>&1; then
        _pass "$label"
    else
        _fail "$label"
        echo "    --- output ---"
        sed 's/^/    /' /tmp/smoke_bootstrap_$$.out | tail -n 30
        echo "    --- end ---"
    fi
    rm -f /tmp/smoke_bootstrap_$$.out
}

echo "=== Phase 0 smoke ==="
echo

# Pre-flight env check (Phase 2, INV-VAULT-1). The webui/orchestrator
# both fail-loud at boot if ``CREDENTIAL_VAULT_KEY`` is unset; surface
# that here so the smoke script flags it before running anything that
# depends on the credential vault — otherwise the failure shows up
# inside an opaque pytest stacktrace.
if [[ -z "${CREDENTIAL_VAULT_KEY:-}" ]]; then
    _fail "CREDENTIAL_VAULT_KEY unset (set to e.g. \`openssl rand -base64 32\`)"
else
    _pass "CREDENTIAL_VAULT_KEY present"
fi

# Pinned tests (each maps to one INV plus PR-7's BOOTSTRAP_USERS).
echo "Pinned suites:"
run_pytest_pinned_file "INV-5 SKILL.md constant"           tests/test_skill_manifest_constant.py
run_pytest_pinned_file "INV-2 IPC unknown-keys filter"     tests/test_ipc_forward_compat_ignores_unknown_fields.py
run_pytest_pinned_file "INV-3 cleanup_orphans whitelist"   tests/test_container_cleanup_image_whitelist.py
run_pytest_pinned_file "INV-4 audit actor resolution"      tests/test_audit_actor_resolution.py
run_pytest_pinned_file "BOOTSTRAP_USERS multi-user map"    tests/test_bootstrap_multi_user.py
run_pytest_pinned_file "Backends API"                      tests/test_backend_capabilities.py
# OpenAPI codegen freshness is best-effort here: the test self-skips
# if `web/node_modules` was not bootstrapped on this machine, so the
# overall smoke still passes for hands-off envs. The contract test
# next to it has no node dependency and always runs.
run_pytest_pinned_file "OpenAPI yaml/ts freshness"         tests/test_openapi_codegen_freshness.py
run_pytest_pinned_file "OpenAPI yaml/Python contract"      tests/test_openapi_contract.py

# Live foreign-container check for INV-3 — only run when dockerd is
# reachable; otherwise skip (the pinned test above already covers
# the logic).
echo
echo "Live INV-3 foreign-container sanity check:"
if docker info >/dev/null 2>&1; then
    name="smoke-foreign-not-rolemesh-$$"
    if docker run --rm --name "$name" -d alpine:3.18 sleep 30 >/dev/null 2>&1; then
        if uv run python - <<PY >/tmp/smoke_inv3_$$.out 2>&1
import asyncio
from rolemesh.container.docker_runtime import DockerRuntime

async def run() -> None:
    rt = DockerRuntime()
    await rt.ensure_available()
    # NB: ``smoke-`` prefix matches the test container's name.
    # The whitelist only contains an image we know doesn't match
    # alpine, so the foreign container must survive.
    await rt.cleanup_orphans(
        "smoke-", allowed_images=frozenset({"rolemesh-agent:latest"})
    )
    await rt.close()

asyncio.run(run())
PY
        then
            if docker ps --filter "name=$name" --format '{{.Names}}' | grep -q "$name"; then
                _pass "foreign container preserved by cleanup_orphans"
            else
                _fail "foreign container was killed by cleanup_orphans"
            fi
        else
            _fail "cleanup_orphans script errored"
            echo "    --- output ---"
            sed 's/^/    /' /tmp/smoke_inv3_$$.out | tail -n 20
            echo "    --- end ---"
        fi
        rm -f /tmp/smoke_inv3_$$.out
        docker stop "$name" >/dev/null 2>&1 || true
    else
        echo "  [SKIP] could not start ephemeral alpine container"
    fi
else
    echo "  [SKIP] docker daemon not reachable; pinned test covers the logic"
fi

echo
echo "=== Summary ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"
if [[ $FAIL -gt 0 ]]; then
    echo
    echo "Failed checks:"
    for c in "${FAILED_CHECKS[@]}"; do
        echo "  - $c"
    done
    exit 1
fi

exit 0
