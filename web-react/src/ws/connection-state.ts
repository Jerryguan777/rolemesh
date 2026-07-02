// Copied from web/src/ws/connection-state.ts @ cf6b0f1; keep in sync manually until workspace extraction.
// ConnectionState — single source of truth for "is the SPA's WS link
// to the backend live?".
//
// Why: the SPA runs two long-lived sockets concurrently — `V1WsClient`
// for streaming and the legacy `AgentClient` for the Stop signal —
// each with its own reconnect loop and its own status. Before this
// module the top-bar connection dot was wired through a one-way
// `agent-connection` CustomEvent that bubbled out of `<rm-message-editor>`.
// A single missed dispatch left the dot stuck on "connected" even after
// the socket had silently dropped. That bug is hard to spot in dev and
// invisible to monitoring.
//
// Model: every client that owns a socket calls `set(channelId, live)`
// on the singleton whenever its underlying WebSocket flips open or
// closes. The aggregate `connected` is the OR of all registered
// channels (any live socket → green dot). Listeners receive a fresh
// boolean only when the aggregate actually changes, so a noisy
// reconnect-storm doesn't churn subscribers.
//
// Test seam: the file exports the class so tests can spin up a fresh
// instance with a known initial state instead of leaking state through
// the module-scoped singleton.

export type ConnectionStateListener = (connected: boolean) => void;

export class ConnectionState {
  // Map<channelId, isConnected>. We keep the boolean (rather than a
  // Set of "connected channels") so a channel reporting `false` while
  // another reports `true` is visible during debugging — toString'd
  // entries reveal the disagreement at a glance.
  private readonly channels = new Map<string, boolean>();
  private readonly listeners = new Set<ConnectionStateListener>();

  /** Aggregate state. True iff at least one registered channel is currently live. */
  get connected(): boolean {
    for (const v of this.channels.values()) if (v) return true;
    return false;
  }

  /** Report a channel's live status. Notifies listeners only when the
   *  aggregate flips, so a reconnect that toggles a single channel
   *  doesn't double-fire if a sibling channel is keeping the
   *  aggregate green. */
  set(channelId: string, connected: boolean): void {
    const before = this.connected;
    this.channels.set(channelId, connected);
    const after = this.connected;
    if (before !== after) {
      for (const l of this.listeners) l(after);
    }
  }

  /** Drop a channel entirely (called on client teardown so a stopped
   *  client's last `false` doesn't outlive its lifetime and prevent
   *  the aggregate from ever flipping back to true via a sibling). */
  remove(channelId: string): void {
    if (!this.channels.has(channelId)) return;
    const before = this.connected;
    this.channels.delete(channelId);
    const after = this.connected;
    if (before !== after) {
      for (const l of this.listeners) l(after);
    }
  }

  subscribe(listener: ConnectionStateListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Test helper — wipe state between cases. Not used in production. */
  reset(): void {
    this.channels.clear();
    this.listeners.clear();
  }
}

/** Process-wide singleton. Importers should NOT new up their own
 *  unless they are in a test that wants isolation. */
export const connectionState = new ConnectionState();
