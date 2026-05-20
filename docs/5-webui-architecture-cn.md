# WebUI 架构

本文档说明 RoleMesh 基于浏览器的 WebUI 通道的设计——为什么它是一个独立进程、它如何通过 NATS 与 orchestrator 通信，以及这些决策背后的权衡。

## 背景

RoleMesh 支持 Telegram 和 Slack 作为消息通道。每一种都有一个网关（TelegramGateway、SlackGateway），实现 `ChannelGateway` 协议并运行在 orchestrator 进程内部。这种方式之所以可行，是因为 Telegram 和 Slack 网关是事件驱动的监听器——它们从外部 API 接收消息，然后调用 orchestrator 的回调。

新增一个基于浏览器的 WebUI 引入了不同的挑战：WebUI 需要一个 **HTTP/WebSocket 服务器**，供浏览器连接。这个服务器必须处理实时流式传输、静态文件服务、REST API（会话历史、admin 面板）以及真实的认证流程——这些表面并不适合放在进程内的网关形态里。

## 为什么用独立进程？

我们考虑了两种架构：

### 选项 A：嵌入到 orchestrator 中

```
Browser ←WebSocket→ [Orchestrator + WebSocket handler] ←NATS→ [Agent Container]
```

WebSocket 服务器运行在 orchestrator 进程内部，与 Telegram/Slack 网关相同。

**优点：**
- 简单——直接函数调用，没有序列化开销
- 单一进程部署

**缺点：**
- 把 HTTP 相关问题（路由、中间件、认证、静态文件）耦合进 orchestrator
- 无法独立于 orchestrator 伸缩 WebUI
- 难以嵌入已有的 SaaS 平台——它们需要的是标准的 REST/WebSocket API，而不是函数调用接口
- 为未来功能（会话管理、admin 面板）增加 REST API 会让 orchestrator 膨胀
- 没有 OpenAPI 文档供第三方集成

### 选项 B：独立的 FastAPI 进程（已选择）

```
Browser ←WebSocket→ [FastAPI service] ←NATS→ [Orchestrator] ←NATS→ [Agent Container]
```

FastAPI 作为独立进程运行。与 orchestrator 的通信完全通过 NATS。

**优点：**
- 清晰的职责分离——orchestrator 处理 agent 生命周期，FastAPI 处理 HTTP
- FastAPI 提供自动 OpenAPI 文档、Pydantic 校验、依赖注入
- 可以独立伸缩（多个 FastAPI 实例放在负载均衡器后面）
- 易于嵌入已有的 SaaS——它就是标准的 HTTP/WebSocket API
- 第三方可以从 OpenAPI 规范生成客户端 SDK
- 未来的 REST API（会话管理、计费、admin 面板）自然地落在这里

**缺点：**
- 更复杂——两个进程、NATS 消息序列化
- 略高的延迟（NATS 跳转 vs 直接函数调用）
- 需要定义并维护一个 NATS 协议

我们选择了选项 B，因为 RoleMesh 的设计目标是 **agent-as-a-service 平台**。WebUI 只是 API 的其中一个消费者——未来的消费者还包括第三方 SaaS 集成、移动应用和 admin 面板。一个良好定义的 API 边界值得这部分额外的复杂度。

## 为什么用 NATS（而不是直接读写数据库）？

FastAPI 服务可以完全绕开 NATS，直接写入数据库，再让 orchestrator 轮询新消息。我们拒绝了这个方案，原因是：

1. **延迟**——轮询会增加数秒级延迟。NATS 在毫秒级内就能投递消息。
2. **流式传输**——agent 的输出必须实时流到浏览器。orchestrator 通过 NATS（`agent.{job_id}.results`）接收 agent 的分块输出，必须立即转发出去。流式分块没有数据库表可供轮询。
3. **一致性**——orchestrator 管理会话状态、session ID、agent 调度和容器生命周期。如果 FastAPI 直接写数据库，就会绕过所有这些逻辑，并产生竞态条件。
4. **一致性对齐**——orchestrator 已经使用 NATS 进行 agent IPC。把 web 通道的 subject 加进同一套基础设施是自然的。

FastAPI 的确直接读取数据库，但仅用于一件事：**token 校验和会话历史读取**（通过 RLS 绑定到发起请求的租户）。这是只读路径，不会干扰 orchestrator 的权威状态。

## NATS Subject 设计

一个独立的 JetStream 流 `web-ipc` 承载四种 subject 模式：

| Subject | 方向 | 用途 |
|---|---|---|
| `web.inbound.{binding_id}` | FastAPI → Orchestrator | 用户发来一条消息 |
| `web.stream.{binding_id}.{chat_id}` | Orchestrator → FastAPI | 流式文本分块 |
| `web.typing.{binding_id}.{chat_id}` | Orchestrator → FastAPI | agent 开始/停止处理 |
| `web.outbound.{binding_id}.{chat_id}` | Orchestrator → FastAPI | agent 的完整回复 |

### 为什么用独立的流？

我们使用独立的 `web-ipc` 流，而不是把 subject 加进已有的 `agent-ipc` 流，原因是：

- **不同的保留需求**——agent IPC 的消息是瞬时的（被 orchestrator 消费一次）。Web 消息可能需要不同的保留策略，用于调试或回放。
- **不同的消费者**——agent IPC 仅由 orchestrator 消费。Web 的 subject 由 FastAPI 消费。独立的流可以避免消费者偏移量被相互污染。
- **运维清晰度**——`nats stream info web-ipc` 可以立刻看到 web 通道的健康状况，而不会与 agent 流量混在一起。

### 为什么 subject 中包含 `binding_id` 和 `chat_id`？

- `binding_id` 标识这条消息属于哪个 Coworker 的 web 通道。它对应数据库中 `channel_bindings` 表的一行。FastAPI 订阅与其服务的 binding ID 相匹配的 subject。
- `chat_id` 标识具体的浏览器 session（会话）。把它放进 subject 可以让 FastAPI 不用解析负载就能把消息路由到正确的 WebSocket 连接。

## orchestrator 侧：WebNatsGateway

在 orchestrator 侧，`WebNatsGateway` 满足与 `TelegramGateway` 和 `SlackGateway` 相同的 `ChannelGateway` 协议。

```python
_gateways = {
    "telegram": TelegramGateway(on_message=_handle_incoming),
    "slack":    SlackGateway(on_message=_handle_incoming),
    "web":      WebNatsGateway(on_message=_handle_incoming, transport=_transport),
}
```

它与其它网关的关键区别：

| | TelegramGateway | SlackGateway | WebNatsGateway |
|---|---|---|---|
| 接收消息来源 | Telegram API（轮询） | Slack Socket Mode | NATS `web.inbound.*` |
| 发送消息途径 | Telegram Bot API | Slack Web API | NATS `web.outbound.*` |
| 输入中状态指示 | `ChatAction.TYPING` | 不支持 | NATS `web.typing.*` |

经过网关层之后，orchestrator 对这三种网关的处理方式完全相同——同样的消息路由、会话查询、agent 调度和输出处理。

### 流式传输：为什么新增方法？

`ChannelGateway` 协议定义了 `send_message(binding_id, chat_id, text)`，用于发送一段**完整**的文本。对 Telegram 和 Slack 来说这是正确的——你只发送一条带有完整响应的消息。

对于 WebUI，我们想要**流式**——浏览器应该看到文本随着 agent 生成而出现，而不是等待整段响应。为了在不修改 Telegram/Slack 所用协议的前提下支持这点，`WebNatsGateway` 增加了两个额外方法：

- `send_stream_chunk(binding_id, chat_id, content)` —— 发布一个文本分块
- `send_stream_done(binding_id, chat_id)` —— 表示响应已结束

agent 输出回调会检查网关类型：

```python
if isinstance(gw, WebNatsGateway):
    await gw.send_stream_chunk(binding.id, chat_id, text)
else:
    await gw.send_message(binding.id, chat_id, text)
```

这样既不影响 Telegram/Slack，又为 WebUI 启用了流式传输。

## FastAPI 侧：WebSocket 生命周期

当浏览器打开一个 WebSocket 连接时：

1. **认证**：FastAPI 通过 `webui.auth.authenticate_ws()` 校验请求（参见下方的"认证"小节）。
2. **会话**：使用一个 UUID 作为 `chat_id`（由客户端传入或自动创建）。浏览器在连接时收到 session 信息。
3. **订阅**：FastAPI 创建作用域为该 `(binding_id, chat_id)` 的 NATS 订阅：
   - `web.stream.{binding_id}.{chat_id}` → 把 `text`/`done` 推送到浏览器
   - `web.typing.{binding_id}.{chat_id}` → 把 `thinking` / `done` 推送到浏览器
4. **消息循环**：浏览器发送 `{ type: "message", content }`，FastAPI 发布到 `web.inbound.{binding_id}`。
5. **断开**：清理 NATS 订阅。

### 为什么按连接创建订阅？

每个 WebSocket 连接订阅包含其自身 `chat_id` 的 subject。这意味着：

- 标签页 A 的消息绝不会到达标签页 B（subject 级别的隔离）
- 不需要客户端做过滤
- 由 NATS 处理路由，而不是应用代码
- 订阅自动作用域化，并在断开时清理

替代方案——按 binding_id 单一通配符订阅 + 客户端分发——也能工作，但增加了复杂度，并使消息投递保证更难推理。

## 会话模型

每个浏览器标签页由一对 `(binding_id, chat_id)` 标识，该组合对应 `conversations` 表中的一行。打开一个新标签页且不带 `chat_id` 查询参数会启动一个新会话；打开带 `?chat_id=...` 的标签页会重新加入一个已有会话。

```
(binding_id, chat_id) → one conversation in the database
```

会话是持久化的：历史记录在页面刷新后仍然存在，并显示在侧边栏中。两个 REST 端点支持这一点：

- `GET /api/conversations?agent_id=&token=` —— 列出当前用户可见的会话
- `GET /api/conversations/{chat_id}/messages?agent_id=&token=` —— 重新加入时回放历史

### 为什么按标签页建会话（而不是共享一个 "default" 聊天）？

为每个 binding 固定一个 `chat_id`（类似 Telegram 私聊那样的单一共享会话）会有两个问题：

- **流式传输混乱**——如果两个标签页共享同一个会话，且用户从标签页 A 发送了一条消息，流式响应也会出现在标签页 B 中。如果用户同时正在标签页 B 输入，体验会非常混乱。
- **侧边栏 UX**——产品需要一个 ChatGPT 风格的历史会话侧边栏。按标签页生成的 UUID 与之天然匹配——每个会话都有自己的 ID，侧边栏让用户可以在它们之间切换。

代价（页面刷新会丢失*当前标签页*正在进行的 WebSocket session）只针对那一个标签页；会话本身存在数据库中，并可通过侧边栏重新打开。

## 认证

认证位于一个可插拔的 `AuthProvider`（External JWT / Builtin / OIDC）之后。WebUI 进程通过 `AUTH_MODE` 配置具体的 provider，并对外暴露浏览器实际通信的表面。

进入 WebUI 有三条有效的认证路径，按优先级排序：

### 1. URL `?token=...`（SaaS 嵌入 + 开发）

浏览器以 `?agent_id=<uuid>&token=<jwt-or-bootstrap>` 打开。FastAPI 的 `authenticate_ws(token)` 处理器会按以下方式解析：

1. **Bootstrap 管理员快捷通路**——若 `token == ADMIN_BOOTSTRAP_TOKEN`（环境变量），请求会被作为 `default` 租户的所有者接受。这是开发 / 首次运行 / 冒烟测试路径。
2. **已配置的 `AuthProvider`**——否则该 token 由当前激活的 provider 校验（External JWT 校验 SaaS 签发的 JWT；Builtin 检查 RoleMesh 签发的凭证；OIDC 校验 IdP 签发的 `id_token`）。

该 token 会被放入 agent 的 `AgentInitData` 中转发给 orchestrator，使 MCP 工具调用可以把用户身份带到下游——参见 [`auth-architecture.md`](auth-architecture.md) 和 [`external-mcp-architecture.md`](external-mcp-architecture.md)。

### 2. OIDC PKCE 登录（`AUTH_MODE=oidc`）

当配置了 OIDC 时，WebUI 进程会注册 `oidc_routes` 路由器，对外暴露：

| 端点 | 用途 |
|---|---|
| `GET /api/auth/config` | 前端读取 IdP 发现信息（issuer、authorization_endpoint、client_id、audience、scope）以发起登录 |
| `POST /api/auth/exchange` | PKCE code → `id_token` + httpOnly 刷新 cookie |
| `POST /api/auth/refresh` | 刷新 cookie → 新的 `id_token`（由 `scheduleRefresh` 在过期前 5 分钟调用） |
| `POST /api/auth/logout` | 清理刷新 cookie |

前端（`web/src/services/oidc-auth.ts`）驱动整个流程：`fetchAuthConfig()` → `startLogin()`（生成 PKCE verifier + challenge，重定向到 IdP）→ 回调页捕获 code → `handleCallback()` 通过 `/api/auth/exchange` 进行交换 → `id_token` 落入 `sessionStorage`，刷新 cookie 以 httpOnly 形式落地。`scheduleRefresh()` 设置一个定时器，在过期前静默续签。

IdP 签发的 token 会被镜像到主机侧的 `TokenVault`，使 orchestrator（以及外部 MCP 服务器）可以代表用户调用 API，而不会把刷新材料暴露给 agent 容器。

### 3. 已存储 token 的回放

如果 OIDC 流程已把一个 token 存入 `sessionStorage` 且尚未过期，SPA 会无需重新登录就解析认证（参见 `app.ts:resolveAuth()`）。

完整的身份模型——用户角色、agent 权限、IdP 集成选择——在 [`auth-architecture.md`](auth-architecture.md) 中。WebUI 进程是这些概念的传输层；它并不拥有它们。

## 不仅仅是聊天：admin 面板

WebUI 进程不只是一个聊天面板。同一个 FastAPI 应用还提供平台的 REST + UI admin 面板。把管理端点放在这里（而不是 orchestrator 中）是"为什么用独立进程"决策的直接延伸：HTTP 相关的事情就应该在 HTTP 这一侧。

admin 面板按模块分组——每一组的权威文档都在自己的文件中：

| 表面 | 端点 | 归属文档 |
|---|---|---|
| 会话历史 | `GET /api/conversations`、`GET /api/conversations/{chat_id}/messages` | 本文件 |
| Coworker / agent CRUD | `/api/admin/agents/*`（含每个 agent 下嵌套的 skills CRUD） | [`auth-architecture.md`](auth-architecture.md)、[`skills-architecture.md`](skills-architecture.md) |
| 安全规则 | `/api/admin/safety/checks`、`/safety/rules`、`/safety/decisions`、`/safety/decisions.csv`、`/safety/rules/{id}/audit` | [`safety/safety-framework.md`](safety/safety-framework.md) |
| 审批策略 + 决策 | `/api/admin/approval/*` | [`approval-architecture.md`](approval-architecture.md) |
| OIDC 认证 | `/api/auth/{config,exchange,refresh,logout}` | 上文的"认证"小节 |

前端把这些挂载为 hash 路由的页面，与聊天面板并列（`#/admin/safety/rules`、`#/admin/safety/decisions`、……）。hash 路由避免了在 FastAPI 的静态处理器中需要 SPA history-API 回退——同一个 `index.html` 在开发服务器（Vite，端口 5173）和 FastAPI 静态挂载（端口 8080）中都可工作，无需配置漂移。

## 前端

前端基于 Lit（Web Components）、Vite 和 Tailwind CSS 构建。路由基于 hash；管理页面挂载在 `#/admin/...`，与聊天面板并列。

### 为什么用 Lit，而不是 React/Vue？

- **轻量**——Lit 编译为原生 Web Components。没有虚拟 DOM，没有框架运行时。整个前端构建后约 70KB（gzip）。
- **无构建复杂度**——没有 JSX 转换，没有特殊的编译器插件。仅仅是 TypeScript + 标准 DOM API。
- **可嵌入**——Web Components 可以放进任意现有页面。当第三方 SaaS 平台嵌入 RoleMesh 时，他们可以把 `<rm-chat-panel>` 当作自定义元素使用，不会与框架冲突。

### 为什么在 Web Components 项目里用 Tailwind？

Lit 组件通常使用 Shadow DOM 配合作用域 CSS。我们改为渲染到 light DOM（`createRenderRoot() { return this; }`），这让 Tailwind 的全局工具类正常生效。代价是失去了 Shadow DOM 的封装；由于 WebUI 是一个独立页面（并非嵌入到其它应用的 CSS 内部），这是可以接受的。

## 权衡总结

| 决策 | 已选 | 备选 | 原因 |
|---|---|---|---|
| 进程模型 | 独立的 FastAPI | 嵌入到 orchestrator 中 | SaaS 集成、API 优先、独立伸缩 |
| 通信 | NATS | 直接 DB / 函数调用 | 实时流式、一致性、与已有 IPC 对齐 |
| 流式传输 | 按分块 NATS 发布 | 通过 send_message 发送完整文本 | 实时 UX，类似 ChatGPT |
| 会话模型 | 按标签页 UUID，侧边栏列出 | 共享固定 ID | 避免流式传输冲突、侧边栏 UX |
| 前端框架 | Lit（Web Components） | React / Vue | 轻量、可嵌入 |
| 路由 | 基于 hash | History API | 在开发 / 静态挂载之间无需 SPA 回退 |
| 认证 | OIDC PKCE + AuthProvider 抽象 | 单一共享 API token | 多租户、IdP 集成、token 轮换 |

## 相关文档

- [`auth-architecture.md`](auth-architecture.md) —— `AuthProvider` 抽象、agent + 用户权限、OIDC 细节
- [`nats-ipc-architecture.md`](nats-ipc-architecture.md) —— orchestrator 侧的 NATS 协议；`web-ipc` 流与 `agent-ipc` 并列存在
- [`safety/safety-framework.md`](safety/safety-framework.md) —— 安全管理页面背后的端点
- [`approval-architecture.md`](approval-architecture.md) —— 审批管理页面背后的端点
- [`skills-architecture.md`](skills-architecture.md) —— 每个 agent skills CRUD 背后的端点
