// @vitest-environment happy-dom
//
// Frontdesk v1.5 delegation sub-chip lifecycle — pins the contract that
// the four `event.delegation.*` frames drive ephemeral
// `<rm-child-agent-chip>` elements under the parent agent's status bar.
//
// The chip stream re-expresses an older-branch feature against the
// current v1 protocol (`V1WsClient` + generated discriminated union).
// What's load-bearing and would silently regress:
//   * a chip is keyed by `child_conv_id`, so two concurrent delegations
//     render as two distinct chips (one shared key would collapse them);
//   * `progress` / `tool_use` mutate the *existing* chip, never conjure
//     a phantom when no chip is open;
//   * `completed` unmounts only the matching chip;
//   * a terminal run frame (completed / error) clears ALL chips so a
//     dropped `completed` can't strand one on screen forever;
//   * the `[via <name>]` delegation marker parses into a badge field
//     rather than leaking into the rendered body.
//
// Anti-mirror: the assertions describe the *observable* chip map and the
// rendered DOM, not a re-derivation of the handler's branching. We poke
// the real handler (`handleV1Event`) with literal wire frames — the same
// harness the sibling chat-panel.test.ts uses — and a couple of cases
// mount the real Lit tree to prove the chips actually paint.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatPanel, extractViaPrefix } from './chat-panel.js';
import type { ChatMessage } from './chat-panel.js';
import './child-agent-chip.js';
// Side-effect import registers <rm-message-item> so the via-badge case
// can mount it directly (chat-panel registers these at the app entry,
// not in-module). The named type import below is erased by the compiler;
// the side-effect import is what actually defines the custom element.
import './message-item.js';
import type { MessageItem } from './message-item.js';

// --- Shapes we reach into. The handlers are private; the chat-panel
//     test established this type-cast seam, so we mirror it. ---

interface ChipInternals {
  v1: unknown;
  api: { cancelRun: ReturnType<typeof vi.fn> };
  activeRunId: string | null;
  runState: 'idle' | 'running' | 'stopping' | 'cancelling';
  runTerminal: boolean;
  messages: ChatMessage[];
  agentStatus: { label: string } | null;
  activeChildChips: Map<
    string,
    {
      delegationId: string;
      targetName: string;
      targetFolder: string;
      contextMode: string;
      baseLine: string;
      startedAt: number;
    }
  >;
  handleV1Event(e: unknown): void;
  childChipLine(chip: { baseLine: string; startedAt: number }): string;
  clearDurationTick(): void;
  clearRunningWatchdog(): void;
}

function makePanel(): ChipInternals {
  const panel = new ChatPanel();
  const i = panel as unknown as ChipInternals;
  // The handler calls resetRunningWatchdog while runState === 'running';
  // give it a run so the chip frames flow through the active-run guard.
  i.activeRunId = 'run-1';
  i.runState = 'running';
  return i;
}

// Common fields shared by every delegation frame (snake_case on the wire).
function common(overrides: Record<string, unknown> = {}) {
  return {
    run_id: 'run-1',
    child_conv_id: 'child-A',
    delegation_id: 'deleg-A',
    target_folder: 'trading',
    target_name: 'Trading Desk',
    ...overrides,
  };
}

describe('extractViaPrefix', () => {
  it('splits a leading [via Name] marker off the body', () => {
    const r = extractViaPrefix('[via Trading Desk] The ledger reconciles.');
    expect(r.viaTargetName).toBe('Trading Desk');
    expect(r.content).toBe('The ledger reconciles.');
  });

  it('leaves a message with no marker untouched', () => {
    const r = extractViaPrefix('Just a normal reply.');
    expect(r.viaTargetName).toBeUndefined();
    expect(r.content).toBe('Just a normal reply.');
  });

  it('only matches the leading marker, never a bracket mid-body', () => {
    // A bracketed phrase later in the text must not be mistaken for the
    // delegation marker — otherwise legitimate prose gets mangled.
    const r = extractViaPrefix('See the note [via email] below.');
    expect(r.viaTargetName).toBeUndefined();
    expect(r.content).toBe('See the note [via email] below.');
  });
});

describe('ChatPanel — delegation chip lifecycle (frontdesk v1.5)', () => {
  afterEach(() => {
    // The duration ticker is a real setInterval; leaking it across tests
    // keeps requestUpdate firing on a detached element. Belt-and-braces.
    document.querySelectorAll('rm-chat-panel').forEach((el) => el.remove());
  });

  it('started mounts a chip keyed by child_conv_id', () => {
    const i = makePanel();

    i.handleV1Event({
      type: 'event.delegation.started',
      ...common({ context_mode: 'isolated', initial_status: 'queued' }),
    });

    expect(i.activeChildChips.size).toBe(1);
    const chip = i.activeChildChips.get('child-A');
    expect(chip).toBeDefined();
    expect(chip!.targetName).toBe('Trading Desk');
    expect(chip!.delegationId).toBe('deleg-A');
    expect(chip!.contextMode).toBe('isolated');
    // initial_status=queued maps to the human-readable line.
    expect(chip!.baseLine).toBe('Queued…');
    i.clearDurationTick();
  });

  it('progress updates the open chip status line', () => {
    const i = makePanel();
    i.handleV1Event({ type: 'event.delegation.started', ...common() });

    i.handleV1Event({
      type: 'event.delegation.progress',
      ...common({ status: 'running' }),
    });

    expect(i.activeChildChips.get('child-A')!.baseLine).toBe('Thinking…');
    i.clearDurationTick();
  });

  it('tool_use beautifies the tool name and appends the input preview', () => {
    const i = makePanel();
    i.handleV1Event({ type: 'event.delegation.started', ...common() });

    i.handleV1Event({
      type: 'event.delegation.tool_use',
      ...common({
        tool_name: 'mcp__rolemesh__send_message',
        tool_input_preview: 'to=ops',
      }),
    });

    // beautifyToolName turns mcp__server__tool into "server › tool".
    expect(i.activeChildChips.get('child-A')!.baseLine).toBe(
      'rolemesh › send_message · to=ops',
    );
    i.clearDurationTick();
  });

  it('completed unmounts the matching chip', () => {
    const i = makePanel();
    i.handleV1Event({ type: 'event.delegation.started', ...common() });
    expect(i.activeChildChips.size).toBe(1);

    i.handleV1Event({
      type: 'event.delegation.completed',
      ...common({ final_status: 'success', duration_ms: 1234 }),
    });

    expect(i.activeChildChips.size).toBe(0);
    i.clearDurationTick();
  });

  it('two concurrent child_conv_ids render two independent chips', () => {
    const i = makePanel();
    i.handleV1Event({
      type: 'event.delegation.started',
      ...common({ child_conv_id: 'child-A', target_name: 'Trading Desk' }),
    });
    i.handleV1Event({
      type: 'event.delegation.started',
      ...common({
        child_conv_id: 'child-B',
        delegation_id: 'deleg-B',
        target_name: 'Finance',
      }),
    });

    expect(i.activeChildChips.size).toBe(2);

    // Completing one must NOT touch the other — the map is per-child.
    i.handleV1Event({
      type: 'event.delegation.completed',
      ...common({ child_conv_id: 'child-A', final_status: 'success' }),
    });
    expect(i.activeChildChips.size).toBe(1);
    expect(i.activeChildChips.has('child-B')).toBe(true);
    expect(i.activeChildChips.get('child-B')!.targetName).toBe('Finance');
    i.clearDurationTick();
  });

  it('progress for an unknown child_conv_id does not conjure a phantom chip', () => {
    // A status frame racing ahead of (or after) the chip's lifetime must
    // not create a chip the user can't dismiss.
    const i = makePanel();

    i.handleV1Event({
      type: 'event.delegation.progress',
      ...common({ child_conv_id: 'ghost', status: 'running' }),
    });

    expect(i.activeChildChips.size).toBe(0);
    i.clearDurationTick();
  });

  it('run.completed clears every lingering chip (stranding guard)', () => {
    const i = makePanel();
    i.handleV1Event({ type: 'event.delegation.started', ...common() });
    i.handleV1Event({
      type: 'event.delegation.started',
      ...common({ child_conv_id: 'child-B', delegation_id: 'deleg-B' }),
    });
    expect(i.activeChildChips.size).toBe(2);

    // Parent run ends while two delegations never sent `completed` — the
    // dropped frames would otherwise strand both chips forever.
    i.handleV1Event({ type: 'event.run.completed', run_id: 'run-1' });

    expect(i.activeChildChips.size).toBe(0);
  });

  it('run.error also clears lingering chips', () => {
    const i = makePanel();
    i.handleV1Event({ type: 'event.delegation.started', ...common() });
    expect(i.activeChildChips.size).toBe(1);

    i.handleV1Event({
      type: 'event.run.error',
      run_id: 'run-1',
      code: 'INTERNAL',
      message: 'boom',
    });

    expect(i.activeChildChips.size).toBe(0);
  });

  it('a delegation frame from a stale run is ignored', () => {
    // JetStream redelivery on reconnect can replay a delegation frame
    // from a run the user has already moved past. Without the run_id
    // guard it would mount a chip for a turn that's over.
    const i = makePanel();
    i.activeRunId = 'run-current';

    i.handleV1Event({
      type: 'event.delegation.started',
      ...common({ run_id: 'run-previous' }),
    });

    expect(i.activeChildChips.size).toBe(0);
    i.clearDurationTick();
  });

  it('a redelivered started keeps the original startedAt (monotonic elapsed)', () => {
    const i = makePanel();
    i.handleV1Event({ type: 'event.delegation.started', ...common() });
    const firstStartedAt = i.activeChildChips.get('child-A')!.startedAt;

    // Force time forward, then redeliver the same started frame.
    const spy = vi
      .spyOn(Date, 'now')
      .mockReturnValue(firstStartedAt + 5_000);
    try {
      i.handleV1Event({ type: 'event.delegation.started', ...common() });
      expect(i.activeChildChips.get('child-A')!.startedAt).toBe(firstStartedAt);
    } finally {
      spy.mockRestore();
    }
    i.clearDurationTick();
  });

  it('childChipLine suppresses the (Ns) tail under 2s, then shows it', () => {
    const i = makePanel();
    const base = 1_700_000_000_000; // realistic epoch-ms anchor
    const spy = vi.spyOn(Date, 'now');
    try {
      // Under 2s: no elapsed tail (avoids a "(0s)" flash on fast turns).
      spy.mockReturnValue(base + 1_000);
      expect(
        i.childChipLine({ baseLine: 'Thinking…', startedAt: base }),
      ).toBe('Thinking…');
      // At 4s: tail appears with the floored seconds.
      spy.mockReturnValue(base + 4_000);
      expect(
        i.childChipLine({ baseLine: 'Thinking…', startedAt: base }),
      ).toBe('Thinking… (4s)');
    } finally {
      spy.mockRestore();
    }
    i.clearDurationTick();
  });
});

// ---------------------------------------------------------------------------
// Rendered-DOM proof: the chips actually paint, and the [via X] badge
// shows up on a history-loaded assistant message.
// ---------------------------------------------------------------------------

async function settle(el: ChatPanel): Promise<void> {
  for (let n = 0; n < 10; n += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

describe('ChatPanel — delegation chip rendering', () => {
  // chat-panel.connectedCallback kicks off bootstrap() → getMe() etc.
  // Stub fetch to a 404 so those calls resolve fast and happy-dom doesn't
  // try to open a real socket (which logs AbortError noise on teardown).
  let originalFetch: typeof globalThis.fetch;
  beforeEach(() => {
    originalFetch = globalThis.fetch;
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ code: 'NOT_FOUND' }), { status: 404 }),
      ) as unknown as typeof globalThis.fetch;
  });

  afterEach(() => {
    document.querySelectorAll('rm-chat-panel').forEach((el) => el.remove());
    globalThis.fetch = originalFetch;
  });

  it('renders one <rm-child-agent-chip> per active chip with its line', async () => {
    const panel = new ChatPanel();
    const i = panel as unknown as ChipInternals;
    i.activeRunId = 'run-1';
    i.runState = 'running';
    document.body.appendChild(panel);
    await settle(panel);

    i.handleV1Event({
      type: 'event.delegation.started',
      ...common({ initial_status: 'running' }),
    });
    i.handleV1Event({
      type: 'event.delegation.started',
      ...common({
        child_conv_id: 'child-B',
        delegation_id: 'deleg-B',
        target_name: 'Finance',
        initial_status: 'queued',
      }),
    });
    await settle(panel);

    const chips = panel.querySelectorAll('rm-child-agent-chip');
    expect(chips.length).toBe(2);
    // The first chip surfaces its target + status line.
    const text = panel.textContent ?? '';
    expect(text).toContain('Trading Desk');
    expect(text).toContain('Finance');
    expect(text).toContain('Thinking…'); // running → Thinking…
    expect(text).toContain('Queued…');

    i.clearDurationTick();
    i.clearRunningWatchdog();
  });

  it('completed removes the chip element from the DOM', async () => {
    const panel = new ChatPanel();
    const i = panel as unknown as ChipInternals;
    i.activeRunId = 'run-1';
    i.runState = 'running';
    document.body.appendChild(panel);
    await settle(panel);

    i.handleV1Event({ type: 'event.delegation.started', ...common() });
    await settle(panel);
    expect(panel.querySelectorAll('rm-child-agent-chip').length).toBe(1);

    i.handleV1Event({
      type: 'event.delegation.completed',
      ...common({ final_status: 'success' }),
    });
    await settle(panel);
    expect(panel.querySelectorAll('rm-child-agent-chip').length).toBe(0);

    i.clearDurationTick();
    i.clearRunningWatchdog();
  });

  it('a [via X] message renders the via badge, not the literal marker', async () => {
    // The chat-panel parses the marker into `viaTargetName` (loadMessages /
    // event.message.appended); message-item turns that field into a badge.
    // We feed message-item the *parsed* message so the two halves of the
    // contract are pinned together: extractViaPrefix splits, the renderer
    // badges, and the literal marker never reaches the bubble body.
    const parsed = extractViaPrefix('[via Trading Desk] Done — ledger balanced.');
    expect(parsed.viaTargetName).toBe('Trading Desk');

    const item = document.createElement('rm-message-item') as MessageItem;
    item.message = {
      role: 'assistant',
      content: parsed.content,
      viaTargetName: parsed.viaTargetName,
      timestamp: 1,
    } as ChatMessage;
    document.body.appendChild(item);
    await item.updateComplete;

    const badge = item.querySelector('[data-testid="via-badge"]');
    expect(badge).not.toBeNull();
    expect(badge!.textContent).toContain('via Trading Desk');
    // The literal marker must NOT appear in the rendered body.
    expect(item.textContent).not.toContain('[via Trading Desk]');
    expect(item.textContent).toContain('Done — ledger balanced.');

    item.remove();
  });

  it('an assistant message with no marker renders no via badge', async () => {
    const item = document.createElement('rm-message-item') as MessageItem;
    item.message = {
      role: 'assistant',
      content: 'A normal reply.',
      timestamp: 1,
    } as ChatMessage;
    document.body.appendChild(item);
    await item.updateComplete;

    expect(item.querySelector('[data-testid="via-badge"]')).toBeNull();
    item.remove();
  });
});
