// Coworker subtitle formatter — shared by the chat-shell sidebar
// switcher and the composer's coworker dropdown.
//
// Before v2-C: the subtitle was the AgentRole enum string (`agent` /
// `super_agent`). v2-A locked agent_role as "do not surface to users"
// (it's an internal A2A orchestration concept), so showing it was
// always a UX accident. v2-C replaces it with what users actually
// care about: which backend + which model is wired up to this
// coworker.
//
// Format:    "Claude · Claude Opus 4.7"
//            "Pi · GPT-4o"
//            "Claude · (no model)"          ← coworker.model_id is null
//            "Claude"                       ← model lookup failed
//
// The separator is a middle-dot (`·`) — softer than the `|` the user
// drafted, matches v2 prototype's tone for hierarchical labels.

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
  if (!c.model_id) return `${backend} · (no model)`;
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
