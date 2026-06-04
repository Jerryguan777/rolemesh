// Coworker subtitle formatter — shared by the chat-shell sidebar
// switcher and the composer's coworker dropdown.
//
// The subtitle shows what users actually care about: which backend +
// which model is wired up to this coworker.
//
// Format:    "Claude · Claude Opus 4.7"
//            "Pi · GPT-4o"
//            "Claude"                       ← coworker.model_id is null
//                                              OR model lookup failed
//
// The separator is a middle-dot (`·`) — softer than the `|` the user
// drafted, matches v2 prototype's tone for hierarchical labels.
//
// Why null model_id renders as backend-only (no "(no model)" hint):
// most pre-v1.1 coworker rows have model_id=NULL even though chat
// works fine — the agent process reads PI_MODEL_ID from the
// environment in that case (see `agent/executor.py`), so "(no model)"
// would be misleading. Treat null as "unknown to the SPA" and let
// the backend keep its own resolution policy.

import type { BackendName, Coworker, Model } from '../api/client.js';

const BACKEND_LABEL: Record<BackendName, string> = {
  claude: 'Claude',
  pi: 'Pi',
};

/** Returns the display name of `backend`. Falls back to the raw enum
 *  when the value is something we have not labelled yet — a future
 *  third backend would print its slug here until the map is updated. */
export function backendLabel(backend: BackendName): string {
  return BACKEND_LABEL[backend] ?? String(backend);
}

/** Format a coworker's subtitle as "Backend · Model display_name".
 *  Passing `modelsById` is optional — without it (or when the model
 *  id is null), the function returns just the backend, never crashes
 *  on a missing lookup. */
export function coworkerSubtitle(
  c: Coworker,
  modelsById?: Map<string, Model>,
): string {
  const backend = backendLabel(c.agent_backend);
  if (!c.model_id) return backend;
  const model = modelsById?.get(c.model_id);
  if (!model) return backend;
  return `${backend} · ${model.display_name}`;
}

/** Build a fast lookup map keyed by `model.id` so render paths can
 *  resolve a coworker's model without a linear scan. */
export function modelsByIdMap(models: readonly Model[]): Map<string, Model> {
  const m = new Map<string, Model>();
  for (const model of models) m.set(model.id, model);
  return m;
}
