// @vitest-environment happy-dom
// Stop vs Cancel routing test — design §4.1 hard split.
//
// The contract being pinned: in chat-panel, **Stop** must call
// ``v1.stop()`` (which sends a ``request.stop`` frame on the v1 WS
// → orchestrator publishes ``web.stop.{...}`` → agent_runner aborts
// the current turn) and **Cancel** must POST to the v1 REST
// ``/api/v1/runs/{id}/cancel``. Collapsing the two would force every
// soft interrupt through a 1-3s container cold-start.
//
// PR-B (2026-05-31) migrated Stop off the legacy ``AgentClient`` /
// ``/ws/chat`` path; both buttons now go through ``V1WsClient``, but
// they ride different surfaces inside it (WS frame vs REST call).
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
    stop: ReturnType<typeof vi.fn>;
    cancelRun: ReturnType<typeof vi.fn>;
    send: ReturnType<typeof vi.fn>;
    disconnect: ReturnType<typeof vi.fn>;
  } | null;
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
    stop: vi.fn(),
    cancelRun: vi.fn(async () => ({ ok: true, alreadyTerminal: false })),
    send: vi.fn(),
    disconnect: vi.fn(),
  };
  // ApiClient fallback — should NOT be called when v1 is wired.
  i.api = {
    cancelRun: vi.fn(async () => ({ ok: true, alreadyTerminal: false })),
  };
  return { panel, i };
}

describe('ChatPanel — Stop vs Cancel routing (design §4.1)', () => {
  it('Stop calls v1.stop() and never touches v1.cancelRun', () => {
    const { i } = makePanel();
    i.runState = 'running';
    i.activeRunId = 'run-1';

    i.handleStop();

    expect(i.v1!.stop).toHaveBeenCalledOnce();
    expect(i.v1!.cancelRun).not.toHaveBeenCalled();
    expect(i.api.cancelRun).not.toHaveBeenCalled();
    expect(i.runState).toBe('stopping');
    // Clean up watchdog so it doesn't leak into other tests.
    i.clearStoppingTimer();
  });

  it('Cancel calls v1.cancelRun(activeRunId) and never triggers Stop', async () => {
    const { i } = makePanel();
    i.runState = 'running';
    i.activeRunId = 'run-42';
    i.runTerminal = false;

    await i.handleCancel();

    expect(i.v1!.cancelRun).toHaveBeenCalledExactlyOnceWith('run-42');
    expect(i.v1!.stop).not.toHaveBeenCalled();
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

    expect(i.v1!.stop).not.toHaveBeenCalled();
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


// ---------------------------------------------------------------------------
// Side-channel events: event.message.appended + event.run.thinking
//
// Pinning the v1 protocol's two non-run-bound additions (PR #38). Both
// were missing from the original v1.1 cutover — scheduled-task replies
// never reached the SPA live, and the typing indicator disappeared
// entirely. A regression that drops the chat-panel handlers (or breaks
// the run_id guard on thinking) would surface here.
// ---------------------------------------------------------------------------


interface InternalsWithEvents extends Internals {
  messages: Array<{ role: string; content: string; streaming?: boolean }>;
  agentStatus: { label: string } | null;
  handleV1Event(e: unknown): void;
}


describe('ChatPanel — side-channel events (PR #38)', () => {
  it('event.message.appended pushes a new assistant bubble', () => {
    const { i: base } = makePanel();
    const i = base as unknown as InternalsWithEvents;
    i.messages = [{ role: 'user', content: 'hi' }];

    i.handleV1Event({
      type: 'event.message.appended',
      content: '⏰ 2 minutes up!',
      source: 'scheduled_task',
      timestamp: '2026-05-31T12:00:00+00:00',
    });

    expect(i.messages).toHaveLength(2);
    expect(i.messages[1]).toEqual({
      role: 'assistant',
      content: '⏰ 2 minutes up!',
      // Ordering key parsed from the event's own server timestamp.
      timestamp: Date.parse('2026-05-31T12:00:00+00:00'),
    });
    // Side-channel push must NOT touch run state — there's no
    // user-initiated run associated with a scheduled-task reminder.
    expect(i.runState).toBe('idle');
  });

  it('event.message.appended with empty content is ignored', () => {
    const { i: base } = makePanel();
    const i = base as unknown as InternalsWithEvents;
    i.messages = [];

    i.handleV1Event({
      type: 'event.message.appended',
      content: '',
      source: 'scheduled_task',
      timestamp: '2026-05-31T12:00:00+00:00',
    });

    // An empty bubble would render as a blank rectangle; defensive
    // drop keeps malformed orchestrator publishes from polluting
    // the conversation.
    expect(i.messages).toEqual([]);
  });

  it('event.run.progress renders a tool_use label when run_id matches', () => {
    const { i: base } = makePanel();
    const i = base as unknown as InternalsWithEvents;
    i.activeRunId = 'run-current';
    i.agentStatus = null;

    i.handleV1Event({
      type: 'event.run.progress',
      run_id: 'run-current',
      status: 'tool_use',
      tool: 'Read',
      input_preview: 'file=README.md',
    });
    expect(i.agentStatus).toEqual({ label: 'Calling Read…' });
  });

  it('event.run.progress maps known statuses to canonical labels', () => {
    const { i: base } = makePanel();
    const i = base as unknown as InternalsWithEvents;
    i.activeRunId = 'r1';

    const cases: Array<[Record<string, unknown>, string]> = [
      [{ status: 'running' }, 'Thinking…'],
      [{ status: 'container_starting' }, 'Starting container…'],
      [{ status: 'queued' }, 'Queued…'],
      [{ status: 'tool_use' }, 'Calling tool…'],
      // Unknown status falls back to a generic label rather than
      // leaking the raw kind to end users (e.g. ``compaction_started``
      // would look like a bug to a non-engineer).
      [{ status: 'unknown_kind_xyz' }, 'Working…'],
    ];
    for (const [extra, expectedLabel] of cases) {
      i.agentStatus = null;
      i.handleV1Event({ type: 'event.run.progress', run_id: 'r1', ...extra });
      expect(i.agentStatus).toEqual({ label: expectedLabel });
    }
  });

  it('event.run.output_done closes the streaming bubble without ending the run', () => {
    // Single-writer refactor: `done` chunks became per-bubble
    // event.run.output_done. In a batched turn (queued follow-ups)
    // several arrive before the one true run-terminal frame — the
    // bubble must close (so the next reply spawns a fresh one) while
    // Stop stays available and the run state stays 'running'.
    const { i: base } = makePanel();
    const i = base as unknown as InternalsWithEvents;
    i.runState = 'running';
    i.runTerminal = false;
    i.activeRunId = 'run-1';
    i.messages = [
      { role: 'user', content: 'first question' },
      { role: 'assistant', content: 'first answer', streaming: true },
    ];

    i.handleV1Event({ type: 'event.run.output_done', run_id: 'run-1' });

    expect(i.messages[1].streaming).toBeFalsy();
    expect(i.runState).toBe('running');
    expect(i.runTerminal).toBe(false);

    // Next reply's token spawns a NEW bubble instead of appending to
    // the closed one — the exact behavior the split exists to keep.
    i.handleV1Event({ type: 'event.run.token', run_id: 'run-1', delta: 'second' });
    expect(i.messages).toHaveLength(3);
    expect(i.messages[2]).toMatchObject({
      role: 'assistant',
      content: 'second',
      streaming: true,
    });

    // Only the run-terminal frame flips the run state.
    i.handleV1Event({ type: 'event.run.completed', run_id: 'run-1' });
    expect(i.runState).toBe('idle');
    expect(i.runTerminal).toBe(true);
    expect(i.messages[2].streaming).toBeFalsy();
  });

  it('event.run.progress from a stale run is ignored', () => {
    // JetStream redelivery on reconnect can replay an old progress
    // frame after the user has moved on to a new turn. Without the
    // run_id guard, the SPA would briefly flash a phase that's
    // already passed — visually disorienting and a clear regression
    // signal in QA.
    const { i: base } = makePanel();
    const i = base as unknown as InternalsWithEvents;
    i.activeRunId = 'run-current';
    i.agentStatus = null;

    i.handleV1Event({
      type: 'event.run.progress',
      run_id: 'run-previous',
      status: 'tool_use',
      tool: 'Read',
    });

    expect(i.agentStatus).toBeNull();
  });
});
