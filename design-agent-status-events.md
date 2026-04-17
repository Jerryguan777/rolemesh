# Agent 执行状态实时事件 — 设计方案

## 要解决什么问题

用户在 Web UI 里给 agent 发消息后，当前的体验是：

```
用户发消息 → thinking 动画 → (漫长等待，什么都看不到) → 突然出现结果
```

等待期间用户不知道 agent 在干什么、进展到哪了、是不是卡住了。

本方案的目标是让用户看到：

```
用户发消息 → 排队中(第2位) → 容器启动中 → 思考中 → 执行 Bash: npm test → 读取 src/app.ts → 编辑 src/app.ts → 结果
```

## 不做什么

- 不做定时任务进展通知
- 不做审批流
- 不做系统运维事件
- 不做 Slack/Telegram 端的状态推送
- 不做断线重连后的状态补发
- 不做可查询的"当前状态"
- 不新建 EventBus 模块或 NATS stream

## 现有架构

### agent_runner 三层结构

`feat/pi` 分支引入了 `AgentBackend` 抽象层，agent_runner 被拆为三层：

```
main.py (NATS bridge)              ← 后端无关，只管 NATS 通信 + 事件翻译
    │
    ▼
backend.py (AgentBackend 协议)     ← 定义 BackendEvent 统一事件接口
    │
    ├── claude_backend.py          ← 包装 claude_agent_sdk
    └── pi_backend.py              ← 包装 pi.coding_agent
```

所有后端通过 `BackendEvent` 向 NATS bridge 汇报，bridge 统一翻译成
`ContainerOutput` 发到 NATS。

### 现有 BackendEvent 类型

```python
# agent_runner/backend.py
BackendEvent = ResultEvent | SessionInitEvent | CompactionEvent | ErrorEvent
```

注意：没有 ToolUseEvent。两个后端当前都把 tool_use 信息丢弃了。

### 现有数据链路

一条消息从 agent 到用户屏幕经过的完整路径：

```
容器内                              orchestrator 进程                       Web UI
─────                               ──────────                             ──────

后端 emit BackendEvent
    ↓
main.py on_event()
  翻译为 ContainerOutput
  publish_output()
    ↓
NATS: agent.{job_id}.results ──→ container_executor.py: _read_results()
                                   解析 JSON, 调回调:
                                     ↓
                                 main.py: _on_output(result)
                                   如果是 Web:
                                     ↓
                                 web_nats_gateway.py: send_stream_chunk()
                                     ↓
                                 NATS: web.stream.{binding}.{chat} ──→ ws.py
                                                                        ↓
                                                                     WebSocket
                                                                        ↓
                                                                     agent-client.ts
                                                                        ↓
                                                                     chat-panel.ts
```

关键观察: 这条链路已经是实时流式的。现在的问题是后端只在
`ResultEvent` 时才发数据，tool_use 信息被丢弃了。

### 两个后端的事件能力对比

Claude SDK 的 `query()` 迭代器产出的消息类型：
- `SystemMessage(subtype="init")` — session 建立
- `AssistantMessage` — 内含 `ToolUseBlock(name, input)` ← **当前被丢弃**
- `ResultMessage` — 最终结果 ← 唯一被翻译成 BackendEvent 的

Pi 的 `AgentEvent` 类型体系（`pi/agent/types.py`）：
- `AgentStartEvent` / `AgentEndEvent`
- `TurnStartEvent` / `TurnEndEvent`
- `MessageStartEvent` / `MessageUpdateEvent` / `MessageEndEvent`
- `ToolExecutionStartEvent(tool_name, args)` ← **当前被忽略**
- `ToolExecutionUpdateEvent(partial_result)`
- `ToolExecutionEndEvent(result, is_error)`

Pi 有一级公民的 tool execution 事件，比 Claude SDK 更丰富。

## 改动思路

利用现有 `BackendEvent` 抽象层，新增事件类型。两个后端各自翻译，
NATS bridge 统一处理。不新建任何通道或模块。

```
Claude SDK                     Pi
──────────                     ──
AssistantMessage               ToolExecutionStartEvent
  含 ToolUseBlock                含 tool_name, args
    ↓                              ↓
claude_backend.py              pi_backend.py
  _emit(ToolUseEvent)            _schedule_emit(ToolUseEvent)
    ↓                              ↓
    └──────────┬───────────────────┘
               ↓
         main.py on_event()
           isinstance(event, ToolUseEvent)
           → publish_output(status="tool_use")
               ↓
         (沿现有链路一路到前端)
```

## WebSocket 协议变更

现有消息类型: `session` / `thinking` / `text` / `done` / `error`

新增一种 `status` 消息:

```jsonc
// 消息排队, 前面还有人
{"type": "status", "status": "queued", "position": 2}

// 容器正在启动
{"type": "status", "status": "container_starting"}

// 后端会话已建立, 开始处理
{"type": "status", "status": "running"}

// Agent 正在调用工具 (可能连续多次)
{"type": "status", "status": "tool_use", "tool": "Bash", "input": "npm test"}
{"type": "status", "status": "tool_use", "tool": "Read", "input": "src/app.ts"}
{"type": "status", "status": "tool_use", "tool": "Edit", "input": "src/app.ts"}

// (现有 text/done 消息不变)
```

`status` 消息是瞬时的状态指示, 不积累到聊天历史中。前端收到后更新状态栏,
收到下一个 `status` 或 `text` 或 `done` 时替换/清除。

## 各层详细改动

### 第 1 层: agent_runner/backend.py (事件定义)

新增两个事件类型:

```python
@dataclass(frozen=True)
class ToolUseEvent:
    """Emitted when the agent starts executing a tool."""
    tool: str           # "Bash", "Read", "Edit", etc.
    input_preview: str  # 用户可读的摘要, 如文件路径或命令前 80 字符

@dataclass(frozen=True)
class RunningEvent:
    """Emitted when the backend session is established and ready."""
    pass

BackendEvent = ResultEvent | SessionInitEvent | CompactionEvent | ErrorEvent | ToolUseEvent | RunningEvent
```

### 第 2 层: claude_backend.py (Claude SDK 翻译)

位置: `run_prompt()` 中 `async for message in query(...)` 循环。

现状: `AssistantMessage` 分支只提取 `uuid`。

改动:

```python
elif cls_name == "AssistantMessage":
    uuid = getattr(message, "uuid", None)
    if uuid:
        self._last_assistant_uuid = uuid

    # ★ 新增: 提取 ToolUseBlock, 发送 ToolUseEvent
    content = getattr(message, "content", None)
    if isinstance(content, list):
        for block in content:
            if getattr(block, "type", None) == "tool_use":
                tool_name = getattr(block, "name", "") or ""
                tool_input = getattr(block, "input", {}) or {}
                await self._emit(ToolUseEvent(
                    tool=tool_name,
                    input_preview=_tool_input_preview(tool_name, tool_input),
                ))
```

`SystemMessage(subtype="init")` 分支在现有 `SessionInitEvent` 之后追加:

```python
if subtype == "init":
    # ... 现有 SessionInitEvent 逻辑 ...
    await self._emit(RunningEvent())
```

新增辅助函数:

```python
def _tool_input_preview(tool_name: str, tool_input: dict) -> str:
    """从 tool input 中提取简短预览。"""
    if tool_name in ("Read", "Write", "Edit", "Glob", "Grep"):
        return (tool_input.get("file_path")
                or tool_input.get("path")
                or tool_input.get("pattern")
                or "")
    if tool_name == "Bash":
        return tool_input.get("command", "")[:80]
    if tool_name in ("WebSearch", "WebFetch"):
        return tool_input.get("query") or tool_input.get("url") or ""
    return ""
```

### 第 3 层: pi_backend.py (Pi 翻译)

位置: `_handle_event()` 方法。

现状: 只处理 `TurnEndEvent`。

改动:

```python
def _handle_event(self, event: AgentSessionEvent) -> None:
    if isinstance(event, TurnEndEvent):
        text = _extract_text(event.message) if hasattr(event, "message") else ""
        if text:
            self._last_result_text = text

    # ★ 新增: tool execution 事件
    elif isinstance(event, ToolExecutionStartEvent):
        self._schedule_emit(ToolUseEvent(
            tool=event.tool_name,
            input_preview=_tool_input_preview(event.tool_name, event.args),
        ))
```

注意: `_handle_event` 是同步回调, 所以用 `_schedule_emit`
(已有, 内部创建 asyncio.Task)。

`start()` 方法在 `SessionInitEvent` 之后追加 `RunningEvent`:

```python
await self._emit(SessionInitEvent(session_id=self._session_file or ""))
await self._emit(RunningEvent())  # ★ 新增
```

`_tool_input_preview` 函数在两个后端之间共享, 放到
`agent_runner/backend.py` 中。

### 第 4 层: agent_runner/main.py NATS bridge (事件 → NATS)

位置: `on_event()` 回调。

现状: 只处理 `ResultEvent`、`SessionInitEvent`、`CompactionEvent`、`ErrorEvent`。

改动:

```python
from .backend import ToolUseEvent, RunningEvent

async def on_event(event: BackendEvent) -> None:
    nonlocal session_id

    if isinstance(event, ToolUseEvent):
        await publish_output(
            js, job_id,
            ContainerOutput(
                status="tool_use",
                result=json.dumps({"tool": event.tool, "input": event.input_preview}),
            ),
        )
    elif isinstance(event, RunningEvent):
        await publish_output(
            js, job_id,
            ContainerOutput(status="running", result=None),
        )
    elif isinstance(event, ResultEvent):
        # ... 现有逻辑不变 ...
```

### 第 5 层: rolemesh/agent/executor.py (类型扩展)

`AgentOutput.status` 扩展:

```python
@dataclass(frozen=True)
class AgentOutput:
    status: Literal["success", "error", "running", "tool_use", "container_starting"]
    result: str | None
    new_session_id: str | None = None
    error: str | None = None
```

### 第 6 层: rolemesh/agent/container_executor.py (container_starting)

在 `execute()` 方法中, 调用 `self._runtime.run(spec)` 之前:

```python
if on_output is not None:
    await on_output(AgentOutput(status="container_starting", result=None))

handle = await self._runtime.run(spec)
```

`_parse_container_output` 无需改动 — 它已经用
`str(raw.get("status", "error"))` 做通用解析。

### 第 7 层: rolemesh/main.py _on_output (orchestrator 路由)

在 `_on_output` 开头增加状态事件处理:

```python
async def _on_output(result: AgentOutput) -> None:
    nonlocal had_error, output_sent_to_user

    # ★ 新增: 状态事件 → 仅 Web channel
    if result.status in ("running", "tool_use", "container_starting"):
        if binding and isinstance(gw, WebNatsGateway):
            status_payload: dict[str, Any] = {"type": "status", "status": result.status}
            if result.status == "tool_use" and result.result:
                tool_info = json.loads(result.result)
                status_payload["tool"] = tool_info.get("tool", "")
                status_payload["input"] = tool_info.get("input", "")
            await gw.send_stream_chunk(
                binding.id, conv.channel_chat_id,
                json.dumps(status_payload),
            )
        _reset_idle_timer()
        return

    # ... 现有 result.result 处理逻辑不变 ...
```

### 第 8 层: rolemesh/container/scheduler.py (排队事件)

给 `GroupQueue` 增加 `on_queued` 回调:

```python
class GroupQueue:
    def __init__(self, ...) -> None:
        # ... 现有代码 ...
        self._on_queued: Callable[[str, int], None] | None = None

    def set_on_queued(self, fn: Callable[[str, int], None]) -> None:
        self._on_queued = fn

    def enqueue_message_check(self, group_jid: str, ...) -> None:
        state = self._get_group(group_jid)
        if state.active:
            state.pending_messages = True
            if self._on_queued:
                position = len(state.pending_tasks) + 1
                self._on_queued(group_jid, position)
            return
        if not self._can_start(state.tenant_id, state.coworker_id):
            state.pending_messages = True
            if self._on_queued:
                position = len(self._waiting_groups) + 1
                self._on_queued(group_jid, position)
            # ... 现有 _waiting_groups 逻辑 ...
```

main.py 初始化时注入回调, 查找 conversation → binding → gateway → send_stream_chunk。

### 第 9 层: webui/ws.py (WebSocket 透传)

`_forward_stream` 检测 status 消息:

```python
if data.get("type") == "text":
    content = data["content"]
    if content.startswith('{"type":"status"'):
        try:
            status_msg = json.loads(content)
            if status_msg.get("type") == "status":
                await _broadcast(binding_id, chat_id, status_msg)
                await msg.ack()
                continue
        except (json.JSONDecodeError, KeyError):
            pass
    await _broadcast(binding_id, chat_id, {"type": "text", "content": content})
```

### 第 10 层: 前端 agent-client.ts + chat-panel.ts

**agent-client.ts** — `ServerMessage` 类型扩展:

```typescript
export type ServerMessage =
  | { type: 'session'; chatId: string; agentId: string }
  | { type: 'thinking' }
  | { type: 'text'; content: string }
  | { type: 'done' }
  | { type: 'error'; message: string }
  | { type: 'status'; status: string; tool?: string; input?: string; position?: number };
```

**chat-panel.ts** — `handleMessage()` 新增 case:

```typescript
case 'status': {
    this.agentStatus = msg;  // 新增 @state 属性
    break;
}
case 'text': {
    this.agentStatus = null;  // 收到文本时清除状态
    // ... 现有逻辑 ...
}
case 'done': {
    this.agentStatus = null;
    // ... 现有逻辑 ...
}
```

渲染 — 在消息列表底部显示状态指示条:

```typescript
${this.agentStatus ? html`
  <div class="agent-status-bar">
    ${this.agentStatus.status === 'queued'
      ? `排队中，前面还有 ${this.agentStatus.position} 个任务`
      : this.agentStatus.status === 'container_starting'
      ? '启动中...'
      : this.agentStatus.status === 'running'
      ? '思考中...'
      : this.agentStatus.status === 'tool_use'
      ? `执行 ${this.agentStatus.tool}: ${this.agentStatus.input}`
      : ''}
  </div>
` : ''}
```

## 事件时序

一次完整的用户交互中, 事件按以下顺序到达前端:

```
1. session                  — WebSocket 建连时 (现有)
2. status/queued            — 如果排队了才发 (新增, 来自 scheduler.py)
3. status/container_starting — 容器启动前 (新增, 来自 container_executor.py)
4. thinking                 — typing 指示器 (现有, 来自 main.py set_typing)
5. status/running           — 后端会话建立 (新增, 来自后端 RunningEvent)
6. status/tool_use          — 每次工具调用 (新增, 来自后端 ToolUseEvent, 可连续多次)
7. text                     — 最终结果文本 (现有, 来自后端 ResultEvent)
8. done                     — 流结束 (现有)
```

如果 agent 在一次执行中调了 10 个工具, 用户会连续收到 10 条
`status/tool_use` 消息, 前端每次替换显示最新的那条。

## 边界情况

**用户在 agent 执行中又发消息**: 现有的多轮机制 (NATS input channel)
不受影响。新消息通过 `agent.{job_id}.input` 注入,
后端继续执行, 状态事件继续发送。

**agent 执行超时**: `container_executor.py` 的 timeout_watcher 终止容器,
`_on_output` 最终收到 `status="error"`, 前端清除状态栏。

**agent 调用 send_message MCP tool**: 走独立的
`agent.{job_id}.messages` 通道, 不经过 `_on_output`, 不影响状态事件。

**断线重连**: 不会补发之前的状态事件。重连后如果 agent
仍在执行, 用户看到的是 `thinking` (因为 `set_typing` 在 agent 开始时
就设置了), 但看不到具体 tool_use。可接受。

**非 Web channel**: `_on_output` 中 Slack/Telegram 分支不处理
`running`/`tool_use`/`container_starting`, 直接 return, 不影响现有行为。

**Pi 后端 vs Claude 后端**: 两者产出相同的 `ToolUseEvent` 和
`RunningEvent`, NATS bridge 和下游完全一致。唯一差异是 Pi 的
`ToolExecutionStartEvent` 自带 `tool_name` 和 `args`, 而 Claude 需要
从 `AssistantMessage.content` 中手动提取 `ToolUseBlock`。

## 改动文件汇总

| 文件 | 改动内容 |
|------|---------|
| `src/agent_runner/backend.py` | 新增 `ToolUseEvent`、`RunningEvent` 定义; 新增 `tool_input_preview()` 辅助函数; 扩展 `BackendEvent` 联合类型 |
| `src/agent_runner/claude_backend.py` | `AssistantMessage` 分支提取 `ToolUseBlock` → emit `ToolUseEvent`; `SystemMessage(init)` 后 emit `RunningEvent` |
| `src/agent_runner/pi_backend.py` | `_handle_event` 增加 `ToolExecutionStartEvent` → emit `ToolUseEvent`; `start()` 末尾 emit `RunningEvent` |
| `src/agent_runner/main.py` | `on_event` 增加 `ToolUseEvent`/`RunningEvent` → `publish_output` |
| `src/rolemesh/agent/executor.py` | `AgentOutput.status` 类型扩展: 增加 `running` / `tool_use` / `container_starting` |
| `src/rolemesh/agent/container_executor.py` | `execute()` 中 `_runtime.run()` 前通过 on_output 发 `container_starting` |
| `src/rolemesh/main.py` | `_on_output()` 新增 status 事件处理分支; 初始化时注入 `on_queued` 回调 |
| `src/rolemesh/container/scheduler.py` | `enqueue_message_check()` 排队时调 `on_queued` 回调 |
| `src/webui/ws.py` | `_forward_stream()` 检测 status JSON 并透传 |
| `web/src/services/agent-client.ts` | `ServerMessage` 类型增加 `status` |
| `web/src/components/chat-panel.ts` | `handleMessage()` 增加 status case; 新增 `agentStatus` 状态; 渲染状态指示条 |
