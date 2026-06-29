"""I. Network egress — gateway-plane attacks.

The third defense layer (after container hardening and the content-safety
pipeline) is the egress gateway: every outbound TCP/HTTP(S) attempt is
funneled through the forward proxy, and every raw DNS lookup through the
gateway's authoritative resolver. Both planes enforce a *positive*
allowlist — what is explicitly permitted, nothing else.

These tests drive the REAL gateway decision seams, not a re-implementation:

  * ``make_egress_domain_check()`` returns the exact async callable the
    gateway's ``safety_call`` aggregator invokes for the ``egress.domain_rule``
    check. It reports ``matched: bool``; the aggregator blocks a request
    when NO rule voted allow.
  * ``GlobalDnsPolicy`` is the platform DNS allowlist the gateway's DNS
    server consults.

Backed by docs/16-egress-control-architecture.md. The socket/CONNECT
plumbing is covered by ``tests/egress/``; this file is the attack-narrative
regression net over the policy contracts those sockets enforce.

Attacks:

  I1. Forward-proxy CONNECT to a non-allowlisted attacker host
      → no rule matches → gateway blocks.
  I2. Port smuggling: reach an allowlisted SNI on a non-allowlisted port
      (SSH on a host whose name matches ``*.github.com``)
      → port scoping refuses the match.
  I3. Malformed rule config must fail CLOSED, never accidentally allow.
  I4. Empty / missing host must not be waved through.
  I5. DNS enforcement mode is fail-closed and a typo'd mode kills boot.
"""

from __future__ import annotations

import pytest

from rolemesh.egress.dns_policy import ALLOWLIST_ENV, MODE_ENV, GlobalDnsPolicy
from rolemesh.egress.safety_call import EgressRequest
from rolemesh.safety.checks.egress_domain_rule import make_egress_domain_check


def _req(host: str, port: int) -> EgressRequest:
    """A forward-proxy CONNECT view — the host is agent-CONTROLLED, which is
    exactly why the gateway must re-decide it rather than trust it."""
    return EgressRequest(host=host, port=port, mode="forward", method="CONNECT")


# A tight, realistic operator allowlist: a couple of SaaS endpoints on 443.
_ALLOWLIST = {"domain_patterns": ["api.anthropic.com", "*.github.com"], "ports": [443]}


# ---------------------------------------------------------------------------
# I1. CONNECT to a non-allowlisted host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attacker_host",
    [
        "evil.attacker.com",
        "github.com.attacker.tld",  # allowlisted label as a LEFT prefix
        "api.anthropic.com.evil.example",  # exact name + attacker suffix
        "notgithub.com",  # suffix without the dot boundary
    ],
)
async def test_I1_connect_to_non_allowlisted_host_not_matched(
    attacker_host: str,
) -> None:
    """Attacker: point an HTTP client / curl at their own server to exfil.
    Defense: the gateway's egress.domain_rule reports no match for any host
    outside the allowlist, so the aggregator (allow iff some rule matched)
    blocks the CONNECT."""
    check = make_egress_domain_check()
    matched, findings = await check(_req(attacker_host, 443), _ALLOWLIST)
    assert matched is False, (
        f"attacker host {attacker_host!r} matched the allowlist — it would "
        "be tunneled out; suffix-confusion / over-broad match bug"
    )
    assert findings == []


# ---------------------------------------------------------------------------
# I2. Port smuggling — allowlisted name, non-allowlisted port
# ---------------------------------------------------------------------------


async def test_I2_allowlisted_name_on_wrong_port_not_matched() -> None:
    """Attacker: tunnel SSH (or any service) out via a host whose NAME is
    allowlisted (``raw.github.com``) but on port 22, hoping a name-only
    allowlist waves it through. Defense: ``ports`` scopes the rule, so a
    match on the name alone is not enough — the gateway docstring calls
    this exact bypass out ("how you accidentally whitelist the SSH service
    for an attacker with a matching SNI")."""
    check = make_egress_domain_check()

    # 443 — the allowed port — DOES match (false-positive control).
    ok_matched, _ = await check(_req("raw.github.com", 443), _ALLOWLIST)
    assert ok_matched is True, "legitimate raw.github.com:443 must be allowed"

    # 22 — outside ports=[443] — must NOT match even though the name does.
    smuggled, _ = await check(_req("raw.github.com", 22), _ALLOWLIST)
    assert smuggled is False, (
        "raw.github.com:22 matched a ports=[443] rule — port scoping is "
        "not enforced, SSH/arbitrary-service smuggling is possible"
    )


# ---------------------------------------------------------------------------
# I3. Malformed rule config must fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_config",
    [
        {},  # no domain_patterns
        {"domains": ["*.github.com"]},  # wrong key (plural typo)
        {"domain_patterns": []},  # empty — "match nothing", not "match all"
        {"domain_patterns": ["*.github.com"], "extra": True},  # extra='forbid'
        "*.github.com",  # not even a dict
    ],
)
async def test_I3_malformed_config_fails_closed(bad_config: object) -> None:
    """Attacker: corrupt an egress rule (via a REST typo or injection) hoping
    the gateway fails OPEN and allows everything. Defense: the adapter
    validates the config and on any failure reports no match — so a broken
    rule blocks rather than allows. We probe with a host a CORRECT rule
    would have allowed, to prove the broken config does not leak it."""
    check = make_egress_domain_check()
    matched, findings = await check(_req("raw.github.com", 443), bad_config)
    assert matched is False, (
        f"malformed config {bad_config!r} still matched raw.github.com — a "
        "bad rule must fail closed, never accidentally allow egress"
    )
    assert findings == []


# ---------------------------------------------------------------------------
# I4. Empty host
# ---------------------------------------------------------------------------


async def test_I4_empty_host_not_matched() -> None:
    """A blank host (truncated CONNECT line, parser edge) must not be treated
    as a match — otherwise an attacker who can elicit an empty host string
    gets a free allow."""
    check = make_egress_domain_check()
    matched, _ = await check(_req("", 443), _ALLOWLIST)
    assert matched is False, "empty host must never count as an allowlist match"


# ---------------------------------------------------------------------------
# I5. DNS plane is fail-closed; a typo'd mode kills the gateway at boot
# ---------------------------------------------------------------------------


def test_I5_dns_default_mode_is_enforce_and_empty() -> None:
    """The steady-state DNS posture is enforce + empty allowlist: a tripwire,
    not a service. A default that resolved anything would be a silent
    DNS-exfil channel."""
    policy = GlobalDnsPolicy()
    assert policy.mode == "enforce"
    assert policy.patterns == ()
    assert not policy.is_allowed("anything.example")


def test_I5_dns_mode_typo_fails_loud_at_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attacker-adjacent ops hazard: a typo'd ``EGRESS_DNS_MODE=observ`` must
    NOT silently fall back to whichever mode the typo is not — it must kill
    the gateway at boot. Fail-closed config, not fail-open."""
    monkeypatch.setenv(MODE_ENV, "observ")  # typo of "observe"
    monkeypatch.setenv(ALLOWLIST_ENV, "")
    with pytest.raises(ValueError):
        GlobalDnsPolicy.from_env()
