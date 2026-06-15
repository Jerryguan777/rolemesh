# Security Policy

RoleMesh is a multi-tenant agent platform that handles real company data and
credentials, so we take security seriously. Thank you for helping keep RoleMesh
and its users safe.

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
pull requests, or discussions.**

Instead, report them privately using one of the following:

- GitHub's [private vulnerability reporting](https://github.com/jerryguan777/rolemesh/security/advisories/new)
  (preferred), or
- Email the maintainer at **jerryguan777@gmail.com** with the subject line
  `SECURITY: <short description>`.

Please include as much of the following as you can:

- A description of the vulnerability and its impact.
- Steps to reproduce (proof-of-concept, affected component, configuration).
- The version, commit, or deployment mode affected.
- Any suggested mitigation, if you have one.

## What to expect

- We aim to acknowledge your report within a few business days.
- We will keep you informed as we investigate and work on a fix.
- We will coordinate a disclosure timeline with you and credit you for the
  finding unless you prefer to remain anonymous.

## Scope

Security-relevant areas include, but are not limited to:

- Tenant isolation (Postgres Row-Level Security, the dual-pool model).
- The container sandbox and hardening (`docs/14-container-hardening-architecture.md`).
- The egress gateway and credential proxy (`docs/16-egress-control-architecture.md`).
- The safety policy engine (`docs/15-safety-framework-architecture.md`).
- Authentication and authorization (`docs/6-auth-architecture.md`).

## Supported versions

RoleMesh is under active development. Security fixes are applied to the `main`
branch. Until a formal release process is in place, please track `main` for the
latest fixes.
