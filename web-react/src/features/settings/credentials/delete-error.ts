// Credential delete-failure formatter (spec G.4; Lit parity with
// credentials-page.ts errMessage). There is NO client-side pre-block —
// nothing on the wire counts consumers — but DELETE /credentials/{p}
// returns 409 RESOURCE_IN_USE with `details.coworker_ids` when a
// coworker still references the provider. Surface the count per-row
// (the spec's G.4 "confirm copy carries the weight" wording omits this;
// the wire + Lit make it authoritative). Pure so it is unit-testable.

import { ApiError } from '../../../api/client';

export function credDeleteErrText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 409 && err.body?.details) {
      const ids = (err.body.details as Record<string, unknown>).coworker_ids;
      if (Array.isArray(ids)) {
        return `This credential is in use by ${ids.length} coworker(s). Detach them before deleting.`;
      }
    }
    return err.body?.message ?? `${err.status}`;
  }
  return (err as Error).message ?? 'delete failed';
}
