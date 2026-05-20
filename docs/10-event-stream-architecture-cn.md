# Agent 事件流架构

本文档阐述 RoleMesh 的**进度事件流**——从正在运行的 agent 容器到用户浏览器的实时状态事件流转。进度事件（`queued`、`container_starting`、`running`、`tool_use`）让用户能够看到在发送消息到获得结果之间的 10–60 秒里，agent 正在做什么。

配套的 **Stop / 追加消息（steering）** 功能与之密切相关——它们共享同一个 NATS subject 和同一个 UI 界面——但其设计在 [`steering-architecture.md`](steering-architecture.md) 中单独记录。本文档只覆盖协议的进度 / 状态这一面。

---

## 背景：沉默的 Agent 问题

在进度事件存在之前，WebUI 的体验是：

```
User sends message → "thinking..." spinner → (10–60s of silence) → result
```

在沉默期间，用户无法知道：

- 容器是否仍在启动？
- LLM 是否正在生成？
- 工具是否正在运行？
- 是否卡住了？

Telegram / Slack 用户能容忍这种沉默，因为他们对聊天延迟有预期。浏览器用户会与 ChatGPT / Claude / Cursor 比较，期待更紧凑的反馈。

## 范围：仅限 WebUI

我们刻意**没有**把进度事件扩展到 Telegram / Slack：

- 这些渠道没有"状态栏"的概念——其速率受限的消息 API 无法在单轮中承载多个临时更新。
- 那些 UI 中没有 Stop 按钮。
- 让进度事件有价值的流式 UX 存在于浏览器中。

所有与进度相关的代码路径都以 `isinstance(gw, WebNatsGateway)` 作为闸门。`BackendEvent` 类型本身与后端无关，但路由层会对非 web 渠道静默丢弃它们。

## 设计目标

1. **以低成本集成到现有基础设施中**——不引入新的 NATS streams、不引入新的认证路径、不引入新的进程。
2. **后端无关的事件词汇**——Pi 与 Claude SDK 必须发出语义相同的相同事件，使得无论容器内运行哪个引擎，UI 都拥有稳定的契约。
3. **与文本流保持有序**——`tool_use: Read src/app.ts` 之后跟随 `text: "I found..."`，必须端到端按此顺序到达。

---

## 为什么不为进度新建一个 NATS subject？

最显而易见的设计是新建一种 subject 模式，例如 `web.progress.{binding_id}.{chat_id}`，并使用自己的 payload 类型。我们拒绝了这一方案。

| 方案 | 优点 | 缺点 |
|---|---|---|
| **新 subject `web.progress.*`** | 语义清晰分离 | 在 orchestrator 和 FastAPI 各多一个 stream consumer 任务；多一条订阅清理路径；跨 subject 与 `web.stream.*` 之间无法保证顺序 |
| **复用现有 `web.stream.*`，用 `type="status"` 鉴别器搭载**（已选） | 保留顺序（同一个有序 consumer）；零新增基础设施；可轻易移除 | `WebStreamChunk` 增加第三种 type |

顺序问题决定了取舍。用户看到 `tool_use: Read src/app.ts` → `text: "I found…"`。这两个事件必须**按该顺序**到达。若使用两个独立的 NATS subject，顺序就依赖于 consumer 的调度——很容易出错，无法保证。若使用同一个 subject，JetStream 的有序 consumer 自动给予这种保证。

`WebStreamChunk` 现在长这样：

```python
@dataclass(frozen=True, slots=True)
class WebStreamChunk:
    type: str  # "text" | "done" | "status"
    content: str = ""   # for status: a JSON-encoded payload
```

`ws.py` 解包内部 JSON 并作为带类型的帧转发：

```json
{"type": "status", "status": "tool_use", "tool": "Bash", "input": "ls /tmp"}
```

## 为什么不用 `is_typing`？

WebUI 历史上有一个 `typing` 通道，会在 agent 开始处理时触发。我们考虑过重载它——扩展 `WebTypingMessage` 让它携带一个 phase 字符串。被拒绝：

- `typing` 对应聊天习惯中"对方正在输入"的语义——一个布尔的在场指示器。用 phase 语义重载它会同时令读者和未来的渠道（例如原生处理 typing 的移动 app）感到困惑。
- 保持二者分开，让我们日后能在不动到进度事件协议的情况下废弃 `typing`。

今天 `typing` 仍会触发（前端用它来生成空的助手消息气泡），但它不再携带 phase 信息，且未来不会扩展。

---

## BackendEvent 抽象

Pi 和 Claude SDK 都在容器内、位于 `AgentBackend` Protocol 之后运行。它们发出 `BackendEvent`，由 NATS bridge 翻译为 NATS 发布。对进度而言有三种事件类型很重要：

- **`RunningEvent`** —— 当 `run_prompt()`（或 `handle_follow_up()`）开始时发出。映射到 UI 状态 `running`。
- **`ToolUseEvent(tool, input_preview)`** —— 在每次工具调用开始时发出。映射到 UI 状态 `tool_use`。
- **`StoppedEvent`** —— 当 `abort()` 完成时发出。映射到 UI 状态 `stopped`。（与 steering 相关——见 [`steering-architecture.md`](steering-architecture.md)。）

完整的 `BackendEvent` union、为什么我们扩展它而不是为每个后端发明各自的 payload、以及 Claude 的 `SystemMessage(init)` / `AssistantMessage` 块与 Pi 的 `SessionInitEvent` / `ToolExecutionStartEvent` 如何映射到它，都记录在 [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md) 中。本文档只负责这三种事件对 UI 状态栏意味着什么。

### `ToolUseEvent.input_preview` 设计

```python
@dataclass(frozen=True)
class ToolUseEvent:
    tool: str            # "Bash", "Read", "mcp__rolemesh__send_message", ...
    input_preview: str   # "ls /tmp", "/etc/hostname", ...
```

`input_preview` 是单一的、简短的、面向用户的字符串，而非结构化的 `{args: dict}`。原因：

- UI 只显示一行：`Bash · ls /tmp`。展示格式是发射方一侧的决策，而非消费方的决策。
- 发射方（后端）知道哪些工具输入字段值得展示。`Bash` 关心 `command`；`Read` 关心 `file_path`；`Grep` 关心 `pattern`。UI 不应该需要承担这种工具特定的知识。
- 直接传整个 `args` 有泄漏敏感内容（密码、token、大型 payload）的风险。预览函数会截断到 80 个字符。

### E2E 中抓到的 Bug：MCP 前缀与大小写

`tool_input_preview` 辅助函数最初是按 Claude SDK 的 PascalCase 工具名（`Bash`、`Read`）写的。Pi 使用小写（`bash`、`read`），而 MCP 工具以 `mcp__<server>__<tool>` 形式到来——这两者都未命中匹配表，导致预览为空。

修复方式是在匹配前剥离命名空间并转为小写：

```python
base = tool_name.rsplit("__", 1)[-1] if "__" in tool_name else tool_name
tn = base.lower()
if tn in ("read", "write", "edit", "glob", "grep", "notebookedit"): ...
```

跨后端的一致性集中在这一个纯函数辅助器中。增加一个新工具就意味着扩展一张匹配表。

---

## `AgentOutput` 状态枚举

在 orchestrator 一侧，`AgentOutput` dataclass 承载状态，并流入 `_on_output`。一旦我们确立了一条规则，扩展它就比听起来简单：**每个状态要么是终态，要么是进度指示，绝不兼具两者**。

```python
# src/rolemesh/agent/executor.py
PROGRESS_STATUSES = ("queued", "container_starting", "running", "tool_use")
TERMINAL_STATUSES = ("success", "error", "stopped", "safety_blocked")

@dataclass(frozen=True)
class AgentOutput:
    status: Literal[*PROGRESS_STATUSES, *TERMINAL_STATUSES]
    result: str | None
    metadata: dict[str, object] | None = None   # tool preview payload

    def is_progress(self) -> bool:
        return self.status in PROGRESS_STATUSES
```

`safety_blocked` 是 Safety Framework V2 的终态——管线在该轮能够产出任何输出之前拦截了它。关于触发条件，参见 [`safety/safety-framework.md`](safety/safety-framework.md)。

`_on_output` 中的进度分支是一个提前返回，永远不会触碰空闲计时器、`notify_idle` 或 "had_error" 标志：

```python
async def _on_output(result: AgentOutput) -> None:
    if result.is_progress():
        if binding and isinstance(gw, WebNatsGateway):
            payload = {"status": result.status, **(result.metadata or {})}
            await gw.send_status(binding.id, conv.channel_chat_id, payload)
        return   # ← progress events do not touch terminal state
    # ... existing success/error/stopped/safety_blocked handling ...
```

早期 reviewer 问过是否要复用 `result` 字符串字段来承载工具 payload（例如 `result=json.dumps({"tool": "Bash"})`）。我们拒绝了：`result` 是一个文本字段，重载它会让类型安全的处理变得更困难。新增的 `metadata: dict` 字段在语义上独立，始终可选，与 `result` 永不冲突。

---

## 发射点位置：每个事件起源于何处

| 事件 | 发射方 | 触发条件 |
|---|---|---|
| `queued` | `GroupQueue.enqueue_message_check`（orchestrator） | 进入跨 coworker 的等待队列 |
| `container_starting` | `GroupQueue._run_for_coworker`（orchestrator） | 从"无运行"过渡到"正在创建容器" |
| `running` | Backend（容器内） | `run_prompt()` 开始 与 `handle_follow_up()` 开始 |
| `tool_use` | Backend（容器内） | Claude 的 `AssistantMessage.content[ToolUseBlock]` 或 Pi 的 `ToolExecutionStartEvent` |
| `stopped` | Backend（容器内） | `backend.abort()` 完成——参见 steering 文档 |

### 为什么 `running` 按每轮发出，而非按每会话

最初我们在 `backend.start()`（容器的会话初始化时）只发出一次 `RunningEvent`。这在冷启动测试中看起来正确，但在温启容器的追加消息中失效了：在仍存活的容器上的第二条消息没有发出 `RunningEvent`；如果那一轮恰好是没有工具的简单纯文本响应，UI 的状态栏整段时间都是空的。

修复方法是在每次 `run_prompt` 和 `handle_follow_up` 开始时都发出 `RunningEvent`，与会话状态无关。Claude SDK 实际上已经在每次 `query()` 调用时再次发出 `SystemMessage(init)`，但我们仍然防御性地发——UI 契约不应依赖 SDK 内部细节。

### 为什么进度发射点位于调度器，而非执行器

`container_starting` 理论上可以从 `ContainerAgentExecutor.execute()` 发出——它知道容器何时开始启动。但执行器的语义是"运行一个容器并翻译其输出"；它不是一个事件源。

调度器（`GroupQueue`）才是正确的发射点，因为它拥有启动容器的**决策权**。第一版把发射放在执行器里，在 review 中被指出是分层异味。`set_on_queued` / `set_on_container_starting` 的回调注入让调度器对事件之后发生什么保持无知（它只是带着 conversation_id 调用一个函数指针），同时允许 `main.py` 路由到 gateway。

### 为什么 `tool_use` 按块发出，而非按消息

一条 Claude 的 `AssistantMessage` 可能包含多个 `ToolUseBlock` 条目（并行工具调用）。我们对每个块发出一个 `ToolUseEvent`，而不是把它们合并成一个带列表的单一事件。

原因：

- UI 把状态栏渲染为单行，显示*当前*活动。一个工具列表会产生误导：它们可能并行运行，但用户随着 agent 推进而把它们看作顺序进行。
- 状态栏的覆盖语义意味着无论如何也只有最后一个事件可见。发出 N 个事件并让 UI 渲染最后一个，比客户端合并要简单。
- 未来的 UI 可以渲染一个 per-tool 列表；现在就拆分能保留这个选项。

---

## Stop / 追加消息（Steering）

Stop 按钮以及在一轮进行中输入追加消息的能力，不在本文档范围内。它们共享同一个 NATS subject（`web.stream.{binding_id}.{chat_id}` 携带 `status: "stopped"` 的 chunk）和同一套 UI 状态机词汇，但其设计决策——关闭 vs. 中断、三态 UI 状态机、Stop 信号上的 IDOR 作用域、追加消息延迟——都记录在 [`steering-architecture.md`](steering-architecture.md) 中。进度事件协议有意识地被设计成能与它们干净地共存：`stopped` 只是又一种终态状态；UI 中的状态栏状态机对它与 `done` / `error` 一视同仁。

---

## 已知限制

1. **进度事件不会持久化。** 重连后，UI 无法回放近期的进度事件——只有消息本身存储在 DB 中。在某轮进行中刷新的用户会丢失状态栏，并看到一种原生的"in-flight"体验，直到下一个事件到达。
2. **状态栏只能覆盖。** 多个并行的 `tool_use` 事件会塌缩为"显示最新一个"。未来的 UI 可以渲染一个 per-tool 列表；发射形态（每块一个事件）为此留下了空间。
3. **冷启动可见性。** 如果用户发出一条消息，而容器需要 5+ 秒才能创建出来，只有 `container_starting`（一个事件）来填补这段空白。UI 目前不区分"image pull"与"进程启动"——它们都归类在 `container_starting` 之下。

---

## 测试策略

单元测试覆盖协议（`AgentOutput.is_progress`、`WebStreamChunk` 往返、跨全部三种命名约定的 `tool_input_preview`、调度器守卫）：

- `tests/agent/test_executor.py`
- `tests/ipc/test_web_protocol.py`
- `tests/test_agent_runner/test_event_translation.py`
- `tests/container/test_scheduler.py`

端到端验证是用 Playwright MCP 在真实运行的栈上完成的：冷启动 → 状态栏推进 → tool_use → 结果。Stop 之后晚到的 `tool_use` 事件不应影响状态栏，这一点由 steering 测试套件验证——参见 [`steering-architecture.md`](steering-architecture.md)。

前端状态栏目前尚无自动化测试——这是一个值得填补的缺口。基于 vitest + happy-dom 并配合 mock 的 `AgentClient`，可以低成本覆盖关键状态转换。

---

## 取舍总结

| 决策 | 选择 | 备选 | 原因 |
|---|---|---|---|
| 进度通道 | 复用 `web.stream.*` 并使用 `type="status"` | 新建 `web.progress.*` subject | 顺序保证，零新增基础设施 |
| 事件词汇 | 扩展 `BackendEvent` union | 按后端定制 payload | Pi 与 Claude 共享同一契约 |
| `ToolUseEvent` 形态 | `{tool, input_preview: str}` | `{tool, args: dict}` | 发射方知道如何格式化；UI 与工具无关；不泄漏 payload |
| `running` 发射 | 按每轮（`run_prompt` + `handle_follow_up`） | 每会话一次 | 否则温启容器追加消息将没有进度 |
| `tool_use` 粒度 | 每个 `ToolUseBlock` 一个事件 | 一个合并的列表事件 | 匹配 UI 的覆盖语义；将 per-tool 列表保留为未来选项 |
| 进度发射点 | 调度器（`_run_for_coworker`） | 执行器（`execute()`） | 执行器不是事件源；调度器拥有该决策 |

---

## 相关文档

- [`steering-architecture.md`](steering-architecture.md) —— 与本协议共享 `status: "stopped"` chunk 的 Stop / 追加消息设计
- [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md) —— `BackendEvent` union，以及每个后端如何把自身原生事件映射到它
- [`5-webui-architecture.md`](5-webui-architecture.md) —— `WebNatsGateway`，将这些事件投递到浏览器的 FastAPI WebSocket 层
- [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md) —— `web.stream.{binding_id}.{chat_id}` subject，以及有序 consumer 行为
