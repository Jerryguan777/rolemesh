# 整体架构

这是 RoleMesh 架构文档的入口。本文涵盖了项目背景与目标、端到端的系统图、各主要模块的职责、模块背后的设计取舍、项目从 NanoClaw 演进而来的历史，以及指向各模块深入文档的链接。

如果你是第一次阅读这个代码库，请从这里开始。每个模块章节末尾都附有一个指向其详细设计文档的链接。

---

## 背景

当今大多数 Agent 平台都属于以下两类之一：

- **封闭式 SaaS**（Claude Projects、Devin、ChatGPT Teams）—— 易于使用，但你不拥有数据，无法本地部署，并且 Agent 也没有办法接入到你团队现有的通道里。
- **单租户库**（LangChain、AutoGPT、CrewAI）—— 你拥有代码，但其他一切都得自己搭：租户隔离、沙箱化、通道集成、凭据管理、审计、审批闸门。

当你想要一个能处理**真实公司数据**、能在团队**通道里对话**、并且**不会泄漏凭据**的 AI Coworker (coworker) 时，这两种形态都不适合。RoleMesh 正是为填补这个空缺而存在：自托管、AGPL 许可证、从数据库底层就是多租户的、由架构而非外挂过滤器实现的沙箱化。

最初的代码线起源于 [NanoClaw](https://github.com/qwibitai/nanoclaw)，一个面向单用户的 TypeScript Claude 助手。RoleMesh 是 Python 重写版本，将其发展成了一个多租户平台 —— 完整的步骤列表见下方的"项目演进"。

---

## 目标

1. **从数据库底层即多租户。** 在每张租户作用域的表上由 Postgres 行级安全 (RLS) 强制实施租户隔离，配合双连接池架构（`rolemesh_app` `NOBYPASSRLS` + `rolemesh_system` `BYPASSRLS`），从而默认姿态就是失败即拒。一个有 bug 的查询命中 app 池后，无法意外地跨租户泄漏数据。
2. **由架构而非外挂过滤器实现的沙箱化。** 三个相互独立的层（容器加固 + 内容安全管道 + 网络出向收口点）—— 每一层都假设其他层可能会失效。
3. **两套可互换的 Agent 运行时。** 在每个 Coworker 的粒度上选择 Claude SDK 或 Pi（从 pi-mono 移植而来的、开源的、多供应商运行时）。在不重写工具、通道或 orchestrator 的前提下切换后端。
4. **多种人类通道。** 开箱即用支持 WebUI、Telegram 和 Slack，背后是统一的通道网关协议，因此新增一个通道（Teams、Discord……）只是局部修改。
5. **真正的人类审批流。** 不仅限于聊天：Agent 能够采取真实动作（退款、调价、授予访问权限），但策略可以将任何工具调用路由到一个人类审批闸门，让其在执行前被审查。
6. **每个 Coworker 独立的能力面。** 每个 Coworker 拥有自己的 MCP 工具、skill (skill)、系统提示词和权限画像 —— 因此一个租户可以拥有一个能调度任务但不能委派给其他 Agent 的"运营机器人"，而另一个租户可以拥有一个两件事都能做的"经理机器人"。

---

## 架构图

![整体架构](diagrams/Overall-Architecture.svg)

该图展示了一个租户所对应的组件。在真实部署中，orchestrator 同时承载多个租户，每个租户都有自己的 Agent 容器、通道绑定和数据库行作用域。

---

## 模块职责

### orchestrator (`src/rolemesh/main.py`)

中心进程。拥有 NATS 连接、Postgres 连接池、通道网关、调度器、Safety RPC 服务器、审批引擎以及 Agent 派生循环。所有其他模块要么运行在 orchestrator 进程内部，要么从 orchestrator 以 NATS / HTTP 方式访问。

orchestrator**除了它的 NATS + DB 连接之外是无状态的** —— 重启它不会影响正在运行的 Agent 容器（持久化的 JetStream 消费者会在重连时重放遗漏的消息；遗留的孤儿容器会在下次启动时按名称前缀 `rolemesh-` 被清理）。

### Agent 容器（Claude SDK / Pi）

每一轮 Coworker 对话都在一个短生命周期的 Docker 容器中运行。两个可互换的后端共享同一个镜像：

- **Claude SDK 后端** (`src/agent_runner/claude_backend.py`) —— 封装 Anthropic 官方的 `claude-agent-sdk`。一等支持 Claude Code 工作流、MCP、skill、subagent、OAuth Max 订阅令牌。
- **Pi 后端** (`src/agent_runner/pi_backend.py`) —— 封装 in-tree 的 Pi 运行时 (`src/pi/`)。多供应商（Anthropic、OpenAI、Gemini、Bedrock）；更干净的流式事件；fork 友好的会话模型。

后端选择是按 Coworker（`coworkers.agent_backend` 列）或按进程（`ROLEMESH_AGENT_BACKEND` 环境变量）的。orchestrator 和通道网关并不知道 —— 也不需要知道 —— 任何一个给定容器内部到底是哪个后端。

→ `docs/agent-executor-and-container-runtime.md`、`docs/switchable-agent-backend.md`、`docs/backend-stop-contract.md`

### NATS 总线

所有 orchestrator ↔ Agent 之间的 IPC，加上 通道 ↔ orchestrator 之间的 IPC，再加上若干内部 RPC，都搭载在同一个 NATS 服务器上（带 JetStream + KV）。agent_runner 与 orchestrator 之间没有直接连接 —— 它从 KV 桶读取初始配置，把结果 / 消息 / 任务操作发布到 JetStream subject 上，并通过同一条总线接收追加消息 / Stop / 关闭信号。

NATS 这个选择替换了最初 NanoClaw 的 IPC 方案（stdin 管道 + stdout 标记 + 文件轮询）—— 见下方"为什么这样选择"。

→ `docs/nats-ipc-architecture.md`

### 通道网关（Telegram、Slack、WebUI）

`ChannelGateway` 协议抽象了一个聊天平台如何投递用户消息以及接收 Agent 回复。Telegram 和 Slack 网关在 orchestrator 进程内部运行（事件驱动的监听器）。WebUI 是一个独立的 FastAPI 进程，通过 `web-ipc` NATS 命名空间与 orchestrator 通信 —— 这样把 HTTP 相关的关注点从 orchestrator 中移出，并允许 WebUI 独立扩展。

→ `docs/webui-architecture.md`

### MCP 工具（in-process + 外部）

每个 Agent 都有两类工具：

- **In-process MCP 工具** —— `rolemesh` MCP 服务器，对外暴露 `send_message`、`schedule_task`、`pause_task`、`list_tasks` 等工具。这些是 Agent 容器内部的直接 Python 函数调用，回到 orchestrator 的线路格式是 NATS。
- **外部 MCP 服务器** —— 由运维方配置的 MCP 端点（CRM、ERP、内部 API）。Agent 容器永远见不到 auth token：它通过本地的凭据代理通信，凭据代理在 HTTP 层使用宿主机上的 token 保险库重写 `Authorization` 头。JWT 在宿主侧通过 OIDC 风格的流程刷新。

→ `docs/external-mcp-architecture.md`

### hook 系统

统一的 `HookHandler` 协议把 Claude SDK 的 hook (`PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`PreCompact`、`Stop`) 与 Pi 的扩展事件 (`tool_call`、`tool_result`、`session_before_compact`) 桥接在一起。审计、DLP、对话归档、审批和可观测性等处理器都只针对这套统一协议写一次 —— 不论 Coworker 实际使用哪个后端，它们都会触发。

→ `docs/hooks-architecture.md`

### 审批模块

针对高风险 MCP 工具调用的、策略驱动的 human-in-the-loop 闸门。容器侧的 hook 拦截匹配到策略的工具调用、挂起它，并等待由 WebUI 的 REST decide 端点或自动审批者发布的 `approval.decided.{id}` 事件。其设计目标是：当部署中**没有任何策略**时，与一个不带审批模块的构建版本是逐位等价的 —— 没人配置时零开销。

→ `docs/approval-architecture.md`

### 安全 (Safety) 框架

三阶段内容管道（`INPUT_PROMPT`、`PRE_TOOL_CALL`、`MODEL_OUTPUT`，再加 `POST_TOOL_RESULT` 和 `PRE_COMPACTION`），配合五种判定动作（`allow` / `block` / `redact` / `warn` / `require_approval`）。八个内置检查器被分为**廉价**的容器内集合（`pii.regex`、`domain_allowlist`、`secret_scanner`）和**昂贵**的 orchestrator RPC 集合（`presidio.pii`、`llm_guard.prompt_injection`、`llm_guard.jailbreak`、`llm_guard.toxicity`、`openai_moderation`）。昂贵集合通过 `[safety-ml]` 这个 extra 控制是否启用，所以仅启用廉价检查的部署可以保持轻量。

→ `docs/safety/safety-framework.md`

### 容器加固

每个 Agent 容器都带着 `CapDrop=ALL`、no-new-privileges、AppArmor `docker-default`、`ReadonlyRootfs=true` 加上 tmpfs 切分、用户命名空间重映射（部署期生效）、按容器的资源上限（内存 / CPU / PIDs / fd / swap）、env 白名单（orchestrator 从不转发任意环境变量）、Docker socket 绑定阻断，以及 OCI 运行时切换（`runc` / `runsc`）。

→ `docs/safety/container-hardening.md`

### 出向网关 (egress gateway)（网络层）

Agent 容器跑在一个 `Internal=true` 的 Docker 网桥上 —— 它们没有任何通往互联网的路由。一个双网卡的 `egress-gateway` 容器同时跨接 Agent 网桥和 egress 网桥；每一条出向流量（LLM API、外部 MCP、包下载……）都要经过它。该网关运行一个 HTTP CONNECT 正向代理（端口 3128）、一个权威 DNS 解析器（端口 53，按租户的白名单）和一个注入凭据的反向代理（端口 3001，真实的 API 令牌就放在这里）。

这是第三个独立的安全层：即使一个恶意 Agent 逃过了内容管道**并**逃出了容器，它仍然没有路径去 `curl example.com`。

→ `docs/egress/deployment.md`

### 认证 (AuthN + AuthZ)

认证 (AuthN) 被委托给一个可插拔的 `AuthProvider`（External JWT、Builtin、OIDC）。授权 (AuthZ) 则始终是 RoleMesh 自己的逻辑 —— `AgentPermissions`（4 个字段：`data_scope`、`task_schedule`、`task_manage_others`、`agent_delegate`）控制一个 Agent 能做什么；用户角色（owner / admin / member）控制人类能做什么。授权检查发生在四个拦截点（IPC handler、REST middleware、通道入站、容器派生）—— **除此之外没有其他位置**。

→ `docs/auth-architecture.md`

### skill (Skills)

按 Coworker 划分的能力文件夹：一个 `SKILL.md`（markdown + YAML frontmatter）加上可选的支撑文件（参考文档、示例、脚本）。它们存储在带 RLS 的 Postgres 中，在每次派生时按只读 bind mount 投影进容器，从不在租户之间共享。模型会基于 frontmatter 中的 `description` 自动调用一个 skill —— 没有斜杠命令，也不需要人这边做任何接线。后端感知的 frontmatter 让同一个 skill body 同时服务于 Claude SDK (`/home/agent/.claude/skills`) 和 Pi (`/home/agent/.pi/skills`)；只属于另一个后端的字段会在投影时被丢弃。

→ `docs/skills-architecture.md`

### 事件流 + Steering

WebUI 展示实时进度事件 (`container_starting`、`running`、`tool_use`)，这样用户在长轮对话期间不用对着一个静默的 spinner 干瞪眼。Stop 按钮通过 JetStream 发布一个 `agent.{job}.interrupt`，这个事件会终止当前轮但不杀容器，因此用户可以立刻用追加消息 (follow-up) 重定向 —— 一次约 30 秒的冷启动只在每次 Coworker 会话中付出一次，而不是每次 Stop 都付出一次。

→ `docs/event-stream-architecture.md`、`docs/steering-architecture.md`

### 评估框架

`rolemesh-eval` CLI（基于 Inspect AI）衡量在不同 `system_prompt` / `tools` / `skills` / `agent_backend` / `model` 配置下 Coworker 行为的变化。它复用生产级的 `ContainerAgentExecutor`，所以评估运行的是与处理真实流量完全相同的代码路径 —— 不存在一个会和生产飘移的并行 orchestrator。每次运行都用 sha256 对 Coworker 的完整配置进行快照，因此 `rolemesh-eval list` 可以将共享同一份配置的运行聚类在一起。

（暂时还没有独立的文档 —— 见 README 的 "Evaluation" 一节和 `src/rolemesh/evaluation/`。）

### 数据库 (Postgres + RLS)

Postgres 16，每张租户作用域的表上都启用了行级安全。两连接池架构：

- `rolemesh_app` —— `NOBYPASSRLS`，被所有业务逻辑查询使用。RLS 强制生效；通过 `SET LOCAL rolemesh.tenant_id` GUC 把每条查询限定在租户作用域内。
- `rolemesh_system` —— `BYPASSRLS`，仅被 schema 迁移、系统级清理以及确实需要跨租户读取的 Safety / 审批 RPC 路径所使用。调用是显式的（`tenant_conn` vs `admin_conn`），所以这种区别在每个调用点都是可见的。

Schema 位于 `src/rolemesh/db/schema.py`；按实体划分的 CRUD 被拆分到 `db/{tenant,user,coworker,chat,task,skill,approval,safety}.py` 中。

→ `docs/multi-tenant-architecture.md`

### 调度器 (Cron)

orchestrator 内部的 cron 风格任务调度器（基于 croniter）。触发时它派生一个 Agent 容器，方式与人类发消息时完全一样，但在初始化负载里把 `is_scheduled_task=true` 标志置位，因此 Agent 提示词会被包裹上 `[SCHEDULED TASK]` 的语境。任务存储在带 RLS 的 `scheduled_tasks` 表中 —— Agent 只能看到 / 管理它自己租户的任务（再被 `data_scope` 进一步过滤）。

---

## 为什么这样选择

六个承重决策塑造了其他一切：

### 1. 按 Coworker 的短生命周期容器（而不是长期运行的 worker）

单个 Coworker 同时处理许多并发轮次和许多用户。朴素的设计是每个 Coworker 配一个长期运行的 worker 进程；选定的设计是每一轮（或每次会话）开一个全新的容器。代价是冷启动延迟（约 3–10 秒）；好处是：
- 故障隔离自动达成 —— 一个被污染的会话无法泄漏到下一次。
- 容器加固是真实有效的 —— rootfs 是临时的，所以"任意写"型攻击无法持久化。
- 资源上限是硬上限 —— 内核会杀掉整个 cgroup，不需要进程内的限制器。
- 后端切换是原子的 —— 改一下 `coworkers.agent_backend`，下一次派生就拿起新的；不需要滚动重启长期运行的 worker。

3–10 秒的冷启动通过 Steering 设计被缓解了（Stop 中断当前轮，但容器在本会话中保留以接受追加消息）。

### 2. 用 NATS 做统一 IPC

最初的 NanoClaw 在同一个代码库里使用了三种 IPC 机制：stdin 管道（初始输入）、stdout 标记（`---NANOCLAW_OUTPUT_START---`）以及文件轮询（其他一切）。这三种机制把 orchestrator 和 Agent 都耦合到了同一台宿主机上 —— 它们没法扩展到 Kubernetes 部署。

NATS（带 JetStream + KV）用一个系统替换了这三种，并补充了：
- 跨主机调度（Agent 容器可以跑在另一个节点上）。
- 持久化消费者，所以 orchestrator 重启会重放遗漏的消息而不是丢弃它们。
- 干净的线路格式 —— 通过命名 subject 传递 JSON，用 NATS CLI 很容易检查。

同一个 NATS 服务器还承载 WebUI ↔ orchestrator (`web-ipc`)、审批信号 (`approval-ipc`) 以及若干内部 RPC（`egress.*`、`safety.*`、`orchestrator.agent.lifecycle`）。

→ `docs/nats-ipc-architecture.md`

### 3. 仅内部的 Agent 网络 + 双网卡出向网关

Container Hardening 上线之后，Agent 仍然拥有不受限制的出向流量 —— `curl evil.com` 仍然能跑通。修复方案是结构性的而不是基于过滤器的：Agent 网桥是 `Internal=true`（Docker 阻止任何直接的互联网路由），而一个双网卡的网关容器是唯一的出口路径。该网关强制实施按租户的 DNS 白名单，并对 LLM / MCP 流量做反向代理，凭据在宿主边界处注入。

这是第三个独立的安全层 —— 与容器加固（阻止沙箱逃逸）和安全框架（阻止恶意提示词 / 输出）相互正交。任意一层失效都可以；同时三层都失效才是威胁模型本身。

→ `docs/egress/deployment.md`、`docs/safety/toggle-experiments.md`

### 4. 两个可互换的 Agent 后端

锁定到一个 LLM 框架是不可接受的：供应商的定价、限速以及功能路线图都会变成单点故障。`AgentBackend` 协议把 SDK 抽象出来，让系统的其他部分（orchestrator、通道、NATS 协议、MCP 工具、审批闸门）都与后端无关。

两个后端在机制上不同但在可观察行为上相同 —— Claude SDK 使用抢占式取消（`Task.cancel()`），Pi 使用协作式取消（`asyncio.Event`）；**Stop 契约**（`docs/backend-stop-contract.md`）记录了任何后端必须交付的四个可观察行为，与它们在内部如何实现无关。

→ `docs/switchable-agent-backend.md`

### 5. 通过 Postgres RLS 的数据库级多租户

同一个 orchestrator 上的两个租户绝不能看到彼此的数据，即使某条查询有 bug。选定的原语是数据库角色级的 Postgres 行级安全 —— 而不是那种一旦忘了 `WHERE tenant_id=` 就会被绕过的应用层过滤。

双连接池设计（`rolemesh_app` `NOBYPASSRLS` + `rolemesh_system` `BYPASSRLS`）让信任边界变得显式：业务代码使用 `tenant_conn(...)`，它会路由到 app 池并 `SET LOCAL rolemesh.tenant_id`；系统代码使用 `admin_conn(...)`，它会路由到 system 池。一次代码评审能在每个调用点直接看出该查询位于这条边界的哪一侧。

→ `docs/multi-tenant-architecture.md`

### 6. 三个相互正交的安全层（纵深防御）

每一层在设计时都假设其他层已经失效：

| 层 | 阻止 | 若被突破，下一层会接住 |
|---|---|---|
| **内容管道** (`safety/safety-framework.md`) | 恶意提示词、输出中的 PII 泄漏、越狱 | 绕过内容管道的提示词注入仍然无法运行特权容器系统调用 |
| **容器加固** (`safety/container-hardening.md`) | 沙箱逃逸、宿主机文件系统访问、能力滥用 | 一个逃出容器的被入侵 Agent 仍然没有互联网路由 |
| **网络出向** (`egress/deployment.md`) | 数据外泄、C2 回连、通过 DNS 进行的凭据窃取 | 网关的按租户白名单 + 凭据代理意味着 token 永远不会到达 Agent 进程 |

此外还有**人类审批流** (`approval-architecture.md`) 作为正交的"判断"层 —— 用于这种情形：Agent 拥有所有合法的权限，但运维方希望让一个人在它执行之前看一眼这个具体动作。

→ `docs/safety/attack-simulation-matrix.md` 跟踪了针对这三层建模过的每一种攻击及其对应的测试。

---

## 项目演进

RoleMesh 是从 [NanoClaw](https://github.com/qwibitai/nanoclaw)（一个面向单用户的 TypeScript Claude 助手）演化而来。整个工作分为两个阶段：

### 阶段 1 —— NanoClaw 时期（步骤 1–6）

在 NanoClaw 代码库上原地完成的工作，最终把它推到了远超单用户起源的程度：

1. **TypeScript → Python 重写。** 一次干净的移植，保留了 NanoClaw 的对外形态，但搬到 Python 生态（`asyncio`、`aiodocker`、`asyncpg`）。
2. **基于文件的 IPC → 基于 NATS 的 IPC。** 用一条单一的 NATS 总线（KV + JetStream + 请求-应答）替换了 stdin 管道 + stdout 标记 + 文件轮询。见 `docs/nats-ipc-architecture.md`。
3. **Agent 执行器 + 容器运行时 抽象。** 把最初 340 行的 `run_container_agent()` 拆成两个独立的层：`ContainerRuntime`（如何启动一个容器）和 `AgentExecutor`（用它来做什么）。见 `docs/agent-executor-and-container-runtime.md`。
4. **SQLite → Postgres。** 抛弃单文件 DB，换上正经的并发访问、schema 迁移以及（后来加上的）行级安全。
5. **Slack 通道。** 通过统一的 `ChannelGateway` 协议在已有的 Telegram 通道之外加上 Slack。
6. **多租户化。** Tenant + Coworker + ChannelBinding + Conversation 实体模型。这次变更彻底打破了"单用户助手"的定位 —— 代码库自此成为一个平台。

### 阶段 2 —— RoleMesh fork（步骤 7+）

在步骤 6 之后，代码库从 NanoClaw fork 出来并改名为 RoleMesh（项目名 + 每一个代码标识符都改了）。后续工作发生在新仓库上：

7. **WebUI。** FastAPI + WebSocket + Lit 前端，作为独立进程运行，把 HTTP 相关的关注点从 orchestrator 中移出。见 `docs/webui-architecture.md`。
8. **AuthN + AuthZ。** 可插拔的认证 provider（External JWT / Builtin / OIDC）、四字段的 `AgentPermissions` 模型、OIDC PKCE 登录。见 `docs/auth-architecture.md`。
9. **外部 MCP 工具。** 凭据代理 + token 保险库 + token 刷新，这样 Agent 容器永远见不到真实的 auth token。见 `docs/external-mcp-architecture.md`。
10. **可切换的 Agent 后端。** 集成 Pi 后端 —— 与 Claude SDK 并列的第二个运行时，由 `coworkers.agent_backend` 控制。见 `docs/switchable-agent-backend.md`。
11. **hook。** 跨 Claude SDK 和 Pi 的统一 hook 系统。见 `docs/hooks-architecture.md`。
12. **事件流。** 实时进度事件推送给 WebUI。见 `docs/event-stream-architecture.md`。
13. **Steering。** Stop 按钮 + 运行中追加消息（真正的轮内 steering 暂不实现）。见 `docs/steering-architecture.md` 和 `docs/backend-stop-contract.md`。
14. **审批。** 针对高风险 MCP 调用、策略闸门驱动的 human-in-the-loop。见 `docs/approval-architecture.md`。
15. **安全栈。** 三层 —— 容器加固、内容安全框架、网络出向控制。见 `docs/safety/container-hardening.md`、`docs/safety/safety-framework.md`、`docs/egress/deployment.md`。
16. **RLS。** 在每张租户作用域的表上启用 Postgres 行级安全；双连接池架构。见 `docs/multi-tenant-architecture.md`。
17. **skill。** 按 Coworker 划分的 markdown skill 文件夹，按每次派生进行投影。见 `docs/skills-architecture.md`。
18. **评估。** 基于 Inspect AI 的 `rolemesh-eval` CLI；复用生产级的 `ContainerAgentExecutor`。
19. **可观测性。** OpenTelemetry tracer + 跨 NATS subject 的 W3C trace-context 传播（进行中）。

阅读较旧的代码或较旧的文档时这种切分很重要：阶段 2 之前的任何东西可能仍在谈论 NanoClaw，并且 IPC + 容器抽象设计 (`nats-ipc-architecture.md`、`agent-executor-and-container-runtime.md`) 描述的是改名之前阶段 1 的工作。

---

## 各模块文档

按主题分组。每篇文档都聚焦于*为什么* —— 考虑过的备选方案与所选取的取舍 —— 这样你可以在不重新审议原始决定的前提下扩展模块。

### 容器与 Agent 运行时

- [`agent-executor-and-container-runtime.md`](agent-executor-and-container-runtime.md) —— `ContainerRuntime` + `AgentExecutor` 双层切分
- [`switchable-agent-backend.md`](switchable-agent-backend.md) —— 按 Coworker 选择 Claude SDK / Pi
- [`backend-stop-contract.md`](backend-stop-contract.md) —— 任何后端在 Stop 时必须交付的可观察行为

### IPC

- [`nats-ipc-architecture.md`](nats-ipc-architecture.md) —— 六通道 NATS 协议；KV + JetStream + 请求-应答

### 多租户与身份

- [`multi-tenant-architecture.md`](multi-tenant-architecture.md) —— Tenant / Coworker / Conversation 实体模型、Postgres RLS
- [`auth-architecture.md`](auth-architecture.md) —— `AgentPermissions`、四个拦截点、三种部署模式

### 通道

- [`webui-architecture.md`](webui-architecture.md) —— FastAPI + Lit、独立进程的设计

### Agent 行为

- [`hooks-architecture.md`](hooks-architecture.md) —— 跨 Claude SDK 与 Pi 的统一 hook 系统
- [`event-stream-architecture.md`](event-stream-architecture.md) —— 推送给 WebUI 的实时进度事件
- [`steering-architecture.md`](steering-architecture.md) —— Stop 按钮 + 运行中追加消息
- [`skills-architecture.md`](skills-architecture.md) —— 按 Coworker 的 skill 文件夹、按每次派生投影

### 工具与 human-in-the-loop

- [`external-mcp-architecture.md`](external-mcp-architecture.md) —— 面向外部 MCP 服务器的凭据代理 + token 保险库
- [`approval-architecture.md`](approval-architecture.md) —— 策略闸门审批流

### 安全

- [`safety/safety-framework.md`](safety/safety-framework.md) —— 三阶段内容管道、八个检查器、廉价 / 昂贵切分
- [`safety/container-hardening.md`](safety/container-hardening.md) —— CapDrop / 只读 rootfs / userns / runsc / 等
- [`safety/attack-simulation-matrix.md`](safety/attack-simulation-matrix.md) —— 建模过的攻击 vs. 防御层
- [`safety/toggle-experiments.md`](safety/toggle-experiments.md) —— 三个安全层的实证 A/B 比较
- [`egress/deployment.md`](egress/deployment.md) —— `Internal=true` Agent 网桥 + 双网卡网关运维指南

每篇文档的中文译文都以 `*-cn.md` 的形式与之并列存放。
