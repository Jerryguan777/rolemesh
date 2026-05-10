# 多租户与多 Coworker 架构

本文档阐述 RoleMesh 多租户、多 Coworker 架构的设计——决策背后的推理、所考量的权衡，以及最终的设计方案。

> **项目脉络。** RoleMesh 起源于 [NanoClaw](https://github.com/qwibitai/nanoclaw) 的 Python 重写版本，NanoClaw 是一个单用户的 TypeScript Claude 助手。多租户化是分水岭式的变更——是 NanoClaw 时代重写的最后一步，此后代码库被 fork 并更名为 RoleMesh。下文的历史性章节讨论的是被替换掉的原始单用户形态；当前的实现，包括数据库级别的 RLS，则属于 RoleMesh 时代。

---

## 背景

NanoClaw 起初是一个单用户的个人 AI 助手：一个人、一个 Agent、一个聊天群组。架构很简单——一个 `RegisteredGroup` 与一个聊天上下文 1:1 映射，Agent 运行在容器中，模块级的 Python 全局变量追踪一切。

随着项目向通用的 **AI Coworker 平台**演进，我们需要支持：

- 多个**组织**（租户）共享同一基础设施
- 每个租户有多个 **AI Coworker（Coworker）**（运营 AI、客服 AI 等）
- 同一类型的多个 Coworker（每条产品线一个、每个区域一个等）
- 每个实例同时与**多个聊天通道**中的用户交互
- 一个组织内的**多个人类用户**，每人都有合适的访问权限

本文档描述了我们如何从单用户设计走向多租户架构。

---

## 核心概念

### 实体层级

```
Tenant (organization)
│
├── Coworker (AI agent)
│   ├── Carries its own config: system prompt, tools, skills, LLM backend
│   ├── Has its own workspace (files, logs)
│   ├── Identified by independent bot identity per channel
│   └── Can operate in multiple chat groups simultaneously
│
├── Conversation (a Coworker's context in a specific chat group)
│   ├── Has independent session/memory
│   └── requires_trigger flag (on for group chats, off for DMs / Web UI)
│
└── User (human team member)
    └── Can interact with multiple Coworkers
```

### 为何选择这些具体实体？

**Tenant** 直截了当——组织级隔离边界。

**Coworker** 是中心实体——一个 AI Agent，拥有自己的身份、配置（system prompt、tools、skills、LLM 后端）、工作空间和并发限制。我们曾考虑将其拆分为"角色模板 + Coworker 实例"模型（Role 定义共享配置，Coworker 继承之），但发现对当前阶段而言属于过度设计：没有代码使用模板复用、每次创建 Coworker 都需要先存在一个 Role、额外的表/JOIN/CRUD 增加了复杂度却没有任何收益。如果以后需要模板复用，添加一个带外键的 `roles` 表是一个直观的扩展。

**Conversation** 来自一个具体的洞察：当同一个 Coworker 在多个 Telegram 群组中工作时，**文件工作空间应当共享**（同一份产品数据、同一份代码库），但**会话记忆应当独立**（不同群组讨论不同主题）。

---

## 设计决策

### 1. Session 作用域：按 Conversation，而非按 Coworker

**决策**：每个 Conversation（Coworker + 聊天群组的组合）拥有自己的 session。

**考量过的替代方案**：

| 方案 | 行为 | 问题 |
|----------|----------|---------|
| 按 Coworker 划分 session | 所有群组共享记忆 | A 群的讨论会泄漏到 B 群 |
| 按 Conversation 划分 session | 每个群组拥有独立记忆 | 隔离正确 ✓ |
| 按 User 划分 session | 每个人类拥有自己的线程 | 破坏群组协作 |

关键洞察：一个 Coworker 就像一个在多个 Slack 频道中工作的人类员工。他们分别记住每个频道里说过的内容，但不论身处哪个频道都访问相同的文件和数据库。

它与决策 6（工作空间隔离）配对，构成完整的共享模型。两者共同回答：**"当同一个 Coworker 在多个聊天群组中工作时，什么是共享的、什么是隔离的？"**

| 资源 | 作用域 | 原因 |
|----------|-------|-----|
| **工作空间文件**（代码、数据、报告） | 按 Coworker（共享） | 不论请求来自哪个聊天群组，同一个 Coworker 管理同一条产品线 |
| **Session/记忆**（对话历史） | 按 Conversation（隔离） | 不同群组讨论不同话题，混在一起会让 Agent 困惑 |
| **日志** | 按 Coworker（共享） | 跨所有 Conversation 的运维可见性 |
| **共享知识**（SOP、手册） | 按 Tenant（只读） | 同一租户内的所有 Coworker 访问同一份参考资料 |

这是一种刻意的非对称。常见的错误是把所有东西都做成按 Conversation（完全隔离）或按 Coworker（完全共享）。这种拆分反映了人类员工实际的工作方式：他们分别记住对话，但无论与谁交谈，桌面和文件都是同一份。

### 2. Bot 身份：按 Coworker，而非按 Tenant

**决策**：每个 Coworker 在每种通道类型上拥有自己的 bot 身份（例如它自己的 Telegram bot）。

**为什么不是每个租户一个 bot？** 如果一个租户拥有 3 个 Coworker（运营 AI、客服 AI、物流 AI）共享一个 Telegram bot，群组里的用户会看到一个 bot，需要使用 `@ops help` 与 `@cs help` 之类的关键字来路由消息。这样：

- 让用户困惑（命令到底是哪个？）
- 脆弱（拼写错误就让路由失败）
- 缺乏视觉身份（每个 Coworker 没有独立的头像/名字）

采用按 Coworker 一个 bot 后，用户在群组里看到 `@acme_ops_bot` 和 `@acme_cs_bot` 是两个独立实体。他们 `@mention` 想交流的那一个，就像提到一位人类同事一样。在 Telegram 上，创建 bot 几乎免费（每个 bot 一条 BotFather 命令）。

**权衡**：要管理更多 bot。但这是一个配置问题，不是架构问题——Channel Gateway 模式可以干净地处理它。

**重要警告——token 去重**：尽管*概念*模型是"每个 Coworker 一个 bot"，在迁移期间多个 Coworker 可能共享同一个 bot token。Gateway 必须按 token 去重：**一个 token = 一个 polling 连接**，消息再扇出到所有关联的绑定。为同一个 token 创建多个 polling 实例会导致平台 API 冲突（例如 Telegram 的 `Conflict: terminated by other getUpdates request`）。

### 3. Channel Gateway 模式

**决策**：每种通道类型一个 Gateway，由其管理多个 bot 实例。

当 N 个 Coworker × M 种通道类型时，单独的 bot 连接无法扩展。Gateway 是某一种通道类型的管理对象——它处理 token 去重、连接生命周期、统一消息回调、共享错误处理与限速。单个 bot 变得轻量（只有 token + 连接），Gateway 承担复杂性。WebUI gateway 的具体实现细节见 [`webui-architecture.md`](webui-architecture.md)。

### 4. OrchestratorState：以结构化状态取代全局变量

**决策**：用结构化的 `OrchestratorState` 类替换模块级全局变量。

**之前**（单租户）：

```python
_sessions: dict[str, str] = {}
_registered_groups: dict[str, RegisteredGroup] = {}
_last_agent_timestamp: dict[str, str] = {}
_queue: GroupQueue = GroupQueue()
_channels: list[Channel] = []
```

这些全局变量在单租户下能工作，因为每个 key 都是唯一的。在多租户下，类似 `"main"` 这种 `group_folder` 可能在每个租户里都存在。扁平的 dict 就崩了。

**之后**（多租户）：

```python
class OrchestratorState:
    tenants: dict[str, Tenant]
    coworkers: dict[str, CoworkerState]     # coworker_id → state

@dataclass
class CoworkerState:
    config: CoworkerConfig
    conversations: dict[str, ConversationState]
```

一切都以 ID 为 key，按 tenant 和 coworker 索引。无歧义，无冲突。

### 5. Coworker 配置：没有模板层

**决策**：每个 Coworker 直接携带其完整配置。

```python
@dataclass
class CoworkerConfig:
    name: str                 # display name (trigger derived from this)
    folder: str               # workspace path
    system_prompt: str | None
    tools: list[McpServerConfig]   # external MCP server bindings
    agent_backend: str        # "claude" or "pi"
    max_concurrent: int
    container_config: dict | None
    agent_role: str           # "super_agent" | "agent"
    permissions: AgentPermissions  # 4 fields (see auth-architecture.md)
```

所有字段都位于 `coworkers` 表。没有 JOIN，没有合并，没有模板层。如果多个 Coworker 需要相同配置，则各自独立配置——在当前规模下重复是可以接受的，并且比模板继承系统更易于推理。

**为什么不是 Role 模板层？** 我们最初设计过一个（带从 `coworkers` 出发的外键的 `roles` 表），后来移除了它，因为：没有代码使用模板复用能力、每次创建 Coworker 都需要先存在一个 Role、额外的表带来了复杂度却无现实收益。如果以后模板复用成为必需（例如管理 UI 中"用同一模板创建 5 个运营 AI"），把它加回来很直接。

> 两块按 Coworker 配置的内容长出了自己的子系统，**不**作为 `coworkers` 上的列：
> - **Skills** — 多文件能力文件夹，存于专用的 `skills` / `skill_files` 表（旧的 `coworkers.skills` JSONB 列已被删除）。见 [`skills-architecture.md`](skills-architecture.md)。
> - **Permissions** — 以 JSONB 列存储，但模型与 user 共享，详见 [`auth-architecture.md`](auth-architecture.md)。

### 6. 工作空间隔离模型

**决策**：在三个层次上实现文件系统隔离——按 tenant、按 coworker、按 conversation：

- **Tenant 边界** —— `data/tenants/{tenant_id}/` 永不交叉；某个租户树下的任何内容都不会挂载到另一个租户的容器中。
- **Coworker 工作空间** —— 可读写，跨该 Coworker 的所有 Conversation 共享。
- **Conversation session** —— 可读写，作用域限定于某一个聊天群组的记忆。
- **租户共享空间** —— 只读知识库（SOP、手册、参考数据），租户内的每个 Coworker 都可访问。

实际的挂载路径与 bind-mount 机制位于 [`agent-executor-and-container-runtime.md`](agent-executor-and-container-runtime.md)——那是把 `coworker_id + conversation_id` 转换为 `ContainerSpec` 的层级。从多租户视角出发，唯一重要的就是这种非对称：工作空间按 Coworker（共享），session 按 Conversation（隔离），共享知识按 tenant（只读）。

**为什么不按 Conversation 划分工作空间？** 如果同一个 Coworker 同时在 Telegram 群组和 Slack 频道中管理广告投放，底层的广告数据和脚本是相同的。按 Conversation 复制工作空间会导致漂移和混乱。

**为什么共享空间是只读？** 共享知识库是经过策划的内容，Coworker 应当读取但不应修改。写权限会在 Coworker 之间造成冲突。

---

## 数据库级隔离：RLS + 双连接池

应用级 `WHERE tenant_id = …` 过滤是最初的隔离机制。它不够——一次遗漏的 `WHERE` 子句就成了跨租户的数据泄漏。当前的设计在**数据库角色级别**强制执行租户隔离，从而把"默认错误"的姿态阻断而非允许。

### 两个 Postgres 角色，两个连接池

```
rolemesh_app      LOGIN NOBYPASSRLS    ← all business code
rolemesh_system   LOGIN BYPASSRLS      ← migrations, system maintenance, cross-tenant resolvers
```

每个按租户作用域的表都启用了 Row-Level Security（行级安全），并绑定到一个按事务设置的 Postgres GUC 变量上：

```sql
CREATE POLICY p_self_tenant ON coworkers
    USING (tenant_id::text = current_setting('rolemesh.tenant_id', true));
```

orchestrator 在 `src/rolemesh/db/_pool.py` 中提供两个连接 helper：

```python
async with tenant_conn(tenant_id) as conn:
    # rolemesh_app pool; SET LOCAL rolemesh.tenant_id = tenant_id;
    # RLS policies bind — every query implicitly scoped to this tenant.
    rows = await conn.fetch("SELECT * FROM coworkers")  # only this tenant's rows

async with admin_conn() as conn:
    # rolemesh_system pool; BYPASSRLS.
    # Cross-tenant work is intentional and visible at the call site.
    rows = await conn.fetch("SELECT * FROM coworkers")  # every tenant's rows
```

代码评审在每个调用点都能看到一个查询位于信任边界的哪一侧——`tenant_conn(...)` 受 RLS 强制；`admin_conn()` 是显式的逃生门。

### 为何选择 RLS 而非应用级过滤

| 方案 | 失败模式 |
|---|---|
| 应用级 `WHERE tenant_id=...` | 一次遗漏的 `WHERE` = 跨租户泄漏。Bug 面 = 代码库中的每一条查询。 |
| 每个租户一个 Postgres 数据库 | 运维负担重（部署、迁移、监控 × N 个租户）。在没有分片感知工具的情况下无法做跨租户分析。 |
| **Postgres RLS + 双连接池** ✓ | Bug 面 = 少数几个 `admin_conn()` 调用点。"这条查询是否需要跨租户？"的代码评审可以机械化执行。 |

### 推迟到未来文档的内容

本节涵盖的是*设计意图*与信任边界契约。完整的 RLS 策略目录（哪些表有哪些策略、函数分类 A/B/C、像 `skill_files` 通过 `skills` 传递键值这样的特殊情况）以行内方式存在于 `src/rolemesh/db/schema.py` 中，并被测试套件（`tests/db/test_rls_enforcement.py`、`test_admin_path_isolation.py`、`test_cross_tenant_isolation.py`）引用。

---

## 身份与权限

本文档讨论的是**行如何按租户作用域化**。正交的问题——*哪个 user 可以使用哪个 agent*、*某个 agent 被允许做什么*、*JWT/OIDC 身份如何变成租户上下文*——在 [`auth-architecture.md`](auth-architecture.md) 中讨论。摘要：

- **用户角色**（`owner` / `admin` / `member`）控制平台自身的管理动作。
- **Agent 权限**是一个 4 字段模型（`data_scope`、`task_schedule`、`task_manage_others`、`agent_delegate`），由每个 Coworker 携带。它们在 IPC 强制时控制 agent 能做什么。这里的 IPC 契约——payload 携带 `tenantId + coworkerId`，由 orchestrator 查找权威权限——记录在 [`nats-ipc-architecture.md`](nats-ipc-architecture.md)。
- **租户上下文传播**——每条业务查询都通过 `tenant_conn(tenant_id)`；GUC 绑定 RLS，任何需要跨租户的查询必须改用 `admin_conn()`。

---

## 数据模型

```
                    ┌──────────┐
                    │  Tenant  │
                    └────┬─────┘
           ┌─────────────┼─────────────┐
           ▼             ▼             ▼
       ┌───────┐   ┌───────────┐  ┌───────┐
       │ User  │   │ Coworker  │  │Shared │
       └───────┘   │(config +  │  │Space  │
                   │ workspace)│  └───────┘
                   └─────┬─────┘
                         │
               ┌─────────┼──────────┐
               ▼                    ▼
       ┌───────────────┐   ┌──────────────┐
       │ChannelBinding │   │ScheduledTask │
       │(bot identity) │   └──────────────┘
       └───────┬───────┘
               │
               ▼
       ┌──────────────┐
       │ Conversation │─── session (independent memory)
       └──────┬───────┘
              │
              ▼
       ┌──────────────┐
       │   Messages   │
       └──────────────┘
```

### 关键关系

- **Tenant → Coworker**：一对多。每个 Coworker 携带其完整配置（prompt、tools、backend）。
- **Coworker → ChannelBinding**：一对多（每种通道类型一个）。每个绑定有 bot 凭据。
- **ChannelBinding → Conversation**：一对多。一个 bot 在多个聊天群组中。
- **Conversation → Session**：一对一。独立的对话记忆。
- **Conversation → Messages**：一对多。
- **Coworker → Workspace**：一对一。跨所有 Conversation 共享的文件系统。

### 数据库表

下面的 schema 仅包含目的明确的列——没有为未实现功能预留的占位字段。每个按租户作用域的表都启用了 RLS（见上文"数据库级隔离"）。

| 表 | 关键列 | 用途 |
|-------|-------------|---------|
| `tenants` | `id`、`name`、`slug`、`max_concurrent_containers`、`last_message_cursor` | 组织边界与限制 |
| `users` | `id`、`tenant_id`、`name`、`role`、`email`、`external_sub` | 人类用户 + 认证提供者映射 |
| `coworkers` | `id`、`tenant_id`、`name`、`folder`、`agent_backend`、`system_prompt`、`tools`（JSONB）、`agent_role`、`permissions`（JSONB）、`container_config`、`max_concurrent` | 带完整配置的 AI Agent |
| `channel_bindings` | `id`、`tenant_id`、`coworker_id`、`channel_type`、`credentials`、`bot_display_name` | 按 Coworker 的 bot 身份 |
| `conversations` | `id`、`tenant_id`、`coworker_id`、`channel_binding_id`、`channel_chat_id`、`requires_trigger`、`last_agent_invocation`、`user_id` | 每个聊天的上下文，带独立 session |
| `sessions` | `conversation_id`（PK）、`tenant_id`、`coworker_id`、`session_id` | 每个 Conversation 的 Claude SDK / Pi session 映射 |
| `messages` | `id`、`tenant_id`、`conversation_id`、`sender`、`content`、`timestamp`、`input_tokens`、`output_tokens`、`cost_usd`、`model_id` | 聊天消息历史 + 每轮使用量 |
| `scheduled_tasks` | `id`、`tenant_id`、`coworker_id`、`conversation_id`、`prompt`、`schedule_type`、`schedule_value`、`next_run` | Cron / 间隔 / 一次性任务 |
| `task_run_logs` | `id`、`task_id`、`run_at`、`duration_ms`、`status`、`result`、`error` | 任务执行历史 |
| `skills` / `skill_files` | （独立子系统） | 按 Coworker 的 skill 文件夹——见 [`skills-architecture.md`](skills-architecture.md) |
| `approval_policies` / `approval_requests` / `approval_audit_log` | （独立子系统） | 审批模块——见 [`approval-architecture.md`](approval-architecture.md) |
| `safety_rules` / `safety_decisions` / `safety_rule_audit` | （独立子系统） | 安全框架——见 [`safety/safety-framework.md`](safety/safety-framework.md) |

---

## 消息流

Telegram 群组中的一条用户消息按以下路径变成一轮 turn：

1. **TelegramGateway** 通过消息中提及的那个 bot 接收（一个 token = 一个 polling 实例，扇出到所有关联的绑定）。
2. **路由**：`binding_id → coworker_id + tenant_id`；`(binding_id, channel_chat_id) → conversation_id`。内部路由 key 是 `conversation_id`（UUID），而非 `channel_chat_id`。
3. **入站过滤**（多 bot 群组）：如果设置了 `requires_trigger` 且消息不匹配 `@coworker.name`，在存储前就丢弃。
4. **存储**到 `messages`（按 RLS 绑定到租户）并入队一轮 turn 待处理。
5. **并发检查**（全局 + 按租户 + 按 Coworker——见 [`agent-executor-and-container-runtime.md`](agent-executor-and-container-runtime.md)）。
6. **创建容器**，挂载正确的工作空间、session 目录、共享空间；通过 `AgentInitData` 传入 Coworker 的权限。
7. **Agent 执行**，结果通过 NATS 流回——见 [`nats-ipc-architecture.md`](nats-ipc-architecture.md)。
8. **回复**通过来源 Coworker 的绑定路由（按 `coworker_id`，而非通过扫描所有绑定查找匹配的 `chat_id`）。

### 关键路由规则（从实现中习得）

1. **`conversation_id` 是内部路由 key**，不是 `channel_chat_id`。在 Telegram 私聊中，同一个用户与 3 个不同 bot 对话产生的 `chat_id` 相同（用户 ID）。只有 `conversation_id`（UUID）才是全局唯一的。
2. **存储前入站过滤**：在多 bot 群组中，每个 bot 都会收到所有消息。每个 bot 必须自行过滤——只存储匹配自己触发模式的消息。否则 Coworker 会累积无关消息，并被本属于其他 Coworker 的触发激活。
3. **事件驱动，而非轮询驱动**：入站消息在存储后直接入队一轮 turn。系统**不**依赖 polling 游标——按租户的游标在多个 app 略有时差地收到同一条消息时会引发竞态。
4. **IPC 回复按 Coworker 路由**：当 agent 通过 `send_message` MCP 工具发送消息时，回复经由源 Coworker 自己的绑定路由，而非通过扫描所有绑定查找匹配 `chat_id`。这避免了在私聊场景中回复被错误地通过另一个 bot 发出。
5. **输出通道之间的去重**：agent 拥有两条输出路径——`send_message`（即时，通过 IPC）与结果流（最终，通过 NATS）。orchestrator 追踪通过 IPC 发送的文本，并在结果流中跳过重复。

---

## 它如何从单租户演进而来

来自原始 NanoClaw 概念的映射。整张表都属于**阶段 1**（NanoClaw 时代的重写，在项目 fork 为 RoleMesh 之前）：

| 原始（NanoClaw 单租户） | 多租户（当前） | 变化 |
|---|---|---|
| `RegisteredGroup` | `Coworker` + `Conversation` | 拆分："谁"与"在哪"分离 |
| `group.folder` | `coworker.folder` | 路径：`groups/x/` → `tenants/{tid}/coworkers/x/` |
| `group.trigger` | 由 `coworker.name` 派生 | 触发文本 = Coworker 名称；`conversation.requires_trigger` 控制开关 |
| `chatJid` | `conversation.channel_chat_id` | 1:N 而非 1:1 |
| `session`（按群组） | `session`（按 Conversation） | 作用域收窄 |
| `ASSISTANT_NAME` | `coworker.name` | 全局常量 → 按实体配置 |
| `TRIGGER_PATTERN` | 来自 `coworker.name` | 由 Coworker 身份派生 |
| `GroupQueue` | 三级调度器 | 加入了租户与 Coworker 的限制 |
| `Channel` 单例 | `ChannelGateway` | 每种类型一个管理器，多个 bot |
| 模块全局 | `OrchestratorState` | 结构化、按 ID 索引 |
| `is_main`（bool） | `agent_role` + `AgentPermissions` | 1 个布尔 → 4 个正交字段（在阶段 2 中加入） |
| 应用级 `WHERE tenant_id` | Postgres RLS + 双连接池 | 信任边界移入数据库（在阶段 2 中加入） |

迁移通过 `DEFAULT_TENANT = "default"` 默认值以及一个用于在 IPC 线上转换历史 `is_main` payload 的转换器，保持向后兼容。

---

## 这套架构**不**做什么

这些是被显式推迟到未来工作的内容：

- **A2A 协作** —— Coworker 之间相互委派任务。`agent_delegate` 权限字段是占位符；运行时强制和委派 IPC 都尚未实现。
- **跨租户市场 / 共享 Coworker** —— 今天每个 Coworker 都恰属于一个租户。不存在"发布我的 Coworker 供其他租户订阅"这一概念。
- **按 Conversation 的权限覆写** —— 权限位于 Coworker 上，而非 Conversation。某个 Coworker 不能被设为"在这个群里只读，在另一个群里完全访问"。
- **超出容器并发的租户资源配额** —— 每个租户有一个 `max_concurrent_containers`，但没有 token / 花费 / API 配额；成本遥测存在于 `messages.cost_usd`，但仅用于报告。

相应地，数据库 schema **不**包含未实现功能的列——字段在其功能被构建时才添加，而不是作为占位符。

---

## 运维考量

### Schema 迁移

跨大版本升级时，迁移路径以行内方式存在于 `src/rolemesh/db/schema.py`（`_create_schema` 是幂等的，能就地处理已存在的表）。该文件目前编码的显著迁移：

- 单租户 → 多租户：检测旧表（例如带 `chat_jid` 列的 `messages`），读取数据，删除旧表，创建新表，重新插入。
- `is_admin` 布尔 → `agent_role` + `permissions` JSONB。
- 将 `agent_backend = 'claude-code'` 这种旧值就地转换为规范的 `'claude'`。
- 在 `oidc_user_tokens` 上回填 `tenant_id`，让 RLS 在 D10 之后能够生效。

orchestrator 在启动时运行 `_create_schema`；滚动部署可行，因为每次变换都是幂等的。

---

## 相关文档

- [`auth-architecture.md`](auth-architecture.md) —— `AgentPermissions`、用户角色、OIDC 流程、四个授权拦截点
- [`agent-executor-and-container-runtime.md`](agent-executor-and-container-runtime.md) —— 三级并发控制（`GroupQueue`）、容器挂载、运行时选择
- [`nats-ipc-architecture.md`](nats-ipc-architecture.md) —— orchestrator 与 Agent 容器之间的 IPC 层
- [`skills-architecture.md`](skills-architecture.md) —— `skills` / `skill_files` 表（与 `coworkers` 分离）
- [`approval-architecture.md`](approval-architecture.md) —— 审批策略与审计日志
- [`safety/safety-framework.md`](safety/safety-framework.md) —— 安全规则与决策
