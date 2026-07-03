// Delete-failure message formatter (spec D.3). A 409 RESOURCE_IN_USE
// carries `details.coworker_ids` — render the count (Lit parity),
// never dump the raw open `details` object. Everything else falls back
// to the message. Pure so the 409-surfacing path is unit-testable.

import { ApiError } from '../../../api/client';

export function deleteErrText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 409 && err.body?.details) {
      const ids = (err.body.details as Record<string, unknown>).coworker_ids;
      if (Array.isArray(ids)) {
        return `In use by ${ids.length} coworker${ids.length === 1 ? '' : 's'} — unbind it from each before deleting.`;
      }
    }
    return err.body?.message ?? `${err.status}`;
  }
  return (err as Error).message ?? 'delete failed';
}
