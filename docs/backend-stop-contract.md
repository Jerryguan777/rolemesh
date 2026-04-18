# Backend Stop contract

This document records what the UI's Stop button demands from an
`AgentBackend` implementation. It exists because when Pi and Claude SDK
backends were initially written, **neither honored Stop correctly**, and
the failure modes were all silent:

- Late `ResultEvent` reaching the UI after it already showed "idle"
- Cancelled turn's content leaking into the next turn's LLM context
- Ghost follow-ups queued mid-abort resurfacing two turns later
- Latent `_aborting` flag gagging every future follow-up

Each bug was fixed independently in the backend where it manifested (see
commits `ace6db3`, `b08f44d`, `143fd03`, `acd469f`, `4e1ba93` on
`fix/three-bugs`). The fixes look similar in shape but diverge in
mechanics because the two runtimes cancel in fundamentally different
ways:

- **Pi**: cooperative cancellation — an `asyncio.Event` signal that
  provider-level stream loops must check between chunks.
- **Claude**: preemptive cancellation — `asyncio.Task.cancel()` injects
  `CancelledError` into whatever await the SDK is parked on.

Resist the urge to share the implementations. Share the **contract**
(this document + the comment block on `AgentBackend.abort()` in
`src/agent_runner/backend.py`) and test it per backend.

## Observable behaviors a backend's `abort()` must deliver

### 1. No event for the aborted turn after `abort()` is awaited

Once the caller of `abort()` has awaited it to completion, the backend
MUST NOT emit any further `BackendEvent` attributable to the turn that
was aborted:

- No late `ResultEvent` (would add an assistant bubble to an idle UI)
- No late `ToolUseEvent` (would flash a tool-call indicator after Stop)
- No `ErrorEvent` if the user aborted (error vs user-initiated stop are
  different UX states — emit `StoppedEvent` instead)

Failure mode observed in the wild: `stream.end()` on a `MessageStream`
or setting a bare signal flag is NOT enough. The provider subprocess
(Claude CLI) or HTTP stream (OpenAI Responses) keeps producing output
until its own cancellation mechanism fires. You must actively interrupt
the provider — via task cancellation, signal check at every await, or
equivalent.

### 2. Rewind the resume anchor

Backends chain turns to each other via some per-turn pointer:

- Pi: `SessionManager._leaf_id` (pointer into the session tree)
- Claude: `_last_assistant_uuid` (fed back via
  `resume-session-at` on the next `query()`)
- A future backend might use: conversation id, thread id, checkpoint
  sha, etc.

On abort, this anchor MUST snap back to the value it had at the start
of the aborted turn. Otherwise the next turn's provider call replays
through the aborted turn's partial output — the user sends "hello" and
gets a reply that continues the cancelled question.

Snapshot it in `run_prompt`/equivalent BEFORE any state-mutating call
into the provider. Restore it in `abort()` AFTER the provider has
actually stopped (wait_for_idle, task completion, whatever).

### 3. Clear internal queues

Backends buffer incoming follow-up messages, steering messages, and
any other "to be injected on next boundary" state. An aborted turn's
queued items belong to the abandoned turn — they must NOT survive into
the next turn.

In Pi this manifested as `agent._follow_up_queue` retaining `Q2` after
abort; two turns later the outer agent loop's
`get_follow_up_messages()` poll pulled Q2 out and processed it as a
phantom continuation of a supposedly fresh conversation. The fix
clears the queue inside `AgentSession.abort()`.

If your backend has ANY per-turn buffer (write-queue, pending-tools,
steering messages, batched user messages), it must be cleared here.

### 4. Guard the follow-up path during the abort window

`handle_follow_up()` is called concurrently from the orchestrator while
run_prompt is still in flight. Between the user clicking Stop and
`abort()` finishing its work, an arriving follow-up can race onto the
provider's input queue BEFORE cancellation has propagated. The pushed
message then becomes part of the aborted turn's input batch — the
aborted context leaks into the next reply.

The standard defense: an `_aborting: bool` flag set at the start of
`abort()`, cleared at its end, consulted by `handle_follow_up()` to
drop pushes while True. See `ClaudeBackend` and `PiBackend` for working
examples.

### 5. Leave the backend usable after abort

- `StoppedEvent` must be emitted so the UI can exit the 'stopping'
  transitional state.
- No latent flag may gag follow-ups on future turns. (Trap: if your
  `_aborting` flag is cleared only inside `run_prompt`'s finally, an
  `abort()` called with no active run_prompt leaves it stuck True.
  Clear it in `abort()` itself when there was no work to cancel.)
- The container must stay alive — `abort()` is not shutdown. The next
  `run_prompt("Q2")` should work normally.

## Checklist for adding a new backend

Before shipping `MyBackend.abort()`, answer these out loud (ideally
in a PR comment):

- [ ] **How do you interrupt the provider's in-flight call?** Document
      the mechanism — signal check in a stream loop, task.cancel(),
      HTTP client close, subprocess SIGTERM, etc. Handwaving
      "stream.end() should stop it" is the #1 way to ship a silent
      late-reply bug.

- [ ] **What is your resume anchor?** Name the field(s) that let the
      NEXT turn continue from the prior turn. Those are what you must
      snapshot-and-rewind.

- [ ] **What internal queues can hold messages during a turn?** List
      them (follow-up, steering, tool-pending, etc.) and ensure each
      is cleared in `abort()`.

- [ ] **Does your stream loop yield control often enough?** If the
      cancellation model is cooperative, your provider must check the
      signal at each chunk. If preemptive, verify the SDK propagates
      `CancelledError` out of its internal awaits rather than
      swallowing it.

- [ ] **Does `abort()` work when called with nothing running?**
      Between-turn aborts (UI misfire, double-click) must be no-ops
      that leave the backend clean for the next turn — no latched
      flags, no half-cleared state.

- [ ] **Does `abort()` work when called mid-pre-prompt?** Abort can
      race with `run_prompt`'s setup phase (between its first await
      and task/stream creation). Your guard must already be in place
      by the time the consumer starts.

- [ ] **Do you have per-backend regression tests for each of the five
      observable behaviors above?** Unit tests against the provider's
      mocked event stream are enough; you do NOT need a real LLM API
      key. See `tests/test_agent_runner/test_claude_abort.py` and
      `tests/test_agent_runner/test_pi_abort_rewind.py` for concrete
      patterns (scripted stream_fn, sys.modules stubbing, mutation
      testing via `git stash`).

## Known gaps (as of 2026-04-17)

- Stop contract is implemented per-backend. A behavioral test suite
  parameterized across backends would catch cross-backend drift; not
  done yet.
- Pi providers other than `openai_responses` and `openai_completions`
  (anthropic, azure_openai_responses, openai_codex_responses) had a
  leftover TS-port bug (`getattr(signal, "aborted", False)` always
  False against `asyncio.Event`). Patched in the same commit that
  added this doc — but the pattern is an easy trap for a new provider
  author. If you port another provider from the Pi TypeScript codebase,
  search for `.aborted` usages and map them to `.is_set()`.
