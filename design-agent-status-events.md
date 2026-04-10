# Agent 执行状态实时事件 — 设计方案

## 要解决什么问题

用户在 Web UI 里给 agent 发消息后，当前的体验是：

```
用户发消息 → thinking 动画 → (漫长等待，什么都看不到) → 突然出现结果
```

等待期间用户不知道 agent 在干什么、进展到哪了、是不是卡住了。

本方案的目标是让用户看到：

```
用户发消息 → 排队中(第2位) → 容器启动中 → Claude 思考中 → 执行 Bash: npm test → 读取 src/app.ts → 编辑 src/app.ts → 结果
```

## 不做什么

- 不做定时任务进展通知
- 不做审批流
- 不做系统运维事件
- 不做 Slack/Telegram 端的状态推送
- 不做断线重连后的状态补发
- 不做可查询的"当前状态"
- 不新建 EventBus 模块或 NATS stream

## 现有数据链路

理解方案需要先理解现有的数据流。当前一条消息从 agent 到用户屏幕经过 8 个节点：

```
容器内                              orchestrator 进程                              Web UI
─────                               ──────────                                    ──────

agent_runner/main.py
  query() 迭代 SDK 消息
  遇到 ResultMessage 时:
    publish_output()
      ↓
  NATS: agent.{job_id}.results ──→ container_executor.py: _read_results()
                                     解析 JSON, 调回调:
                                       ↓
                                   main.py: _on_output(result)
                                     判断是 Web 还是 Slack/TG
                                     如果是 Web:
                                       ↓
                                   web_nats_gateway.py: send_stream_chunk()
                                       ↓
                                   NATS: web.stream.{binding}.{chat} ──→ ws.py: _forward_stream()
                                                                            解析 JSON, 推到 WebSocket:
                                                                              ↓
                                                                          WebSocket → 浏览器
                                                                              ↓
                                                                          agent-client.ts: onmessage
                                                                              ↓
                                                                          chat-panel.ts: handleMessage()
                                                                            渲染到 UI
```

关键观察: 这条链路已经是实时流式的。现在的问题是容器内只在 `ResultMessage`
时才发数据, SDK 迭代器产出的其他消息类型（`SystemMessage`、`AssistantMessage`
含 `ToolUseBlock`）都被丢弃了。

## 改动思路

不新建任何通道或模块。沿着上面这条链路, 在每一层增加对新消息类型的处理:

1. 容器内: agent_runner 多发几种 status
2. orchestrator: 识别新 status, 转发到 Web channel
3. 前端: 渲染新消息类型

另外, orchestrator 进程内部产生的 `queued` 事件不需要经过容器, 直接从
scheduler 发到 Web channel。

## WebSocket 协议变更

现有消息类型: `session` / `thinking` / `text` / `done` / `error`

新增一种 `status` 消息:

```jsonc
// 消息排队, 前面还有人
{"type": "status", "status": "queued", "position": 2}

// 容器正在启动
{"type": "status", "status": "container_starting"}

// Claude 会话已建立, 开始处理
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

### 第 1 层: agent_runner/main.py (容器内)

位置: `run_query()` 函数的 `async for message in query(...)` 循环。

现状: 只处理 3 种 SDK 消息类型。

改动:

```python
async for message in query(prompt=stream, options=options):
    cls_name = type(message).__name__

    if cls_name == "SystemMessage":
        subtype = getattr(message, "subtype", "")
        data = getattr(message, "data", {})

        if subtype == "init":
            result.new_session_id = data.get("session_id") if isinstance(data, dict) else None
            # ★ 新增: 发送 running 状态
            await publish_output(js, job_id, ContainerOutput(
                status="running", result=None,
            ))

    elif cls_name == "AssistantMessage":
        uuid = getattr(message, "uuid", None)
        if uuid:
            result.last_assistant_uuid = uuid

        # ★ 新增: 提取 ToolUseBlock, 发送 tool_use 状态
        content = getattr(getattr(message, "message", None), "content", None)
        if content is None:
            content = getattr(message, "content", None)
        if isinstance(content, list):
            for block in content:
                block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
                if block_type == "tool_use":
                    tool_name = getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else "")
                    tool_input = getattr(block, "input", None) or (block.get("input") if isinstance(block, dict) else {})
                    input_preview = _tool_input_preview(tool_name, tool_input)
                    await publish_output(js, job_id, ContainerOutput(
                        status="tool_use",
                        result=json.dumps({"tool": tool_name, "input": input_preview}),
                    ))

    elif cls_name == "ResultMessage":
        # ... 现有逻辑不变 ...
```

其中 `_tool_input_preview` 是一个纯函数, 从 tool input 中提取用户可读的摘要:

```python
def _tool_input_preview(tool_name: str, tool_input: dict) -> str:
    """从 tool input 中提取简短预览, 用于状态显示。"""
    if tool_name in ("Read", "Write", "Edit", "Glob", "Grep"):
        return tool_input.get("file_path") or tool_input.get("path") or tool_input.get("pattern") or ""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:80]
    if tool_name in ("WebSearch", "WebFetch"):
        return tool_input.get("query") or tool_input.get("url") or ""
    return ""
```

`ContainerOutput.status` 类型需要从 `"success" | "error"` 扩展, 新增
`"running"` 和 `"tool_use"`。

### 第 2 层: agent/executor.py + container_executor.py (orchestrator 侧解析)

`AgentOutput.status` 当前是 `Literal["success", "error"]`。

改动: 扩展为 `Literal["success", "error", "running", "tool_use"]`。

`container_executor.py` 的 `_parse_container_output` 函数已经是通用的
`str(raw.get("status", "error"))`, 不需要改代码, 只需要类型标注跟上。

`_read_results` 中调 `on_output(parsed)` 也不需要改, 新 status 会原样
传到 `_on_output` 回调。

### 第 3 层: main.py _on_output (orchestrator 侧路由)

现有的 `_on_output` 回调只处理 `result.result` 有值的情况
(即 `status="success"` 且有文本结果)。

改动: 在 `_on_output` 开头增加对 `status` 事件的处理:

```python
async def _on_output(result: AgentOutput) -> None:
    nonlocal had_error, output_sent_to_user

    # ★ 新增: 状态事件 → 仅 Web channel, 发送 status 类型的 stream chunk
    if result.status in ("running", "tool_use"):
        if binding and isinstance(gw, WebNatsGateway):
            status_payload = {"type": "status", "status": result.status}
            if result.status == "tool_use" and result.result:
                tool_info = json.loads(result.result)
                status_payload["tool"] = tool_info.get("tool", "")
                status_payload["input"] = tool_info.get("input", "")
            await gw.send_stream_chunk(
                binding.id, conv.channel_chat_id,
                json.dumps(status_payload),
            )
        _reset_idle_timer()
        return  # 不是最终结果, 不更新 output_sent_to_user

    # ... 现有 result.result 处理逻辑不变 ...
```

注意: `send_stream_chunk` 现在既可以发纯文本 (现有 `text` 场景),
也可以发 JSON 字符串 (新 `status` 场景)。需要在下一层区分。

### 第 4 层: web_nats_gateway.py (NATS 发布)

不需要改动。`send_stream_chunk(content)` 只是把 content 包装成
`WebStreamChunk(type="text", content=...)` 发到 NATS, 不关心内容是什么。

### 第 5 层: webui/ws.py (WebSocket 转发)

`_forward_stream` 现有逻辑:

```python
if data.get("type") == "text":
    await _broadcast(binding_id, chat_id, {"type": "text", "content": data["content"]})
```

改动: 检查 content 是否为 JSON 格式的 status 消息, 如果是则直接透传:

```python
if data.get("type") == "text":
    content = data["content"]
    # ★ 新增: 检测 status 消息并透传
    if content.startswith('{"type":"status"'):
        try:
            status_msg = json.loads(content)
            if status_msg.get("type") == "status":
                await _broadcast(binding_id, chat_id, status_msg)
                await msg.ack()
                continue
        except (json.JSONDecodeError, KeyError):
            pass
    # 普通文本, 走原有逻辑
    await _broadcast(binding_id, chat_id, {"type": "text", "content": content})
```

### 第 6 层: scheduler.py (排队事件, orchestrator 进程内)

排队事件不来自容器, 而是发生在 orchestrator 进程的 `GroupQueue` 中。

当 `enqueue_message_check` 判定需要排队时 (`state.active` 为 True
或并发达到上限), 需要通知用户。

问题: scheduler 当前不知道 binding_id 和 chat_id, 也没有 gateway 引用。

方案: 给 `GroupQueue` 增加一个可选的 `on_queued` 回调, 在 main.py 初始化
时注入:

```python
# scheduler.py
def enqueue_message_check(self, group_jid: str, ...) -> None:
    state = self._get_group(group_jid)
    if state.active:
        state.pending_messages = True
        # ★ 新增: 通知排队
        if self._on_queued:
            position = len(state.pending_tasks) + (1 if state.pending_messages else 0)
            self._on_queued(group_jid, position)
        return
    # ... 并发满时同理 ...

# main.py 初始化时
def _handle_queued(group_jid: str, position: int) -> None:
    # 查找 group_jid 对应的 conversation → binding → gateway
    # 发送 {"type": "status", "status": "queued", "position": position}
    ...
_queue.set_on_queued(_handle_queued)
```

### 第 7 层: container_executor.py (container_starting 事件)

在 `execute()` 方法中, 调用 `self._runtime.run(spec)` 之前, 通过 `on_output`
回调发送 `container_starting` 状态:

```python
# 启动容器前通知
if on_output is not None:
    await on_output(AgentOutput(
        status="container_starting", result=None,
    ))

handle = await self._runtime.run(spec)
```

`AgentOutput.status` 类型标注相应增加 `"container_starting"`。

### 第 8 层: 前端 agent-client.ts + chat-panel.ts

**agent-client.ts** — `ServerMessage` 类型扩展:

```typescript
export type ServerMessage =
  | { type: 'session'; chatId: string; agentId: string }
  | { type: 'thinking' }
  | { type: 'text'; content: string }
  | { type: 'done' }
  | { type: 'error'; message: string }
  // ★ 新增
  | { type: 'status'; status: string; tool?: string; input?: string; position?: number };
```

**chat-panel.ts** — `handleMessage()` 新增 case:

```typescript
case 'status': {
    this.agentStatus = msg;  // 新增 @state 属性
    // 不追加到 messages 数组, 只更新状态栏
    break;
}
case 'text': {
    this.agentStatus = null;  // 收到文本时清除状态
    // ... 现有逻辑 ...
}
case 'done': {
    this.agentStatus = null;  // 完成时清除状态
    // ... 现有逻辑 ...
}
```

**渲染**: 在消息列表底部 (输入框上方) 显示一个状态指示条:

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
1. session         — WebSocket 建连时 (现有)
2. status/queued   — 如果排队了才发 (新增, 来自 scheduler.py)
3. status/container_starting — 容器启动前 (新增, 来自 container_executor.py)
4. thinking        — typing 指示器 (现有, 来自 main.py set_typing)
5. status/running  — Claude 会话建立 (新增, 来自 agent_runner)
6. status/tool_use — 每次工具调用 (新增, 来自 agent_runner, 可连续多次)
7. text            — 最终结果文本 (现有, 来自 agent_runner ResultMessage)
8. done            — 流结束 (现有)
```

如果 agent 在一次执行中调了 10 个工具, 用户会连续收到 10 条
`status/tool_use` 消息, 前端每次替换显示最新的那条。

## 边界情况

**用户在 agent 执行中又发消息**: 现有的多轮机制 (NATS input channel +
MessageStream) 不受影响。新消息通过 `agent.{job_id}.input` 注入,
agent_runner 继续执行, 状态事件继续发送。

**agent 执行超时**: `container_executor.py` 的 timeout_watcher 终止容器,
`_on_output` 最终收到 `status="error"`, 前端清除状态栏。

**agent 调用 send_message MCP tool**: agent 主动发消息给用户,
走的是 `agent.{job_id}.messages` → `_handle_messages` → `_send_via_coworker`
这条独立通道, 不经过 `_on_output`, 不影响状态事件。

**断线重连**: 用户断线后重连, 不会补发之前的状态事件。重连后如果 agent
仍在执行, 用户看到的是 `thinking` (因为 `set_typing` 在 agent 开始时
就设置了), 但看不到具体 tool_use。这是"纯事件流, 不做状态查询"的
预期行为, 可以接受。

**非 Web channel**: Slack/Telegram 的 `_on_output` 分支不处理
`running`/`tool_use` status, 直接 return, 不影响现有行为。

## 改动文件汇总

| 文件 | 改动内容 |
|------|---------|
| `src/agent_runner/main.py` | `run_query()` 中 SystemMessage(init) 发 running; AssistantMessage 提取 ToolUseBlock 发 tool_use; 新增 `_tool_input_preview()` |
| `src/rolemesh/agent/executor.py` | `AgentOutput.status` 类型扩展: 增加 `running` / `tool_use` / `container_starting` |
| `src/rolemesh/agent/container_executor.py` | `execute()` 中 `_runtime.run()` 前通过 on_output 发 `container_starting` |
| `src/rolemesh/main.py` | `_on_output()` 新增 status 事件处理分支; 初始化时注入 `on_queued` 回调 |
| `src/rolemesh/container/scheduler.py` | `enqueue_message_check()` 排队时调 `on_queued` 回调 |
| `src/webui/ws.py` | `_forward_stream()` 检测 status JSON 并透传 |
| `web/src/services/agent-client.ts` | `ServerMessage` 类型增加 `status` |
| `web/src/components/chat-panel.ts` | `handleMessage()` 增加 status case; 渲染状态指示条 |
