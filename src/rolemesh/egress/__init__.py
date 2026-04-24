"""Egress Control — network-layer gateway for agent outbound traffic.

EC-1 wires the gateway container and network topology (this PR). EC-2
adds the forward proxy (CONNECT), DNS resolver, identity map, and
Safety pipeline integration. EC-3 registers the egress_domain_rule
check and Stage.EGRESS_REQUEST.

Module boundaries:

    gateway.py    — container ENTRYPOINT module. Runs inside the
                    gateway container, binds listeners, wires the
                    safety pipeline, reads .env secrets.
    launcher.py   — orchestrator-side. Creates and starts the gateway
                    container attached to both the internal agent
                    bridge and the egress bridge.

Anything that could be mistaken for a second implementation of the
reverse-proxy logic during PR-1 lives in
``rolemesh.security.credential_proxy`` and stays there. PR-2 migrates
the business into ``rolemesh.egress.reverse_proxy`` and makes
``credential_proxy`` a thin re-export.
"""
