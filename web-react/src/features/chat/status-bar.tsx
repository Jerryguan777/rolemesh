// StatusBar — the reference statusBar region (spec §5.4): the one
// deliberate addition. Left: colour-hashed dot + active agent name so
// a single-agent conversation is always legible. Right: run spinner +
// progress label + Stop while a run is active (spec §6.4), or a
// reconnecting hint when the stream is down.

import type { Coworker } from '../../api/client';
import { avatarColourVar } from '../../lib/avatar-colour';
import type { ConnectionStatus } from '../../ws/v1_client';

export function StatusBar({
  agent,
  runActive,
  progress,
  wsStatus,
  hasChat,
  onStop,
}: {
  agent: Coworker | null;
  runActive: boolean;
  progress: string | null;
  wsStatus: ConnectionStatus;
  hasChat: boolean;
  onStop: () => void;
}) {
  const disconnected =
    hasChat && (wsStatus === 'reconnecting' || wsStatus === 'closed');
  return (
    <div className="status-bar">
      <div className="agent-line">
        {agent ? (
          <>
            <span className="dot" style={{ background: avatarColourVar(agent.id) }} />
            <span>{agent.name}</span>
          </>
        ) : null}
      </div>
      <div className="run-line">
        {runActive ? (
          <>
            <span className="spinner" aria-hidden="true" />
            <span>{progress ?? 'running'}</span>
            <button className="stop-btn" onClick={onStop}>
              Stop
            </button>
          </>
        ) : disconnected ? (
          <span>Reconnecting…</span>
        ) : null}
      </div>
    </div>
  );
}
