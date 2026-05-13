# Steering：Stop 与 Follow-up 架构

本文档介绍 RoleMesh 的 "steering" 功能——让用户对正在进行的 agent turn 拥有控制权。包含两种能力：

1. **Stop 按钮**：在不杀掉容器的前提下中断 agent 当前 turn，让用户能立即用一条新消息重新指引方向。
2. **运行中追加消息（Follow-up while running）**：在 agent 还在为前一条消息生成回复时，就可以输入并发送新消息。

本文涵盖 WebUI 侧的交互体验、控制面信号设计，以及若干非显而易见决策背后的原因。关于进度事件那一侧的故事（`container_starting`、`running`、`tool_use`——agent 流向用户的*输出*），见 [`10-event-stream-architecture.md`](10-event-stream-architecture.md)。这两套特性共享同一组 NATS 协议表面，但解决的是不同问题。

---

## 背景：「Agent 卡死了——让我重来一次」

在 steering 出现之前，如果用户发现 agent 走偏了，他能做的只有一件事：等待。"thinking…" 动画会跑 30–60 秒，直到 agent 把那个错误的 turn 跑完，他才能再次输入。生成期间输入框是禁用的（`<textarea ?disabled=${this.isStreaming}>`）。

这与 ChatGPT 2022 年前后的最初行为一致，但时至今日，每一个主流 agent UI（ChatGPT、Claude.ai、Cursor、Copilot）都支持：

- 用 Stop 按钮中止当前生成
- 在当前消息仍在生成时输入下一条消息

我们也需要这些。问题在于*怎么做*——答案有一些有趣的约束，因为 RoleMesh 把 agent 跑在 Docker 容器里，冷启动要花好几秒，这就排除了那种朴素的「杀掉再重启」模式。

---

## 设计目标

1. **Stop 不能带来一次冷启动的代价。** 用户点 Stop 是因为想换个方向，而不是想离开。如果他接下来打的下一条消息要走容器重建流程（拉镜像、SDK 初始化、连接 MCP、恢复 session），那就是 5–10 秒的空白——比让那个错误的 turn 跑完还要糟糕。
2. **UI 不能说谎。** 用户点了 Stop 后，按钮应当表达「正在请求服务端停止」直到服务端确认，而不是立刻显示「已停止」。Abort 是尽力而为，在 agent 内部可能要几秒钟。
3. **Follow-up 不能要求新基础设施。** Telegram 和 Slack 用户在一个 turn 期间本来就会发多条消息；orchestrator 已经处理了这条路径。WebUI 的改动只应是「解除发送按钮的禁用」加上必要的交互打磨。
4. **不引入新的认证表面。** Stop 路径不得引入新的攻击者可控输入。一个被攻陷的浏览器不能 Stop 别人的对话。
5. **后端无关。** Stop 协议必须对 Pi 与 Claude SDK agent 行为一致，因为用户并不知道也不关心当前跑的是哪种后端。

---

## Stop：`interrupt` 信号 vs. `shutdown`

### 既有的 `shutdown` 信号很诱人——但是错的

RoleMesh 本就有 `GroupQueue.request_shutdown(group_jid)`，它会发布到 `agent.{job_id}.shutdown`。在容器内，处理器会：

1. `abort` 掉当前 turn
2. **跳出主 `while True:` 循环**，导致容器进程退出

对它原本的三个调用方来说这是正确行为——`IDLE_TIMEOUT` 到期、任务容器被调度器抢占、优雅关闭。三者都共享同一种意图：「这个容器我们暂时不要了，回收资源吧」。

这个功能的第一版直接复用 `request_shutdown` 实现 Stop 按钮。它通过了 E2E 测试。但用户**下一步**动作——输入一条追加消息——就不得不冷启动一个全新容器，因为上一个已经退出了。那就是 5–10 秒用户并未要求的延迟，违反了目标 1。

### 修正：两个信号，差一行

我们新增了一个平行的信号 `agent.{job_id}.interrupt`，它与 `shutdown` 的差别仅在一行：

```python
# agent_runner/main.py

async def handle_shutdown(msg: Any) -> None:
    await msg.respond(b"ack")
    shutdown_received.set()           # ← main loop detects this → break

async def handle_interrupt(msg: Any) -> None:
    await msg.respond(b"ack")
    log("Interrupt signal received, aborting current turn")
    await backend.abort()             # ← no shutdown_received; loop continues
```

两者都会调用 `backend.abort()`——这是开销最大的部分（Pi 的 `session.abort()` 会等待 agent 变为 idle；Claude 的 `stream.end()` 会关闭 SDK 输入）。唯一的差别在于 `while True` 循环是否也一并退出。

中断之后，容器会：

- 把当前 turn 排空收尾（如果有工具在跑可能要好几秒，因为 abort 不是抢占式的）
- 回到主循环的「等待下一次输入」分支
- 继续持有它的 Pi `AgentSession` / Claude `query()` session、MCP 连接、工作区挂载、凭证代理

当用户输入下一条消息时，它走的是正常的 follow-up 路径（见下文），命中的是一个温启容器。

底层差异（`interrupt` 用 JetStream 而 `shutdown` 用 Core NATS request-reply，以及为什么）见 [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md) Channel 3。

### 命名：`stop`（产品） vs `interrupt`（系统）

| 层次 | 用词 |
|---|---|
| 面向用户的按钮 | **Stop** |
| WebSocket 消息类型 | `{type: "stop"}` |
| FastAPI → NATS subject | `web.stop.{binding_id}.{chat_id}` |
| NATS → agent subject | `agent.{job_id}.interrupt` |
| 后端方法 | `backend.abort()` |

这种拆分是有意为之。**Stop** 是用户与产品经理在讨论的产品概念。**Interrupt** 是系统概念，对应 Unix / Jupyter 里「暂停当前活动，回到 ready 状态」的惯例。这把它和 `shutdown`（关闭容器）以及 `cancel`（在多数库里隐含 HTTP/连接拆除语义）区分开来。

---

## 三态 UI

### 为什么是三态，而不是两态

一个两态按钮（Send ↔ Stop）配上对 Stop 的乐观确认，会让人觉得很灵敏，但在欺骗用户。后端的 `abort()` 并非瞬时：

- Claude SDK：`stream.end()` 关闭输入流，但 SDK 仍会继续把当前工具输出排空直到完成。
- Pi：`session.abort()` 内部会调 `wait_for_idle()`。如果当前工具是 `bash sleep 60`，abort 就会等 bash 跑完——60 秒。

在这个窗口期，agent 仍可能发出 `tool_use`、`text`、`done` 事件。如果 UI 已经翻回 Send 模式，用户可能会：

- 再点一次 Stop——但 turn 实际上没在跑。服务端无副作用但用户会困惑。
- 发送一条新消息——这就会与 abort 形成竞争。新消息可能在前一个 turn 清空之前就到达，违反用户「我先把旧的停了」的心理模型。

我们采用了三态：

```
idle ── user sends ──► running ── user clicks Stop ──► stopping ── server confirms ──► idle
 ▲                       │                                │
 │                       │                                │ 10s safety timer
 └───────────────────────┴────────────────────────────────┘
```

| 状态 | 按钮外观 | 是否可点击？ | 输入框 |
|---|---|---|---|
| `idle` | 品牌色 ↑（发送） | 仅当有文本时 | 接受输入 |
| `running` | 深色 ■（停止） | 始终可点 | 接受 follow-up 输入 |
| `stopping` | 深色半透明 ■ + 旋转圆环 | **禁用**，光标 `wait` | 接受输入 |

输入框在三个状态下都保持启用，这样用户在等待 abort 完成的同时也能起草下一条消息。

### 进入和退出条件

`stopping` 是乐观进入的——用户一点 Stop，UI 不等服务端就立即切换状态。这样是安全的，因为：

- 按钮变为禁用，所以用户没法再点。
- 退出 `stopping` 是由服务端四种事件中的*任意一种*驱动的（见下文），所以即使卡在 `stopping`，UI 也能自愈。

`stopping` 的退出路径：

| 事件 | 为什么有效 |
|---|---|
| `{type: "status", status: "stopped"}` | Happy path——服务端确认了 abort |
| `{type: "done"}` | 当前 turn 在 abort 生效之前自然跑完了。可以接受——用户看到的是正常的最终输出，而不是被截断到中途 |
| `{type: "error"}` | 出了点问题；视为终态 |
| 10s 安全计时器 | 服务端始终没确认（容器崩溃、NATS 分区、agent 卡住）。UI 自己脱困，而不是要求刷页面 |

### 状态机对迟到进度事件的加固

Claude 和 Pi 可能在用户点了 Stop *之后*、abort 生效*之前*仍发出 `tool_use` 或 `running`。朴素实现会在这些事件到来时把 `stopping` 重置回 `running`，导致按钮闪烁。

`chat-panel.ts` 的 `handleMessage` 中的守卫如下：

```typescript
case 'status':
  if (msg.status === 'stopped') {
    // Happy path out of stopping.
    this.clearStoppingTimer();
    this.agentState = 'idle';
    break;
  }
  // Any other status (tool_use, running, etc.) updates the status bar
  // but does NOT reset 'stopping' back to 'running'. Only idle can
  // elevate to running — stopping is sticky until terminal.
  this.agentStatus = { status: msg.status, tool: msg.tool, input: msg.input };
  if (this.agentState === 'idle') {
    this.agentState = 'running';
  }
  break;
```

规则是：**进度事件只能把状态从 `idle` 抬升到 `running`，绝不能从 `stopping` 退回 `running`。** 在 abort 窗口期，这是一扇单向门。

---

## Stop 全链路流程

```
Browser (chat-panel.ts)
  click handler → state = 'stopping' (optimistic, synchronous)
  → client.stop()
  │
  ▼
WebSocket: {"type": "stop"}
  │
  ▼
ws.py (handle_ws receive loop)
  uses authenticated binding_id + chat_id from the WS handshake
  (NEVER reads these from the client payload — IDOR guard)
  → _js.publish("web.stop.{binding_id}.{chat_id}", b"{}")
  │
  ▼
NATS subject: web.stop.{binding_id}.{chat_id}
  │
  ▼
WebNatsGateway._stop_listener (orchestrator)
  parses subject parts → invokes _on_stop(binding_id, chat_id)
  │
  ▼
main.py._handle_web_stop
  find_conversation_by_binding_and_chat → conv
  _queue.interrupt_current_turn(conv.id)
  │
  ▼
GroupQueue.interrupt_current_turn (scheduler.py)
  guard: if not state.active or not state.job_id: return
  else: publish NATS request to agent.{job_id}.interrupt
  │
  ▼
agent_runner.handle_interrupt (container)
  ack, then await backend.abort()
  │
  ▼
Backend.abort() emits StoppedEvent
  Pi:     await session.abort()       → emit
  Claude: stream.end()                → emit
  │
  ▼
NATS bridge → ContainerOutput(status="stopped")
  published to agent.{job_id}.results
  │
  ▼
Orchestrator _on_output (rolemesh/main.py)
  status == "stopped" branch:
    gw.send_status(binding, chat, {"status": "stopped"})
    gw.send_stream_done(binding, chat)
    _queue.notify_idle(conv.id)
  │
  ▼
NATS subject: web.stream.{binding_id}.{chat_id}
  │
  ▼
ws.py._forward_stream → WebSocket frames:
  {type:"status", status:"stopped"} and {type:"done"}
  │
  ▼
Browser handleMessage('status'/'stopped')
  → clearStoppingTimer; state = 'idle'; button = ↑
```

整条往返大概在 200ms 到几秒之间，主要取决于 agent 在当前工具或当前生成中实际响应 abort 的快慢。

---

## 安全：基于 Subject 的授权

Stop 路径是一个新的攻击者表面：原则上任何被攻陷的浏览器都可以向它想要的*任何* `web.stop.*.*` subject 发布消息。我们在 `ws.py` 中用一条严格的规则来缓解这一点：

**浏览器的 payload 完全被忽略。** 编码在 NATS subject 里的 `binding_id` 与 `chat_id` 取自经过认证的 WebSocket 握手，绝不从客户端那里重新读取：

```python
elif data.get("type") == "stop":
    # Do NOT use data.get("chat_id") / data.get("binding_id") from
    # the payload — always use the authenticated binding_id/chat_id
    # from the WebSocket handshake to prevent IDOR from a compromised
    # or malicious client.
    await _js.publish(f"web.stop.{binding_id}.{chat_id}", b"{}")
```

消息体是 `b"{}"`——一个有意为之的标记，表示根本没有什么需要解析。orchestrator 的 `WebNatsGateway._stop_listener` 只读 `msg.subject`，绝不读 `msg.data`。一个被攻陷的浏览器只能 Stop 它已经认证过的对话。

这个设计意味着 subject 本身*就是*授权令牌。如果以后要做按信号粒度的权限（比如「管理员能 Stop 任意 agent」），就得换一套机制。眼下，「你只能 Stop 自己连上的那个」就是全部策略。

---

## Follow-up 消息：复用既有的链路

Follow-up **零后端改动**。Telegram / Slack 用户本来就会在一个 turn 期间发多条消息，orchestrator 的消息循环早已处理这种情况：

```
Browser types during running turn
  ↓ WebSocket {type: "message", content: "..."}
  ↓ ws.py → NATS web.inbound.{binding_id}
  ↓ _handle_incoming → DB store + enqueue_message_check
  ↓ (state.active=True → state.pending_messages=True; no-op on schedule)
  ↓
  _message_loop polls every POLL_INTERVAL (2s)
  ↓ get_messages_since finds new messages
  ↓ _queue.send_message(conv_id, formatted_text)
  ↓ NATS publish agent.{job_id}.input
  ↓
  Container's poll_nats_during_query receives
  ↓ backend.handle_follow_up(text)
  ↓
  Pi:     session.prompt(text, streaming_behavior="followUp")
  Claude: self._stream.push(text)
```

WebUI 唯一的改动是删掉 `message-editor.ts` 中的 `if (this.isStreaming) return` 守卫：

```typescript
private handleSend() {
  // Follow-up messages are allowed even while the agent is running.
  // The orchestrator queues them for after the current turn.
  if (!this.value.trim()) return;  // ← removed the isStreaming check
  ...
}
```

### 已知延迟：0–2.5 秒

由于这条路径要走 `_message_loop` 的 2 秒轮询节拍，follow-up 有一个延迟下限：

- 0–2 秒等待下一次轮询节拍
- ~50–100 毫秒的 DB 读取 + 格式化
- 0–500 毫秒的容器侧 `input_sub.next_msg(timeout=0.5)` 周期

**合计：0–2.5 秒，平均约 1 秒。** 这不是退化——Telegram / Slack 一直就是这么工作的。

让 `_handle_incoming` 绕过轮询循环的快速路径是可行的（当 `state.active` 时直接调 `_queue.send_message`），但会重复格式化逻辑，这次也不在范围内。如果这点延迟成了用户的抱怨点，那里就是要去优化的地方。

### `followUp` vs `steer`：Pi 的模式选择

Pi 的 `AgentSession.prompt()` 对 turn 中途到来的消息有两种模式：

- **`followUp`**——把消息排到队列里，等当前 turn 完成*之后*再处理。Pi 内部队列把它当作下一个用户 turn 处理。这就是我们使用的方式。
- **`steer`**——*中途*打断 agent 并把消息注入到当前 turn。Agent 把它当作一次纠正，立即吸收进当前的回应。

`followUp` 与 Claude SDK 的行为一致（被 push 进去的消息进入输入流，当前 turn idle 后才会被消费），对用户来说也可预期：「我的新消息会在这条跑完之后被看到」。

`steer` 更强大，但也更出人意料。一个用户在 agent 起草长回复时输入「其实算了，忘掉第一个请求」，与他在回复到达之后输入相同文本，行为会完全不同。这种用户看不见的模式分叉是个埋雷。Steer 在被暴露出来之前，应该有专属的 UI 元素（不同的按钮，或者一个修饰键）。

### 一个已知的 Pi bug：msg1 的回应可能丢失

Pi 后端有一个早就存在的问题，follow-up 把它更明显地暴露了出来：

`PiBackend.run_prompt` 把最后一个 `TurnEndEvent` 的文本存到 `self._last_result_text`，并在 `session.prompt()` 结束时只发出**一个** `ResultEvent`。如果 msg1 正在处理，msg2 作为 follow-up 进来，Pi 会顺序处理两者，但 `_last_result_text` 会被 msg2 的回应覆盖——**msg1 的回应永远不会被发给客户端。**

Claude 不受影响，因为它的 `run_prompt` 对每个 SDK 的 `ResultMessage` 都发出一个 `ResultEvent`，所以多个 turn 会产出多个 result。

修复需要在 Pi 的 `_handle_event` 内部，每收到一个 `TurnEndEvent` 就发出 `ResultEvent`，而不是仅在结束时发一次，还要考虑下游 `notify_idle` 的后果（`pi_backend.py` 的注释里之所以说当初先搁置了，是有原因的）。这件事与 steering 正交，但在这里值得提一句，因为 steering 让这个场景更容易出现。

---

## Orchestrator 的冷启动竞争

`GroupQueue.interrupt_current_turn` 里有一道守卫：

```python
def interrupt_current_turn(self, group_jid: str) -> None:
    state = self._get_group(group_jid)
    if not state.active or not state.job_id:
        return   # silent no-op
    ...
```

`state.active` 在 `_run_for_coworker` 被调用时立即设为 `True`。但 `state.job_id` 是稍后才被 `register_process(container_name, job_id)` 设上的，而后者由 `ContainerAgentExecutor` 在容器创建**完成之后**通过 `on_process(...)` 回调来触发。

这就有一个窗口——冷启动期间通常 0.5–3 秒——此时 `state.active=True` 但 `state.job_id=None`。在这个窗口内点击 Stop 会被守卫拦下，悄无声息地 return。用户看到的是：

- 按钮变成 `stopping`（客户端乐观地）
- 服务端毫无效果
- 10 秒安全计时器触发 → 按钮回到 `idle`
- 但 agent 实际跑到了自然结束

我们接受这是一个罕见情况。健壮的修复方法是在 `_GroupState` 上记一个 pending 的 `interrupt` 标记，`register_process` 时检查它，并在 `job_id` 可用后再发送信号。眼下没做；若有反馈再做。

---

## 事件顺序：`stopped` vs `done`

当 Pi 的 `backend.abort()` 跑起来时，有两个协程在抢着发事件：

- **`abort()` 路径**：`await session.abort()`（它自己会等到 idle）→ 发出 `StoppedEvent` → orchestrator 发布 `status: stopped` 和一个 `done` 帧。
- **`run_prompt()` 路径**：`await session.prompt()` 在 idle 时返回 → 发出 `ResultEvent`，携带 `_last_result_text` → orchestrator 发布 `text`（如有）和一个 `done` 帧。

两者都在 agent 进入 idle 时触发。我们在 E2E 中观察到 `ResultEvent` 路径通常会赢：客户端看到的是 `text → done → status:stopped → done`，而不是干净的 `status:stopped → done`。

前端两种顺序都能容忍：

- `text → done → stopped → done`（实际看到的）：`done` 让状态转为 `idle`；姗姗来迟的 `status:stopped` 发现状态已经 idle，是个无害的 no-op。
- `stopped → done → done`（理论上更干净的顺序）：`status:stopped` 让状态转为 idle；后面的 `done` 是个 no-op。

我们有意没在后端把两条路径串行化：

1. 串行化要么阻塞 `run_prompt` 的最后一次 emit 等待 abort 完成，要么反过来。哪种都会引入复杂度。
2. `stopped` 之前那个额外的 `text` 事件其实是*有用的信息*——用户看得到 Pi 的自然语言收尾消息（"The command was aborted before it could complete. Would you like to try…"）。其实是好的 UX。
3. 重复的 `done` 是可检测且天然幂等的。

我们接受这个顺序「足够接近」，把复杂度预算花在三态 UI 上。

---

## 已知局限

1. **Abort 在工具内部不是抢占式的。** 如果 agent 正在跑 `bash sleep 60`，abort 会一直等到 bash 跑完。"Stopping…" 视具体工具而定，最长可达 60 秒。
2. **冷启动期间 Stop 会被静默吞掉。** 见上面的竞争一节。
3. **Stop 的作用域是 chat，不是 tab。** 同一 `(binding_id, chat_id)` 上的多个标签页共享一个容器。一个标签的 Stop 会中止所有标签页里这段对话的 turn。
4. **Follow-up 延迟最高 2.5 秒。** 见 follow-up 那一节。
5. **Stop 没有限流。** 用户狂点按钮会发布很多条 NATS 消息；调度器的守卫让重复变成 no-op，但没有任何节流。
6. **不会恢复部分输出。** 如果 Pi 在被 abort 前已经生成了 200 个字符，这些字符在助手气泡里，但没有存进 DB（只有完整的助手消息才会通过 `_on_output` 持久化）。刷新页面，这段半成品就丢了。

---

## 测试策略

### 自动化

单元测试覆盖了协议各部分：

- `tests/agent/test_executor.py` —— `AgentOutput.is_progress()`、`TERMINAL_STATUSES` 包含 `stopped`
- `tests/test_agent_runner/test_event_translation.py` —— `StoppedEvent` → `ContainerOutput(status="stopped")` 的映射
- `tests/container/test_scheduler.py` —— `interrupt_current_turn` 的守卫（无活跃容器、无 transport）

### E2E

用 Playwright MCP 在完整的活栈上验证过（orchestrator、webui、vite、mock_mcp、跑在 OpenAI 上的 Pi 后端）。已测试的流程：

- 冷启动 → 状态栏 → 工具执行中按 `Stop` → `stopping` → `idle`
- 状态机对 Stop 之后迟到的 `tool_use` 的抵抗力（按钮不会闪回 Stop ■ 状态）
- 温启容器上的 follow-up（无 `container_starting`，但每个 turn 仍会触发 `running`，所以状态栏依然工作）
- Stop 后紧接着发新消息——确认容器仍是温启，且第二条消息不会带来冷启动延迟

### 缺口

前端状态机目前还没有 vitest 覆盖。所有跳转都是手动验证的。基于 fixture 对 `chat-panel.ts` 的 `handleMessage` 喂入合成事件序列的测试可以低成本地抓回归。初次 steering 提交里没纳入范围。

---

## 权衡总结

| 决策 | 选择 | 备选方案 | 原因 |
|---|---|---|---|
| Stop 信号 | 新加 `agent.{job_id}.interrupt`（容器保活） | 复用既有 `agent.{job_id}.shutdown`（终止容器） | Stop 不能带来一次冷启动 |
| UI 按钮状态 | 三态（`idle` / `running` / `stopping`） | 两态（`idle` / `running`），乐观回弹 | abort 是尽力而为；UI 不能说谎 |
| Stopping 退出条件 | 四种（stopped / done / error / 10 秒计时器） | 只等显式的 `stopped` | 事件顺序并不严格；`done` 同样有效 |
| 状态机的「黏性」 | `stopping` 胜过迟到的进度事件 | 进度事件始终更新状态 | 防止 abort 窗口期闪烁 |
| Stop 信号授权 | 基于 subject，忽略 payload | 在 payload 里带 chat_id | IDOR 防护——binding/chat 只来自认证过的握手 |
| Follow-up 机制 | 复用既有的 Telegram / Slack 消息循环 | 新做一条 per-channel 快速路径 | 零新基础设施；熟悉的代码路径 |
| Follow-up 延迟 | 接受 0–2.5 秒轮询节拍 | 让 `_message_loop` 走快速旁路 | 还不是用户痛点；旁路会重复格式化逻辑 |
| Pi turn 中途模式 | `followUp`（排到下一个 turn） | `steer`（注入当前 turn） | UX 可预期；steer 应当有自己的 UI 元素 |
| 后端 abort API | 既有 `backend.abort()`，加上 `StoppedEvent` 的 emit | 新做一个 abort-with-confirmation API | 既有方法够用；只差那个终态信号 |
| 命名 | `stop`（浏览器） / `interrupt`（后端） | 全链路统一一个名字 | 产品概念 vs 系统概念——不同读者有不同心理模型 |
| 事件顺序 | 接受 `text → done → stopped → done` | 在 Pi 内部串行化两条 emit 路径 | 信息性的 `text`（那段 "aborted" 消息）是好 UX；重复 `done` 幂等 |
| 冷启动竞争处理 | 在 `job_id` 还没就绪时接受 Stop 静默 no-op | 在 `_GroupState` 里排队待发的 interrupt | 罕见场景；10 秒安全计时器能恢复 UI；若有反馈再修 |
| Abort 时的部分输出 | 留在助手气泡里，不持久化 | 落库并打 `is_interrupted` 标记 | 为边际价值增加 schema 复杂度不划算；现有行为是「消息要么到，要么没到」 |

---

## 相关文档

- [`10-event-stream-architecture.md`](10-event-stream-architecture.md) —— 同一 `web.stream.*` subject 上的进度事件（`container_starting`、`running`、`tool_use`）；UI 状态栏协议
- [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md) —— `agent.{job}.interrupt`（JetStream） vs `agent.{job}.shutdown`（Core NATS request-reply）——为什么各自用相应的 NATS 原语
- [`backend-stop-contract.md`](backend-stop-contract.md) —— 任何后端在 `abort()` 上必须交付的可观测行为；Claude 抢占式 vs Pi 协作式取消
- [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md) —— `StoppedEvent` 与 `BackendEvent` 联合体
