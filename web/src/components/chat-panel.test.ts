// @vitest-environment happy-dom
// Stop vs Cancel routing test — design §4.1 hard split.
//
// The contract being pinned: in chat-panel, **Stop** must call the
// legacy `AgentClient.stop()` (which fires `{type:"stop"}` over the
// `/ws/chat` endpoint) and **Cancel** must POST to the v1 REST
// `/api/v1/runs/{id}/cancel`. Collapsing the two would force every
// soft interrupt through a 1-3s container cold-start.
//
// We deliberately test the *handlers* without spinning up the real
// LitElement render tree (Lit + jsdom is heavier than the contract
// being asserted needs). The handlers are private; the test reaches
// in via type-cast to assert the routing is correct. That's the
// thing the design §4.1 Pitfall warned about — a refactor that
// silently repoints either button breaks here.

import { describe, expect, it, vi } from 'vitest';
import { ChatPanel } from './chat-panel.js';

interface Internals {
  v1: {
    cancelRun: ReturnType<typeof vi.fn>;
    send: ReturnType<typeof vi.fn>;
    disconnect: ReturnType<typeof vi.fn>;
  } | null;
  stopClient: { stop: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn> } | null;
  api: { cancelRun: ReturnType<typeof vi.fn> };
  runState: 'idle' | 'running' | 'stopping' | 'cancelling';
  activeRunId: string | null;
  runTerminal: boolean;
  handleStop(): void;
  handleCancel(): Promise<void>;
  clearStoppingTimer(): void;
  clearCancellingTimer(): void;
}

function makePanel(): { panel: ChatPanel; i: Internals } {
  const panel = new ChatPanel();
  const i = panel as unknown as Internals;
  // Wire fakes that the design §4.1 split depends on.
  i.v1 = {
    cancelRun: vi.fn(async () => ({ ok: true, alreadyTerminal: false })),
    send: vi.fn(),
    disconnect: vi.fn(),
  };
  i.stopClient = {
    stop: vi.fn(),
    disconnect: vi.fn(),
  };
  // ApiClient fallback — should NOT be called when v1 is wired.
  i.api = {
    cancelRun: vi.fn(async () => ({ ok: true, alreadyTerminal: false })),
  };
  return { panel, i };
}

describe('ChatPanel — Stop vs Cancel routing (design §4.1)', () => {
  it('Stop calls the legacy AgentClient.stop() and never touches v1.cancelRun', () => {
    const { i } = makePanel();
    i.runState = 'running';
    i.activeRunId = 'run-1';

    i.handleStop();

    expect(i.stopClient!.stop).toHaveBeenCalledOnce();
    expect(i.v1!.cancelRun).not.toHaveBeenCalled();
    expect(i.api.cancelRun).not.toHaveBeenCalled();
    expect(i.runState).toBe('stopping');
    // Clean up watchdog so it doesn't leak into other tests.
    i.clearStoppingTimer();
  });

  it('Cancel calls v1.cancelRun(activeRunId) and never touches the legacy Stop path', async () => {
    const { i } = makePanel();
    i.runState = 'running';
    i.activeRunId = 'run-42';
    i.runTerminal = false;

    await i.handleCancel();

    expect(i.v1!.cancelRun).toHaveBeenCalledExactlyOnceWith('run-42');
    expect(i.stopClient!.stop).not.toHaveBeenCalled();
    expect(i.api.cancelRun).not.toHaveBeenCalled();
    expect(i.runState).toBe('cancelling');
    i.clearCancellingTimer();
  });

  it('Cancel on a 409 ALREADY_TERMINAL response synthesises the idle/terminal transition', async () => {
    const { i } = makePanel();
    i.runState = 'running';
    i.activeRunId = 'run-x';
    i.v1!.cancelRun = vi.fn(async () => ({ ok: false, alreadyTerminal: true }));

    await i.handleCancel();

    expect(i.v1!.cancelRun).toHaveBeenCalledExactlyOnceWith('run-x');
    expect(i.runState).toBe('idle');
    expect(i.runTerminal).toBe(true);
  });

  it('Stop is a no-op when the run is not in running state', () => {
    const { i } = makePanel();
    i.runState = 'idle';
    i.activeRunId = 'run-1';

    i.handleStop();

    expect(i.stopClient!.stop).not.toHaveBeenCalled();
    expect(i.runState).toBe('idle');
  });

  it('Cancel is a no-op when there is no active run id', async () => {
    const { i } = makePanel();
    i.runState = 'running';
    i.activeRunId = null;

    await i.handleCancel();

    expect(i.v1!.cancelRun).not.toHaveBeenCalled();
    expect(i.runState).toBe('running');
  });
});
