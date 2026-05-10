# 基于 NATS 的 IPC 架构

本文档描述 RoleMesh 的 orchestrator（Orchestrator）和容器内的 Agent 如何通过 NATS 进行通信。内容涵盖原始方案存在的问题、为何选择 NATS、6 通道协议的设计，以及每个通道使用的 NATS 原语。

> **项目谱系。** RoleMesh 起源于对 [NanoClaw](https://github.com/qwibitai/nanoclaw) 的 Python 重写；从基于文件的 IPC 迁移到 NATS 就是在那次重写中完成的，因此下文的历史章节谈论的是被替换掉的最初 NanoClaw 方案。后期添加的 subject（`interrupt`、`safety_events`，以及 `web-ipc` / `approval-ipc` 流）则属于 RoleMesh 时期在同一条 NATS 总线上的扩展。

## 背景：为什么不用文件或 stdin/stdout？

最初的 NanoClaw 在 Orchestrator 与 Agent 之间使用了三种不同的通信机制：

1. **stdin** —— Orchestrator 把初始 JSON 通过管道送入容器的标准输入
2. **stdout 标记** —— Agent 在标准输出上把结果写在 `---NANOCLAW_OUTPUT_START---` / `---NANOCLAW_OUTPUT_END---` 这两个标记之间
3. **基于文件的 IPC** —— Agent 把 JSON 文件写入共享目录，Orchestrator 每秒轮询一次

这种方式对单用户工具是够用的，但存在根本性的问题：

| 机制 | 问题 |
|-----------|---------|
| stdin JSON | Kubernetes Job 不支持 stdin 管道传输。Agent 必须能够自行启动并拉取自己的输入。 |
| stdout 标记 | 解析过于脆弱。任何意料之外的输出（库的警告、调试打印）都会破坏标记检测。 |
| 文件轮询 | 1 秒的延迟下限。每秒对每个组目录执行 `readdir` 难以扩展。需要 `.tmp` + `rename` 的 workaround 来规避文件系统竞态。在 Kubernetes 中需要共享卷（ReadWriteMany PVC），而它既慢又不可靠。 |

这三种机制还有一个更深层的共同问题：它们**把 Orchestrator 和 Agent 耦合到同一台主机上**。Agent 容器必须与 Orchestrator 处于同一台机器才能共享 stdin/stdout 管道与文件系统挂载。这阻碍了在 Kubernetes 集群中跨节点调度 Agent 容器。

## 为什么选 NATS？

我们评估了三个候选方案：

| 选项 | 优点 | 缺点 |
|--------|------|------|
| **Redis Streams** | 成熟、广泛部署、支持 consumer group | 需要再运维一个有状态服务；同一系统内没有原生的带 TTL 的 KV |
| **gRPC** | 强类型、双向流 | 需要生成 protobuf stub；对简单 JSON 消息过于笨重；Agent 容器需要运行 gRPC 服务 |
| **NATS** | 单一二进制、零配置；JetStream 一并提供持久化、KV Store 与请求-应答；Kubernetes 原生；内存占用 <10MB | 知名度不如 Redis |

NATS 胜出的原因是它能**用一套系统替代上述三种原始机制**，而且恰好提供我们所需的原语：

- **KV Store** —— 用于初始输入和快照（点读语义）
- **JetStream** —— 用于流式结果、消息和任务（有序、持久、可 ack）
- **请求-应答** —— 用于关闭信号（确认送达）

一个加上 `--jetstream` 标志的 NATS server 二进制就涵盖一切。本地开发只需 `docker run nats:latest --jetstream`。无需配置文件，也无需搭建集群。

## 6 通道协议

Orchestrator 与 Agent 通过六条逻辑通道通信。每条通道有清晰的方向、用途以及对应的 NATS 原语：

```
 Orchestrator                                       Agent (container)
 ────────────                                       ──────────────────
                  Channel 1: Initial Input
           ──── KV Store (agent-init) ────→
                  Orch writes before start,
                  Agent reads on startup

                  Channel 2: Streaming Results
           ←─── JetStream (results) ──────
                  Agent publishes result blocks,
                  Orch subscribes

                  Channel 3: Control + Follow-ups
           ──── JetStream (input) ─────────────→   follow-up messages
           ──── JetStream (interrupt) ─────────→   stop signal (current turn)
           ──── Request-Reply (shutdown) ──────→   shutdown signal (close container)

                  Channel 4: Agent Messages
           ←─── JetStream (messages) ─────
                  Agent sends messages to users

                  Channel 5: Task Operations
           ←─── JetStream (tasks) ────────
                  Agent creates/manages tasks

                  Channel 6: Snapshots
           ──── KV Store (snapshots) ─────→
                  Orch writes before start,
                  Agent reads via MCP tools
```

通道 3 承载三种不同的子信号（追加消息、停止、关停）。除了这六条通道之外，RoleMesh 还在同一个 `agent-ipc` 流上添加了一个 `safety_events` 审计 subject，并新增了几个非 agent 的 NATS 命名空间（`web.>`、`approval.*`、`egress.*`），它们各自在别处文档化 —— 完整清单见下文 "Subject Naming Convention"。

### 通道 1：初始输入

**方向**：Orchestrator → Agent
**NATS 原语**：KV Store，bucket 为 `agent-init`
**Key**：`{job_id}`

在启动容器之前，Orchestrator 会写入 Agent 的初始配置。负载（`src/rolemesh/ipc/protocol.py` 中的 `AgentInitData`）刻意做得很"胖" —— Agent 启动所需的每一项状态都搭载在这一个 KV 条目里，这样新容器在产出输出之前不需要任何额外的往返。字段大致分为以下几组：

- **会话上下文** —— `prompt`、`chat_jid`、`group_folder`、`session_id`、`is_scheduled_task`
- **多租户身份** —— `tenant_id`、`coworker_id`、`conversation_id`、`user_id`
- **每 coworker 配置** —— `assistant_name`、`system_prompt`、`role_config`
- **权限** —— 一个 4 字段的 dict；详见 `auth-architecture.md`
- **外部 MCP** —— `mcp_servers`；详见 `external-mcp-architecture.md`
- **审批模块** —— `approval_policies`；详见 `approval-architecture.md`
- **Safety 框架** —— `safety_rules` + `slow_check_specs`；详见 `safety/safety-framework.md`

对每一组与模块绑定的字段，"本次运行不存在"用 `None` 表示，于是当没有任何策略适用时，容器会完全跳过该模块的 hook 注册 —— IPC 契约让"模块禁用"的开销为零。

Agent 在启动时一次性读取这份数据，然后开始执行。

**为什么用 KV Store 而不是 stdin？** Agent 容器在 Kubernetes 中可能被调度到另一个节点上。它无法跨网络接收 stdin 管道。而 KV 是拉取模式 —— 容器启动后，读取自己的配置，开始工作。Orchestrator 在创建容器*之前*就已经写好 KV 条目，因此不存在竞态。

**TTL**：1 小时。即便容器在没有读取条目的情况下崩溃，条目也会自动清理。

### 通道 2：流式结果

**方向**：Agent → Orchestrator
**NATS 原语**：JetStream
**Subject**：`agent.{job_id}.results`

每个结果块都是一条 JSON 消息：

```json
{
  "status": "success",
  "result": "Here is the ad performance analysis...",
  "newSessionId": "session-uuid-for-resume",
  "error": null
}
```

Agent 在执行过程中会发布多个结果块（流式）。Orchestrator 订阅之后实时把每个结果块转发给用户。容器退出前的最后一条消息就是最终的确定结果。

**为什么用 JetStream 而不是 stdout 标记？** JetStream 消息是结构化 JSON —— 没有标记解析，也不会被乱入的输出污染。消息是有序且被 ack 的。如果 Orchestrator 在流的中途重启，可以重放尚未 ack 的消息。

**基于活动的超时**：每收到一条结果就会重置 Orchestrator 的超时计时器。如果在超时窗口内没有任何结果到来，则容器会被停止。

### 通道 3：控制信号 + 追加消息

**方向**：Orchestrator → Agent
**NATS 原语**：JetStream（追加消息 + 中断）+ Core NATS 请求-应答（关停）

这条通道向运行中的 agent 传递三种不同的控制信号：

#### 追加消息（JetStream `agent.{job_id}.input`）

当用户在 Agent 仍在运行（空闲等待输入）时发送额外消息，Orchestrator 会把这些消息以 JetStream 消息的形式发布：

```json
{"type": "input", "text": "Also check ASIN B08YYY"}
```

Agent 订阅这个 subject。在 Claude SDK 后端中，追加消息会被喂入 SDK 的 `query()` 函数所消费的 `MessageStream`；在 Pi 后端中，它们会被追加到当前活跃的 session 上。无论哪种方式，agent 都把它们视为同一段对话的延续，而非新的一个 turn。

#### 停止信号（JetStream `agent.{job_id}.interrupt`）

中止**当前 turn**，但不关闭容器。Agent 用 ordered consumer + `DeliverPolicy.NEW` 的方式订阅，因此当 agent 的事件循环正忙时，消息会被 JetStream 缓存。UX 设计的考量以及 agent 端的 ack 契约见 `docs/steering-architecture.md` 和 `docs/backend-stop-contract.md`。

**为什么停止信号用 JetStream 而不是 Core NATS？** 早期原型用的是 Core NATS pub/sub 加请求-应答。在轻负载下能用，但在 Pi 后端的流处理负载下就垮了：发布发生时 Core NATS 的订阅未必已经注册，于是抛出 `NoRespondersError` 并把停止信号悄悄丢掉。JetStream 会先把消息存起来，等 consumer 就绪后再投递 —— 对这一类信号而言，持久性比 ack 延迟更重要。

#### 关停信号（Core NATS 请求-应答 `agent.{job_id}.shutdown`）

当 Orchestrator 想**主动关闭容器本身**时使用 —— 空闲超时、被更高优先级任务抢占、调度器在任务完成后驱动的关停。Agent 对请求做 ack；这个 ack 告诉 orchestrator 它现在可以放心停止 Docker 容器，不会截断尚在进行中的工作。

**为什么关停用请求-应答（Core NATS），而中断用 JetStream？** 关停信号在 orchestrator 端是和容器拆除明确绑定的 —— 延迟很重要，而且容器无论如何马上就要消失，所以持久性无关紧要。中断恰恰相反：容器还会继续运行，消息必须能熬过 agent 的繁忙时段，因此持久性比 ack 延迟更重要。

### 通道 4：Agent 发给用户的消息

**方向**：Agent → Orchestrator
**NATS 原语**：JetStream
**Subject**：`agent.{job_id}.messages`

Agent 可以通过 `send_message` MCP 工具主动给用户发送消息（进度更新、通知）：

```json
{
  "type": "message",
  "chatJid": "tg:12345",
  "text": "Found 3 underperforming campaigns. Analyzing each...",
  "groupFolder": "main",
  "timestamp": "2026-03-28T10:00:00+00:00",
  "sender": null
}
```

Orchestrator 用 durable consumer `orch-messages` 订阅 `agent.*.messages`（用通配匹配所有 job ID）。它会按发起请求的 coworker 的 `AgentPermissions` 做授权校验（参见下文 "Authorization Model"），然后把消息路由到合适的通道（Telegram、Slack、WebUI 等）。

### 通道 5：任务操作

**方向**：Agent → Orchestrator
**NATS 原语**：JetStream
**Subject**：`agent.{job_id}.tasks`

Agent 可以通过 MCP 工具创建和管理定时任务：

```json
{
  "type": "schedule_task",
  "taskId": "task-1711612800000-a1b2c3",
  "prompt": "Daily ad performance check",
  "schedule_type": "cron",
  "schedule_value": "0 8 * * *",
  "context_mode": "group",
  "targetJid": "tg:12345",
  "groupFolder": "main"
}
```

支持的操作有：`schedule_task`、`pause_task`、`resume_task`、`cancel_task`、`update_task`。（最初的 NanoClaw 还暴露了 `refresh_groups` 和 `register_group`，但这些已经在多租户 Auth 重构期间被移除 —— 组注册现在是不走这条通道的管理员端操作。）

Orchestrator 用 durable consumer `orch-tasks` 订阅 `agent.*.tasks`。授权按发起请求的 coworker 的 `AgentPermissions` 强制执行（参见下文 "Authorization Model"）。

### 通道 6：快照

**方向**：Orchestrator → Agent
**NATS 原语**：KV Store，bucket 为 `snapshots`
**Key**：`{tenant_id}.{group_folder}.tasks`

在启动容器之前，Orchestrator 会写入当前的定时任务快照，并预先按发起请求的 coworker 被允许看到的范围进行裁剪 —— orchestrator 负责切片，从而 agent 拿到的就是一份现成可读的列表。Agent 通过 `list_tasks` MCP 工具读取它。权限语义在 `auth-architecture.md` 中详述。

数据是某个时间点的快照 —— 不是实时流。这样做是合适的，因为 Agent 需要查询当前状态，而不是订阅变更；如果 agent 运行期间有任务被新增或暂停，下一次容器启动时会读到新状态。

最初的 NanoClaw 还为 `list_groups` / `register_group` 发布过一份 `groups` 快照。它在 Auth 重构期间被移除：组注册已经从 agent IPC 表面移出，因此 agent 端不再需要 groups 快照。

**为什么用 KV Store 而不是 JetStream？** 快照回答的是"当前状态是什么？"—— 最新值覆盖旧值的语义。JetStream 适合那些顺序与历史都重要的事件流。KV 更简单，且对此用例的语义也更准确。

## Subject 命名约定

```
agent.{job_id}.results        # Channel 2
agent.{job_id}.input          # Channel 3 (follow-ups)
agent.{job_id}.interrupt      # Channel 3 (Stop button — JetStream)
agent.{job_id}.shutdown       # Channel 3 (close container — Core NATS request-reply)
agent.{job_id}.messages       # Channel 4
agent.{job_id}.tasks          # Channel 5
agent.{job_id}.safety_events  # Safety Framework V2 — fire-and-forget audit events
```

**为什么用 `job_id` 而不是 `coworker_id`？** 同一个 coworker 可能同时拥有多个并发容器 —— 一个处理用户消息，另一个跑定时任务。`job_id` 在每次容器调用中都是唯一的，可以保证精确路由。它在容器创建时被生成为 `{group_folder}-{uuid_hex[:12]}`。

JetStream 流 `agent-ipc` 抓取所有匹配 `agent.*.(results|input|interrupt|messages|tasks|safety_events)` 的 subject。关停信号走的是 Core NATS（不是 JetStream）—— 理由见通道 3。

除了上文描述的 `agent.*` 命名空间之外，RoleMesh 的 NATS 总线上还跑着：

- `web.>`（`web-ipc` 流）—— FastAPI 与 orchestrator 之间的 WebUI 流量
- `approval.decided.*` / `approval.cancel_for_job.*`（`approval-ipc` 流）—— 审批模块的 worker 队列与 Stop 级联
- `egress.{rules,identity,mcp}.snapshot.request` —— egress 网关在启动时调用 orchestrator 的请求-应答 RPC
- `egress.mcp.changed`、`safety.rule.changed` —— fire-and-forget 广播，用于在网关与 agent 容器内热加载缓存
- `orchestrator.agent.lifecycle` —— agent 容器的启动/停止生命周期事件

它们各自由所属模块文档化（`webui-architecture.md`、`approval-architecture.md`、`safety/safety-framework.md`、`egress/deployment.md`）—— 它们属于各自独立的关注点，只是恰好共用同一台 NATS server。

## NATS 基础设施

### JetStream 流

orchestrator 为 agent IPC 维护一个流；另外两个流（`web-ipc`、`approval-ipc`）与它共存于同一台 NATS server，但分别由 WebUI 和审批模块拥有 —— 它们在各自模块的文档中描述。

```python
StreamConfig(
    name="agent-ipc",
    subjects=[
        "agent.*.results",
        "agent.*.input",
        "agent.*.interrupt",
        "agent.*.messages",
        "agent.*.tasks",
        "agent.*.safety_events",
    ],
    max_age=3600.0,  # 1 hour TTL — auto-cleanup
)
```

这里使用 LIMITS retention（不是 WorkQueue），因为 Orchestrator 与 Agent 在同一个流内订阅不同的 subject —— WorkQueue 模式下每个 subject 只能有一个 consumer。

流定义使用 `add_stream` 并带有 `update_stream` 的回退，从而让那些原本不包含 `agent.*.interrupt`（在 Steering 期间加入）或 `agent.*.safety_events`（在 Safety V2 期间加入）的旧部署在启动时就地更新配置，避免在滚动发布期间让 subject 滞留在错误的配置上。

### KV Bucket

```python
KeyValueConfig(bucket="agent-init", ttl=3600.0)   # Channel 1
KeyValueConfig(bucket="snapshots",  ttl=3600.0)   # Channel 6
```

两者都是 1 小时 TTL。即便消费者崩溃，条目也会自清理。

### 持久化 Consumer

Orchestrator 为 agent IPC fan-in 创建了两个 durable JetStream consumer：

- `orch-messages` —— `agent.*.messages`（通道 4）
- `orch-tasks` —— `agent.*.tasks`（通道 5）

Durable consumer 能在 Orchestrator 重启后存活；尚未处理的消息会在重连后被重放。Safety 与审批模块各自注册了自己的 durable consumer（例如 `orch-safety-events`、`orch-approval-cancel`）—— 它们在各自模块的文档中描述。

通道 2（结果）和通道 3（追加消息 + 中断）使用按 `job_id` 范围限定的临时订阅 —— 容器启动时创建，退出时取消订阅。这些订阅不需要持久性，因为它们与单个容器的生命周期绑定。关停信号走 Core NATS 请求-应答，因此根本没有 consumer。

## 容器环境变量

Orchestrator 给每个 agent 容器传递两个环境变量：

| 变量 | 示例 | 用途 |
|----------|---------|---------|
| `NATS_URL` | `nats://nats:4222`（EC-1+）或 `nats://host.docker.internal:4222`（旧版） | NATS server 地址 |
| `JOB_ID` | `main-a1b2c3d4e5f6` | 每次容器调用唯一 |

Agent 用 `NATS_URL` 连接，用 `JOB_ID` 作为 `agent.{job_id}.*` subject 的路由键。

`NATS_URL` 中的主机名取决于网络拓扑。当前默认（EC-1 之后）将 agent 放在 `Internal=true` 的桥接网络上，并把 NATS 以服务名 `nats` 接入；旧版部署使用 `host.docker.internal`。Orchestrator 会自动改写该 URL —— 完整拓扑见 `docs/egress/deployment.md`。

## 双方如何连接

### Orchestrator 端（`NatsTransport`）

```python
class NatsTransport:
    async def connect(self) -> None:
        self._nc = await nats.connect(url)
        self._js = self._nc.jetstream()
        # Create stream and KV buckets (idempotent)
        await self._js.add_stream(StreamConfig(name="agent-ipc", ...))
        await self._js.create_key_value(KeyValueConfig(bucket="agent-init", ...))
        await self._js.create_key_value(KeyValueConfig(bucket="snapshots", ...))
```

启动时初始化一次，在所有容器调用之间共享。

### Agent 端（在 `agent_runner/main.py` 中）

```python
nc = await nats.connect(NATS_URL)
js = nc.jetstream()

# Channel 1: Read initial input
kv = await js.key_value("agent-init")
entry = await kv.get(JOB_ID)
init_data = AgentInitData.deserialize(entry.value)

# Channel 3: Subscribe to follow-ups (JetStream),
# stop signal (JetStream, ordered + DeliverPolicy.NEW),
# and shutdown signal (Core NATS request-reply).
input_sub = await js.subscribe(f"agent.{JOB_ID}.input")
interrupt_sub = await js.subscribe(
    f"agent.{JOB_ID}.interrupt",
    cb=handle_interrupt,
    ordered_consumer=True,
    deliver_policy=DeliverPolicy.NEW,
)
shutdown_sub = await nc.subscribe(f"agent.{JOB_ID}.shutdown", cb=handle_shutdown)

# Channels 4, 5: Publish via MCP tools
# (fire-and-forget, using asyncio.ensure_future for non-blocking)
```

每个容器在启动时创建自己的 NATS 连接，退出时关闭。

## MCP 工具：Agent 端的 IPC 接口

Agent 不会直接调用 NATS 的 publish 函数。相反，IPC 操作会作为 **MCP 工具** 暴露给 LLM 调用：

| MCP 工具 | 通道 | NATS Subject / KV Key |
|----------|---------|-----------------------|
| `send_message` | 4 | `agent.{job_id}.messages` |
| `schedule_task` | 5 | `agent.{job_id}.tasks` |
| `pause_task` | 5 | `agent.{job_id}.tasks` |
| `resume_task` | 5 | `agent.{job_id}.tasks` |
| `cancel_task` | 5 | `agent.{job_id}.tasks` |
| `update_task` | 5 | `agent.{job_id}.tasks` |
| `list_tasks` | 6 | KV `snapshots.{tenant_id}.{group_folder}.tasks` |

这些工具被注册为**进程内 MCP server**（在 Claude SDK 后端中通过 `create_sdk_mcp_server()`；在 Pi 后端中作为内建的 `AgentTool` 实例）。无论哪种方式：
- MCP server 没有独立进程
- 工具调用就是直接的 Python 函数调用
- LLM 看到的是带 JSON Schema 参数的常规工具

这种设计把 NATS 通信逻辑集中在一个地方（`agent_runner/tools/rolemesh_tools.py`），同时让 LLM 与之交互的是干净、有文档的工具接口。

## 授权模型

IPC 层关于授权的契约只有一句话：**通道 4 / 通道 5 的负载携带 `tenantId` + `coworkerId`，但绝不携带发起请求的 agent 的权限。** agent_runner 从 `AgentInitData` 中（而不是从 LLM 中）填入这些字段，orchestrator 在响应请求之前会查找该 coworker 的权威 `AgentPermissions`。Agent 无法通过修改负载来提权，因为负载本来就不声明权限。通道 6 的快照同样在 orchestrator 侧预先做了过滤，因此即便是有 bug 的 `list_tasks` 调用也无法读取另一个租户的数据。

完整的权限模型 —— 字段、角色模板、多租户的设计依据 —— 在 `auth-architecture.md` 中。

## 错误处理与可靠性

### 容器崩溃

如果容器在尚未发布结果时崩溃，Orchestrator 的超时（默认 5 分钟）会触发。容器被清理，错误被报告给用户。

KV 条目（`agent-init`、`snapshots`）有 1 小时 TTL —— 即便没有显式删除，它们也会自清理。

### NATS server 重启

如果 NATS 重启，Orchestrator 会重连（3 次重试，每次间隔 1 秒）。JetStream 流和 KV 数据持久化到磁盘，不会丢消息。

Durable consumer（`orch-messages`、`orch-tasks`）会在重连后从最后一次 ack 的位置继续。

### Orchestrator 重启

如果 Orchestrator 在容器仍在运行时重启：
- 运行中的容器继续跑（它们是独立的 Docker 进程）
- `orch-messages` 和 `orch-tasks` 中尚未 ack 的消息会被重放
- 孤儿容器会在下次启动时被 `DockerRuntime.cleanup_orphans("rolemesh-")` 清理
- 进行中的任务的通道 2（结果）订阅会丢失 —— 这些容器会超时

### 消息顺序

JetStream 在同一个 subject 内保留消息顺序。通道 2 的结果会按 Agent 发布的顺序到达。通道 4 与通道 5 的消息按 durable consumer 内的顺序处理。

## 开发环境搭建

```yaml
# docker-compose.dev.yml
services:
  nats:
    image: nats:latest
    ports:
      - "4222:4222"   # Client connections
      - "8222:8222"   # HTTP monitoring dashboard
    command: ["--jetstream", "--store_dir=/data"]
    volumes:
      - nats-data:/data

volumes:
  nats-data:
```

`http://localhost:8222` 上的监控 dashboard 可以看到当前活跃的连接、流、consumer 和 KV bucket —— 调试 IPC 问题时很有用。

环境变量：`NATS_URL=nats://localhost:4222`（默认值，本地开发无需任何配置）。
