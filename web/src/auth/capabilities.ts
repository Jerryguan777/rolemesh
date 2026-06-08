// Capability gating — the ONLY mechanism the SPA uses to decide which
// affordances a user may see (spec §7.1). The SPA never keeps its own
// role -> capability matrix; the backend's permissions.py is the single
// source of truth and the wire (`GET /api/v1/me`) carries the resolved
// `capabilities` list. Every render path reads from the cache below.
//
// `currentMe` is named deliberately to NOT collide with the ASYNC server
// fetch `api.getMe()`:
//   - api.getMe()  — async, hits /api/v1/me, called ONCE at app boot.
//   - currentMe()  — sync, reads the module cache, called everywhere else.
// Confusing the two is the "everything renders empty because me is null"
// failure mode the bootstrap refactor (app.ts §7.2) exists to prevent.

import type { Me } from '../api/client.js';

// Module-level cache. Held in a plain `let` (no persistence, no storage).
// Writing here does NOT trigger any Lit reactivity, which is exactly why
// <rm-app> must keep `authState` at 'loading' until setMe() has run — see
// the atomic-bootstrap contract in app.ts resolveAuth().
let _me: Me | null = null;

/** Setter — called once at app boot by <rm-app> after the async
 *  api.getMe() call resolves. NOT called from any render path. */
export function setMe(me: Me | null): void {
  _me = me;
}

/** Synchronous cache reader — every render path uses this to gate UI. */
export function currentMe(): Me | null {
  return _me;
}

/** True iff the resolved Me carries `action` in its capability list.
 *  False (fail-closed) whenever no Me is cached yet. */
export function hasCapability(action: string): boolean {
  return !!_me && _me.capabilities.includes(action);
}

/** Three-value-safe ownership check mirroring the backend's
 *  `require_manage_or_owner` ownership escape. A null/undefined
 *  `created_by_user_id` is a platform-default (system-created) resource
 *  and NEVER qualifies as owned — matching the SQL `col = :uid` semantics
 *  where NULL never compares equal. */
export function isOwnResource(resource: {
  created_by_user_id?: string | null;
}): boolean {
  return (
    !!_me &&
    !!resource.created_by_user_id &&
    resource.created_by_user_id === _me.user_id
  );
}

/** Frontend mirror of the backend ownership escape: a user may manage a
 *  resource if they hold the manage capability OR they own it. UX courtesy
 *  only — the backend still enforces; this just avoids rendering an Edit
 *  button that 403s on click (spec §7.3). */
export function canManage(
  resource: { created_by_user_id?: string | null },
  manageAction: string,
): boolean {
  return hasCapability(manageAction) || isOwnResource(resource);
}
