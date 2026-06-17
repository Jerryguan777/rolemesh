"""Internal-name exemption derivation from the gateway's resolv.conf.

The security-critical property under test: a name is exempt from the DNS
allowlist iff the platform's own resolver is authoritative for it and will
not forward it to a public upstream. Get the boundary wrong in either
direction and you have a regression — too narrow breaks ``nats`` (the bus
is unreachable), too wide reopens the DNS-exfil channel the allowlist
exists to close. The tests therefore lead with the adversarial cases:
names crafted to *look* internal, and the runtime asymmetry on bare
single labels.
"""

from __future__ import annotations

import textwrap

from rolemesh.egress.dns_internal import (
    DOCKER_EMBEDDED_DNS,
    ResolvConf,
    build_internal_exemption,
    parse_resolv_conf,
)

# Representative resolv.conf bodies. K8s pods get the cluster search list +
# a kube-dns ClusterIP; Docker containers get the embedded DNS and no
# (useful) search domain.
_K8S = textwrap.dedent(
    """
    nameserver 10.96.0.10
    search rolemesh.svc.cluster.local svc.cluster.local cluster.local
    options ndots:5
    """
)
_DOCKER = f"nameserver {DOCKER_EMBEDDED_DNS}\n"
# A real dev-box gateway: embedded DNS + the HOST's inherited ISP search
# domain. The host domain must never become an allowlist exemption.
_DOCKER_DEV = f"nameserver {DOCKER_EMBEDDED_DNS}\nsearch hsd1.ca.comcast.net\n"
# A cluster that also injects a corp search domain alongside cluster.local.
_K8S_WITH_HOST = "nameserver 10.96.0.10\nsearch rolemesh.svc.cluster.local cluster.local corp.example.com\n"


def _matcher(text: str):
    exemption = build_internal_exemption(parse_resolv_conf(text))
    assert exemption is not None, f"expected an exemption from:\n{text}"
    is_internal, upstreams = exemption
    return is_internal, upstreams


# --------------------------------------------------------------------------
# parse_resolv_conf
# --------------------------------------------------------------------------


def test_parse_collects_nameservers_in_order() -> None:
    resolv = parse_resolv_conf("nameserver 10.0.0.1\nnameserver 10.0.0.2\n")
    assert resolv.nameservers == ("10.0.0.1", "10.0.0.2")


def test_parse_last_search_line_wins_like_libc() -> None:
    # glibc keeps only the final search directive, not the union.
    resolv = parse_resolv_conf("search first.local\nsearch a.local b.local\n")
    assert resolv.search == ("a.local", "b.local")


def test_parse_treats_domain_directive_as_search() -> None:
    resolv = parse_resolv_conf("domain corp.example.\n")
    assert resolv.search == ("corp.example",)


def test_parse_ignores_comments_and_blank_lines() -> None:
    resolv = parse_resolv_conf("# a comment\n; another\n\nnameserver 10.0.0.1\n")
    assert resolv.nameservers == ("10.0.0.1",)
    assert resolv.search == ()


# --------------------------------------------------------------------------
# Adversarial: names crafted to look internal must NOT be exempt
# --------------------------------------------------------------------------


def test_suffix_spoof_with_internal_as_prefix_is_external() -> None:
    # The classic exfil dressing: put the authoritative suffix early and
    # the attacker apex last. Must be evaluated as external (-> allowlist).
    is_internal, _ = _matcher(_K8S)
    assert is_internal("cluster.local.attacker.com") is False
    assert is_internal("nats.svc.cluster.local.evil.example") is False


def test_label_glued_to_suffix_without_a_dot_is_external() -> None:
    # "evilcluster.local" shares letters with "cluster.local" but is not a
    # dotted subdomain of it — must not be exempt.
    is_internal, _ = _matcher(_K8S)
    assert is_internal("evilcluster.local") is False


def test_external_apex_is_external_under_k8s() -> None:
    is_internal, _ = _matcher(_K8S)
    assert is_internal("api.anthropic.com") is False
    assert is_internal("secret-payload.evil.test") is False


def test_inherited_host_search_domain_is_not_internal() -> None:
    # REGRESSION: a Docker gateway inherits the host's ISP search domain.
    # Treating it as internal would forward `secret.<host-domain>` to the
    # host resolver, bypassing the allowlist tripwire. It must stay
    # external; only the single-label container path remains internal.
    is_internal, _ = _matcher(_DOCKER_DEV)
    assert is_internal("secret-payload.hsd1.ca.comcast.net") is False
    assert is_internal("nats") is True


def test_non_cluster_suffix_filtered_even_behind_kube_dns() -> None:
    # A cluster injecting a corp domain alongside cluster.local: only the
    # cluster domain is authoritative-local, so corp names stay external.
    is_internal, _ = _matcher(_K8S_WITH_HOST)
    assert is_internal("nats.rolemesh.svc.cluster.local") is True
    assert is_internal("intranet.corp.example.com") is False


# --------------------------------------------------------------------------
# Runtime asymmetry on bare single labels (the heart of the design)
# --------------------------------------------------------------------------


def test_bare_single_label_is_internal_on_docker() -> None:
    # Docker embedded DNS answers `nats` locally; a single label has no
    # public delegation, so treating it as internal cannot leak.
    is_internal, _ = _matcher(_DOCKER)
    assert is_internal("nats") is True
    assert is_internal("egress-gateway") is True


def test_bare_single_label_is_external_on_k8s() -> None:
    # kube-dns WOULD forward a bare single label to its own upstream, so on
    # K8s a single label must stay on the allowlist path (fail-closed).
    # Short internal names arrive as FQDNs anyway, via ndots:5 search.
    is_internal, _ = _matcher(_K8S)
    assert is_internal("nats") is False


def test_dotted_external_name_is_external_on_docker() -> None:
    # The embedded DNS forwards multi-label unknowns to the host resolver,
    # so a dotted name must NOT be exempt — it goes through the allowlist.
    is_internal, _ = _matcher(_DOCKER)
    assert is_internal("secret.attacker.com") is False


# --------------------------------------------------------------------------
# Internal names that MUST resolve
# --------------------------------------------------------------------------


def test_cluster_local_names_are_internal_on_k8s() -> None:
    is_internal, _ = _matcher(_K8S)
    assert is_internal("nats.rolemesh.svc.cluster.local") is True
    assert is_internal("egress-gateway.rolemesh.svc.cluster.local") is True
    # Cross-namespace name matches the broad `cluster.local` suffix.
    assert is_internal("postgres.other.svc.cluster.local") is True
    # The bare apex itself.
    assert is_internal("cluster.local") is True


def test_matching_is_case_insensitive_and_dot_tolerant() -> None:
    is_internal, _ = _matcher(_K8S)
    assert is_internal("NATS.Rolemesh.SVC.Cluster.Local") is True
    # Fully-qualified form with the root dot.
    assert is_internal("nats.rolemesh.svc.cluster.local.") is True


def test_internal_upstream_is_the_resolv_nameserver() -> None:
    _, k8s_up = _matcher(_K8S)
    assert [(u.host, u.port) for u in k8s_up] == [("10.96.0.10", 53)]
    _, docker_up = _matcher(_DOCKER)
    assert [(u.host, u.port) for u in docker_up] == [(DOCKER_EMBEDDED_DNS, 53)]


# --------------------------------------------------------------------------
# No exemption => fail-closed (None), never a blanket forward
# --------------------------------------------------------------------------


def test_no_nameserver_yields_no_exemption() -> None:
    assert build_internal_exemption(ResolvConf(search=("cluster.local",))) is None


def test_ipv6_only_nameserver_yields_no_exemption() -> None:
    # The forwarder is AF_INET only; an unusable upstream must not produce
    # an exemption that then SERVFAILs every internal name.
    resolv = parse_resolv_conf("nameserver fd00::a\nsearch cluster.local\n")
    assert build_internal_exemption(resolv) is None


def test_kube_dns_without_search_does_not_blanket_forward() -> None:
    # A kube-dns-like nameserver with no search list and no embedded-DNS
    # marker must NOT enable the exemption — otherwise every name would be
    # forwarded to a resolver that recurses to the public internet.
    resolv = parse_resolv_conf("nameserver 10.96.0.10\n")
    assert build_internal_exemption(resolv) is None
