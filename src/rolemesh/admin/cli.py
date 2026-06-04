"""``rolemesh-admin`` CLI — platform genesis + emergency escape hatch.

``create-admin`` seeds the first ``platform_admin`` on a fresh deploy —
the one privileged account that nobody can create through the UI yet
(zero users, no one to log in). This is the canonical production path
for seeding the initial admin, replacing the static
``ADMIN_BOOTSTRAP_TOKEN`` backdoor.

Every other privileged account already has a creator and does NOT need
this tool: further platform_admins are made by the first one in the UI;
tenant owner/admin/member come from the platform_admin UI/API or OIDC
JIT provisioning. ``--role`` keeps owner/admin/member available purely
as a zero-cost escape hatch (scripts / tests / lockout recovery).

Assumes infrastructure is up: PostgreSQL reachable via ``DATABASE_URL``
(and ``ADMIN_DATABASE_URL`` for the BYPASSRLS pool). The schema is
created idempotently on connect, so a brand-new database is fine.

Exit codes:
  0 — user created, or already present (idempotent)
  1 — configuration error (bad role / missing --tenant) or DB failure
"""

from __future__ import annotations

import argparse
import asyncio
import sys

# Side-effect import: runs load_env() so ``.env`` lands in os.environ
# before rolemesh.core.config captures DATABASE_URL. Mirrors the eval
# CLI / webui entrypoints.
import rolemesh.bootstrap  # noqa: F401
from rolemesh.admin.core import (
    ALLOWED_ROLES,
    PLATFORM_ADMIN_ROLE,
    AdminProvisionError,
    create_admin,
)
from rolemesh.core.config import DATABASE_URL


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rolemesh-admin",
        description="RoleMesh operational admin provisioning.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    ca = sub.add_parser(
        "create-admin",
        help="create/confirm a privileged user (default: platform_admin)",
    )
    ca.add_argument(
        "--email",
        required=True,
        help="login identifier the IdP is matched against (not a credential)",
    )
    ca.add_argument(
        "--role",
        default=PLATFORM_ADMIN_ROLE,
        choices=sorted(ALLOWED_ROLES),
        help=(
            "default platform_admin (platform genesis); owner/admin/member "
            "are an emergency escape hatch only"
        ),
    )
    ca.add_argument(
        "--external-sub",
        default=None,
        help="known IdP subject to bind now; else linked on first OIDC login",
    )
    ca.add_argument(
        "--name",
        default=None,
        help="display name (default: the email's local part)",
    )
    ca.add_argument(
        "--tenant",
        default=None,
        help="tenant slug — required for tenant-scoped roles only",
    )
    return p


async def _cmd_create_admin(args: argparse.Namespace) -> int:
    from rolemesh.db import close_database, init_database

    await init_database(DATABASE_URL)
    try:
        result = await create_admin(
            email=args.email,
            role=args.role,
            external_sub=args.external_sub,
            name=args.name,
            tenant=args.tenant,
        )
    finally:
        await close_database()

    verb = "Created" if result.created else "Already exists —"
    print(
        f"{verb} user {result.user_id} "
        f"(role={result.role}, tenant={result.tenant_slug})"
    )
    if result.external_sub:
        print(f"  external_sub bound: {result.external_sub}")
    else:
        print(
            "  No external_sub bound — the row links on the first OIDC login "
            f"whose subject matches email {result.email!r}."
        )
    print(f"  Login: sign in through your IdP as {result.email}.")
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    handlers = {"create-admin": _cmd_create_admin}
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover - argparse 'required' guards this
        parser.error(f"unknown command {args.command!r}")
        return 1

    try:
        return asyncio.run(handler(args))
    except AdminProvisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
