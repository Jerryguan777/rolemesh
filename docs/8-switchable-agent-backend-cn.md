# 可切换 Agent 后端架构

本文档描述 RoleMesh 如何在同一套 NATS IPC 协议背后支持多种 agent 后端（Claude SDK 和 Pi），使每个 Coworker 都能运行在不同的 LLM 框架上，而 orchestrator 完全不知道是哪一个在跑。文档涵盖设计目标、引入的抽象、权衡过的取舍，以及实现过程中遇到的坑。

目标读者：希望添加第三种后端、理解跨服务商凭据流，或调试 Pi 后端为何与 Claude 表现不同的开发者。

## 背景：为什么需要两种后端？

最初的设计只在 agent 容器里跑 Claude SDK。Claude SDK 非常适合通用编码场景，对 MCP、skills 和 subagent 都有一流支持。但锁定单一框架是有风险的：

- **厂商锁定**：Anthropic 的 API 成本、功能路线图、限流都会成为单点失败。
- **模型多样性**：有些任务在 OpenAI、Gemini 或开源模型上更便宜或效果更好。Claude SDK 只能和 Anthropic 通信。
- **框架多样性**：不同的 agent 框架各有所长（Pi 的流式事件更干净，会话/分叉语义更好，工具模型更简洁）。

我们需要一种方式，在容器内部运行不同的 agent 框架，而不必重写 orchestrator、NATS 协议、IPC 工具或 channel 网关。无论容器里跑的是哪个框架，对宿主侧都应该是不可见的。

## 设计目标

1. **按 Coworker 选择**：每个 Coworker 在数据库（`coworkers.agent_backend` 列）里选择自己的后端。默认是 `"claude"`。
2. **单一 Docker 镜像**：一个镜像、一个 entrypoint、一条构建流水线。后端在运行时通过环境变量来选择。
3. **宿主侧透明**：orchestrator 不知道也不关心容器里跑的是哪个后端。所有 IPC 仍走现有的 NATS subject。
4. **共享的工具逻辑**：IPC 工具（`send_message`、`schedule_task`、`pause_task`、……）只写一次，每个后端适配一下即可。
5. **服务商中立的凭据管理**：凭据代理负责 Anthropic、OpenAI、Google、Bedrock 的鉴权。容器永远看不到真实的 API key。

## 抽象：AgentBackend 协议

在容器内部，`agent_runner` 被拆成两部分：

1. **NATS 桥接**（`main.py`）：从 KV 读取 `AgentInitData`，订阅 input / interrupt / shutdown subject，把 result / message / task 发布回 orchestrator。与后端无关。
2. **后端**（`claude_backend.py` 或 `pi_backend.py`）：包装具体的 SDK。实现 `AgentBackend` 协议。

```
             ┌──────────────────────────────────────┐
             │  NATS Bridge (main.py)               │
             │  reads AgentInitData from KV,        │
             │  subscribes input / interrupt,       │
             │  handles shutdown request-reply,     │
             │  publishes results / messages / ...  │
             └──────────────┬───────────────────────┘
                            │ BackendEvent
                            ▼
             ┌──────────────────────────────────────┐
             │  AgentBackend Protocol               │
             │  start(init, ctx, mcp_servers)       │
             │  run_prompt(text)                    │
             │  handle_follow_up(text)              │
             │  abort()      — stop current turn    │
             │  shutdown()   — close container      │
             │  subscribe(listener)                 │
             └────┬──────────────────────┬──────────┘
                  │                      │
       ┌──────────▼──────────┐  ┌────────▼────────────┐
       │  ClaudeBackend      │  │  PiBackend          │
       │  wraps              │  │  wraps              │
       │  claude_agent_sdk   │  │  pi.AgentSession    │
       └─────────────────────┘  └─────────────────────┘
```

### BackendEvent 类型

桥接通过 `backend.subscribe(listener)` 监听后端事件：

| 事件 | 用途 |
|-------|---------|
| `ResultEvent(text, usage, new_session_id)` | 最终（或中间）的 assistant 输出。桥接将其发布到 `agent.{job}.results`。 |
| `SessionInitEvent(session_id)` | 后端已建立会话 ID。桥接更新其 tracker。 |
| `CompactionEvent` | 后端即将压缩上下文（触发归档 hooks）。 |
| `ErrorEvent(error, usage)` | 不可恢复的后端错误。桥接发布错误状态。 |
| `RunningEvent(stage)` | 提供给 WebUI 的进度信号（"running"、"container_starting" 等）。详见 [`event-stream-architecture.md`](event-stream-architecture.md)。 |
| `ToolUseEvent(tool_name, input_preview)` | 一次工具调用即将触发。以 "tool_use" 状态呈现给 WebUI。 |
| `StoppedEvent(usage)` | agent 已确认停止并空闲。orchestrator 把它作为真正意义上的 "agent 已确认中止" 信号——见 [`backend-stop-contract.md`](backend-stop-contract.md)。 |
| `SafetyBlockEvent(reason, usage)` | 安全管线阻止了这一轮。与 `ResultEvent` 区分开，便于 orchestrator 正确为审计标记该消息。 |

后端发出事件；桥接把它们翻译成 NATS 消息。后端从不直接接触 NATS。

### `abort()` 与 `shutdown()`

两个语义截然不同的取消方法：

- **`abort()`** —— 停止**当前一轮**，但保持容器存活。WebUI 上的 Stop 按钮使用它（用户可以立刻通过 follow-up 重新指挥；没有冷启动开销）。
- **`shutdown()`** —— 关闭**容器本身**。orchestrator 在空闲超时、抢占式或调度器驱动的清理场景下使用它。

两种后端的实现方式不同（Claude 使用抢占式的 `Task.cancel()`；Pi 使用协作式的 `asyncio.Event` 检查，在 provider 流的 chunk 之间检查），但无论内部机制如何，两者必须给出相同的可观测行为。完整契约——"被中止的那一轮不能有迟到事件"、"被中止的上下文不能泄漏到下一轮"、"不能有遗留的 `_aborting` 标志压制后续 follow-up"——都写在 [`backend-stop-contract.md`](backend-stop-contract.md) 里。这份契约是后端切换的承重件：只要新后端遵守它，orchestrator 就不需要知道当前跑的是哪个后端。

### 为什么不用"不同后端用不同容器"？

被否决，原因如下：

- 维护两份 Dockerfile 会把构建/CI 的工作量翻倍。
- NATS 桥接代码是 100% 共享的；把它复制到两个镜像里会引入漂移。
- 按 Coworker 选择后端意味着两种后端在镜像拉取时都必须可用。

单一镜像加上通过 `AGENT_BACKEND=claude|pi` 环境变量的运行时分发要简单得多。`__main__.py` 读取该环境变量并 import 对应的后端类。

## 宿主侧分发

宿主侧在 `_executors` dict 里为每个后端维护一个 `ContainerAgentExecutor`，以后端名为 key。orchestrator 在分发时查 `_executors[coworker.agent_backend]`。两个 executor 都指向同一个 Docker 镜像——只有 `extra_env`（特别是 `AGENT_BACKEND=claude|pi`）和少量 volume mount 开关（例如 `skip_claude_session`）不同。

完整设计——按 Coworker 分发、单镜像的理由、`BACKEND_CONFIGS` 映射——在 [`3-agent-executor-and-container-runtime.md`](3-agent-executor-and-container-runtime.md) 中。这里要强调的是：**宿主侧除了挑选正确的 executor，没有任何后端相关的逻辑**。

## 共享的工具逻辑

IPC 工具（`send_message`、`schedule_task`、`pause_task`、`resume_task`、`cancel_task`、`update_task`、`list_tasks`）是 `agent_runner/tools/rolemesh_tools.py` 里的纯异步函数：

```python
async def send_message(args: dict, ctx: ToolContext) -> ToolResult: ...
async def schedule_task(args: dict, ctx: ToolContext) -> ToolResult: ...
# ...
```

`ToolContext` 携带工具所需的一切（NATS JetStream 客户端、`job_id`、租户 / Coworker ID、权限）。每种后端有一个轻量级适配器：

- **`tools/claude_adapter.py`** —— 用 Claude SDK 的 `@tool` 装饰器包装每个函数，并通过 `create_sdk_mcp_server("rolemesh", ...)` 把它们注册为进程内 MCP server。
- **`tools/pi_adapter.py`** —— 把每个函数包装为 `pi.agent.types.AgentTool` 的子类。

工具的业务逻辑（参数校验、NATS 发布格式、权限检查、与 results 流的去重）只有一处实现。后端适配器只翻译签名和返回格式。

完整的每个工具的 wire 格式——subject、payload 结构、orchestrator 侧的授权——在 [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md) 中（Channel 4 消息、Channel 5 任务操作、Channel 6 snapshot 读取）。

### 设计权衡：为什么不在 Pi 中用统一工具接口？

Pi 有自己的 `AgentTool` ABC，Claude SDK 有自己的 `@tool` 装饰器。我们本可以强迫 Pi 用 Claude 的格式（反过来也行），从而省去适配器。但我们没有，原因是：

- 每个框架的工具接口都有框架特有的能力（Claude SDK 有 MCP 命名空间，Pi 有用于 UI 的 `AgentToolResult.details`）。
- 强制使用最小公倍数接口会让框架细节泄漏到另一边的后端。
- 适配器代码每个后端只有约 60 行。维护成本很低。

## Pi 中的 MCP 集成

Claude SDK 内置 MCP 客户端支持，Pi 没有。我们没有去给 Pi 的核心加 MCP 支持（侵入性大，会与上游分叉），而是把 `src/pi/mcp/` 作为 sidecar 模块加进来：

- `pi.mcp.client.McpServerConnection` —— 管理一个 MCP server 连接（SSE 或 streamable-HTTP 传输）。
- `pi.mcp.tool_bridge.load_mcp_tools(specs, user_id)` —— 连接到 server，发现工具，并把每个工具包装为 `pi.agent.types.AgentTool`。

在 Pi 的 agent 循环里，远端 MCP 工具与本地工具看起来完全一样。包装器的 `execute()` 把调用通过 wire 转发出去。Wire 格式（URL 重写、`auth_mode`、按用户注入 token）在 [`7-external-mcp-architecture.md`](7-external-mcp-architecture.md) 中描述；本节只讲 Pi 后端如何接入。

### 为什么用 sidecar 而不是 Pi 扩展？

Pi 有一个扩展系统（`ExtensionAPI.register_tool(ToolDefinition)`），乍一看是自然契合。我们否决了它，原因如下：

- Pi 的扩展是基于文件系统发现的（带工厂函数的 Python 文件）。MCP server 配置来自 NATS 的 `AgentInitData`，不来自文件系统。
- Pi 的扩展系统是为用户插件设计的，不是为框架级基础设施设计的。语义错位。
- 扩展生命周期没有合适的关闭 hook 来关闭 MCP 连接。

sidecar 方案让 MCP 相关的事情自成一体，也让我们能跟踪 Pi 上游而不冲突。

### 被废弃的 SDK 函数陷阱

`mcp` Python SDK 1.27 从 `mcp.client.streamable_http` 导出了两个函数：

- `streamablehttp_client(url, headers=...)` —— 接受 headers，但带 `@deprecated`。
- `streamable_http_client(url, http_client=...)` —— 推荐使用，**不接受** headers。

我们使用未废弃的那个函数，并通过自定义 `httpx.AsyncClient` 注入 headers。如果你看到 `unexpected keyword argument 'headers'`，检查一下调用的是哪个函数。

## 跨后端的凭据流

凭据代理位于每一次出站 LLM API 调用之前。容器拿到的永远是占位 API key；代理在 HTTP 层注入真实 key。代理通过路径前缀（`/proxy/openai/...`、`/proxy/google/...`、Anthropic 的兼容路径 `/v1/...`）区分服务商。每个 LLM SDK 读取自己的 `*_BASE_URL` 环境变量，指向代理。

新增一个服务商意味着在代理的服务商注册表里加一条记录——不需要改 agent 后端。详细的代理机制（路由布局、按服务商的配置、MCP 转发、`auth_mode`）在 [`7-external-mcp-architecture.md`](7-external-mcp-architecture.md) 中；按用户的 IdP token 模型在 [`6-auth-architecture.md`](6-auth-architecture.md) 中。

对于后端切换而言关键的一点是：**每个后端的 SDK 只需要在其环境中有正确的 `*_BASE_URL` 和一个占位 key**。无论后端要和 Anthropic、OpenAI 还是 Bedrock 通信，走的都是同一条经过代理的路径。

## Pi 特有的坑

集成 Pi 时暴露出几个值得记录的问题，免得后来者重复同样的调试。

### 1. Provider 不会自动注册

Pi 的 LLM provider（Anthropic、OpenAI、Google）放在一个注册表里，但默认是空的。必须在第一次 LLM 调用前显式调用 `register_built_in_api_providers()`。否则 `stream()` 会抛 `ValueError: No API provider registered for api: anthropic-messages`——但 Pi 的错误处理捕获了它，把它存到 `assistant_message.error_message`，所以外层代码看到的是"query 顺利结束但没有任何输出"。

我们在 `PiBackend.start()` 顶部调用 `register_built_in_api_providers()`。

### 2. Pi 的 SDK 里 `custom_tools` 只是个 stub

`CreateAgentSessionOptions.custom_tools` 会被存到 session config 上，但从来不会被传到 `agent.set_tools()`。Pi 的 agent 状态以空工具列表起步——不管你给 `custom_tools` 传什么，LLM 都看不到。

这是 Pi Python 移植版本中一个已知的缺口（TypeScript 原版通过交互模式的扩展系统把工具接好）。我们在 `pi/coding_agent/core/sdk.py` 中的补丁会把按 `initial_tool_names` 过滤后的内置工具加上 `custom_tools` 组装起来，并设置到 agent 的初始状态上。

### 3. 每次 prompt 多个 `TurnEndEvent`

Pi 在每一轮之后都会发出一个 `TurnEndEvent`，包括中间的工具调用轮次。如果我们在每个 `TurnEndEvent` 上都发 `ResultEvent`，宿主在每条用户消息上会收到多次"成功"结果，这会反复触发 `notify_idle`，并可能造成调度竞争。

修复：跨所有 `TurnEndEvent` 收集最后一段 assistant 文本，在 `session.prompt()` 返回之后只发一次 `ResultEvent`。

### 4. JetStream 临时消费者重新投递

原始循环在每次迭代时都创建一个新的 `js.subscribe()`。JetStream 把每次 `subscribe` 调用视为一个新的临时消费者，对之前的 ack 没有记忆。Follow-up 消息会在每次循环迭代时被重新投递给新的消费者，造成无限处理循环。

修复：在 `run_query_loop()` 顶部订阅一次，跨迭代复用，在 `finally` 块里取消订阅。

为什么 Claude 后端没受影响：Claude SDK 的 `query()` 迭代器通过 `MessageStream` 的 push 队列在多次 prompt 之间一直存活。整个 session 就是外层循环的一次迭代。Pi 的 `session.prompt()` 每一轮之后就返回，把这个潜伏的 bug 暴露了出来。

### 5. `model.base_url` 的坑

Pi 的模型注册表在每个 OpenAI 模型上都硬编码了 `base_url="https://api.openai.com/v1"`。当 Pi 创建 SDK client 时：

```python
openai.AsyncOpenAI(api_key=key, base_url=model.base_url or None)
```

如果 `model.base_url` 设了值，SDK 会忽略 `OPENAI_BASE_URL` 环境变量。请求会带着占位 key 直奔 OpenAI，得到 401。

**修复**：在 `pi_backend.py` 中，在解析出 model 之后，用环境变量里的代理 URL 覆盖 `model.base_url`：

```python
_PROXY_ENV_MAP = {"openai": "OPENAI_BASE_URL", "anthropic": "ANTHROPIC_BASE_URL"}
proxy_env = _PROXY_ENV_MAP.get(model.provider)
if proxy_env and os.environ.get(proxy_env):
    model.base_url = os.environ[proxy_env]
```

这是针对 Pi 设计选择的临时绕过；如果/当 Pi 的 provider 原生读取环境变量，这段就可以移除。

## 何时添加第三种后端

流程如下：

1. 写 `agent_runner/new_backend.py` 实现 `AgentBackend`（包括 [`backend-stop-contract.md`](backend-stop-contract.md) 中的 abort/shutdown 契约）。
2. 写 `agent_runner/tools/new_adapter.py` 包装共享的工具函数。
3. 在 `rolemesh/agent/executor.py` 里加 `NEW_BACKEND = AgentBackendConfig(name="new", ...)`。
4. 在 `BACKEND_CONFIGS` 里注册它。
5. 如果新框架使用了不同形态的 LLM API，在凭据代理的 `_build_provider_registry()` 里加一条 provider 配置。
6. 如果该框架的 SDK 读取的是另一个 `*_BASE_URL` 环境变量，在 `rolemesh/container/runner.py` 里注入对应环境变量。

你**不需要**改的东西：

- NATS 协议
- 宿主 orchestrator 的路由
- 共享 IPC 工具的业务逻辑
- channel 网关
- 数据库 schema

## 测试策略

后端集成由三层测试覆盖：

1. **单元测试**（`tests/test_agent_runner/test_rolemesh_tools.py`、`test_pi_adapter.py`、`test_event_translation.py`）—— 工具校验、事件翻译、适配器行为。不依赖 NATS，不依赖真实 LLM。
2. **NATS 集成测试**（`tests/test_agent_runner/test_nats_bridge.py`）—— 真实的 NATS server（来自 `docker-compose.dev.yml`），配合一个模拟 agent 事件的 `FakeBackend`。端到端地验证 agent IPC subject。
3. **手动 E2E** —— 真实 channel + 真实 LLM API 调用。用一个跑 Claude 后端的 Coworker 和另一个跑 Pi 后端的 Coworker，并排对比行为。

`FakeBackend` 模式是可复用的：任何新后端只要实现 `AgentBackend` 并返回预设响应，就能用同一套 NATS 桥接测试去验证。

## 设计决策汇总

| 决策 | 原因 |
|----------|-----|
| 单一 Docker 镜像，通过环境变量分发 | 避免在多个镜像间复制 NATS 桥接代码 |
| 容器内的 `AgentBackend` 协议 | 隔离后端逻辑；桥接保持与框架无关 |
| 共享工具函数 + 每后端适配器 | 工具业务逻辑（校验、NATS 格式）只活一处 |
| Pi 的 MCP 作为 sidecar（`src/pi/mcp/`） | 避免修改 Pi 核心；避免与扩展系统的语义错位 |
| 停止契约与实现解耦 | 只要契约成立，每个后端可以自由选择抢占式或协作式取消 |
| 按 Coworker 覆盖后端 | 生产环境可以混跑多种后端，做 A/B 测试或成本优化 |
