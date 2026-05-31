# 人工审批（HITL）工具调用架构

本文说明 RoleMesh 如何在 agent **执行**一次高风险工具调用**之前**请人工审批，以及这套审批机制为何是现在这个形态。

重点在于 *为什么*：驱动它的需求、那一个决定其余一切的架构分叉（带外执行子系统 vs. loop 内的有界暂停），以及让"阻塞等待"能在一个被沙箱化、会被 idle 回收的容器里安全存活的几条关键不变式。

目标读者：扩展审批策略模型、接入新投递渠道，或排查审批为何超时、卡住、或在一个后端触发而另一个不触发的开发者。

---

## 背景：有些工具调用不该在无人值守时直接执行

RoleMesh coworker 会代表用户调用外部 MCP 工具——发起退款、记一笔账、删除记录、给客户发消息。多数是例行操作。少数后果重大到组织希望"人先看一眼再让 agent 动手"，并且希望这道闸门是**有条件的**：不是"每一次 `refund` 都要批"，而是"金额大于 100 的 `refund` 才批"。

安全管线（见 [`safety/safety-framework.md`](safety/safety-framework.md)）已经能在 `PRE_TOOL_CALL` 阶段对工具调用分类并**拦截**。但拦截不适合干这件事：它直接拒绝动作，没有"让人来决定"的出路。我们需要的是介于*放行*与*拦截*之间的第三种结果——**暂停、问人，再根据答复放行或拒绝**。

这第三种结果就是人工审批（HITL）。

## 需求

1. **可条件化的、用户自定义策略。** 租户管理员声明哪些调用需要审批，按 **MCP server + tool 名 + 参数条件**（如 `amount > 100`）。未命中的一律无摩擦放行。
2. **自审路由。** 审批人就是这件工作的归属者：发消息的用户；定时任务则是创建该任务的用户。请求发给*他本人*。
3. **有界等待。** pending 的审批不会无限等。超过 `APPROVAL_TIMEOUT`（默认 20 分钟）自动拒绝。
4. **优雅过期。** 拒绝或超时后，agent 告知用户发生了什么（"那个操作被拒绝/超时了；需要的话我可以重新发起"），且对话之后可继续——用户回来说"继续吧"，agent 重新发起审批。
5. **后端对等。** 无论 coworker 跑在 Claude SDK 还是 Pi 上，审批行为必须一致。
6. **多租户隔离。** 策略、请求、审批决策都在 Postgres RLS 下按租户隔离，与其他租户资源一致。

---

## 架构分叉：带外执行子系统 vs. loop 内暂停

构建审批有两种根本不同的路子。RoleMesh 先做了第一种，整套删除，再从零（greenfield）重建了第二种。理解二者的区别是本文的钥匙——其余每一个设计决策都派生自它。

### A 方案 —— 审批作为一套独立的带外执行子系统（已删除）

审批是一套自包含、带外的系统。当一次调用需要审批时，工具调用被**从 agent 手里抠出来**：请求被持久化，容器继续往下走；一旦有人批准，一个专门的 `ApprovalWorker` 消费 `approval.decided.*` 事件、认领请求、**自己**经 credential proxy 去打那次 MCP 调用——再想办法把结果塞回去。

它换来一件事：等待可以任意长，因为没有任何东西被阻塞——状态在 Postgres 里。但为此付出极大代价：一个 10 状态的状态机、一个审计触发器、一个 worker 和一个 executor、为重投准备的幂等键、两条 reconcile 补偿循环、跨重启去重、一个 `submit_proposal` 工具、一条 `auto_execute` collapse 路径。执行机制还与后端纠缠（重跑调用的一方必须把结果重新注入到*那个*后端的 transcript）。规模约两万多行。

A 方案已被整套删除（`feat/remove-approval`）。

### B 方案 —— 审批作为 agent 自身 loop 里的一次有界暂停（当前）

审批就是 **agent 自己的 ReAct loop 里一次带超时的 `await`**。没有带外 executor。`PreToolUse` hook 在策略命中时阻塞于 `asyncio.wait_for(decision, APPROVAL_TIMEOUT)`：

- **批准** → hook 返回"放行"，agent 在**同一个回合**里把这次工具调用真正跑完，真实结果自然回流给 LLM。
- **拒绝 / 超时** → hook 返回一个 block verdict，其 `reason` 被当作 tool result 喂回，agent 用自己的话在 loop 里接着说（"那个被拒了——要我重新发起吗？"）。

对 agent 而言，审批只是"一次工具调用暂时卡了一下"。等待在结构上就是短的：容器至多阻塞 `APPROVAL_TIMEOUT`，到点自动拒绝并退出。长等待不是被*绕开*的——而是被**直接定义掉**的。

### 为什么选 B

| 维度 | A —— 带外子系统 | B —— loop 内暂停 |
|---|---|---|
| **执行模型** | 一个独立 worker 重跑被批准的调用并重新注入结果 | agent 自己在原回合里跑这次调用 |
| **与 ReAct loop 的关系** | 脱离 loop——推理-行动被打断并被接管 | 贴合 loop——一次 `PreToolUse` `await`；批了让工具跑，拒了变成 agent 读到的 tool result |
| **等待与超时** | 长等待：容器退出、状态进 Postgres、审批到达再唤醒 | 短等待：容器阻塞 ≤ `APPROVAL_TIMEOUT`，超时自动拒绝，下次再继续 |
| **后端耦合** | 执行与各后端 transcript 纠缠 | 零耦合——纯 `async hook + asyncio.wait_for`；Claude 与 Pi 通吃 |
| **复杂度** | 约 2 万行：10 状态机、worker、executor、审计触发器、幂等、两条 reconcile、proposal、auto-execute | 4 状态（pending/approved/rejected/expired）、一个 gate hook、一条决定通道、一条挂起/恢复规则 |

一句话：**A 是"审批 = 一个会替 agent 执行的外部系统"，B 是"审批 = agent 自己等一下人点头"。**

B 之所以特别适合 RoleMesh，是因为 hook 层已经给了我们一个 backend 无关的 `PreToolUse` 网关，在 Claude 和 Pi 上行为一致（见 [`9-hooks-architecture.md`](9-hooks-architecture.md)）。审批只需要这个网关加上一个等待的办法——而 loop 内等待恰好删掉了让 A 沉重且后端耦合的整个带外执行问题。

---

## 设计目标（B 方案）

1. **审批是一种 hook 结果，不是一个子系统。** 它挂在统一的 `HookRegistry.on_pre_tool_use` 之后，与安全管线用的是同一道网关。任何工具调用都只由 agent 自己执行，绝无他者代劳。
2. **结构上后端中立。** 因为 B 从不带外重跑调用，就不存在"该注入到哪个后端"的问题。同一个 hook handler 在 Claude 和 Pi 上原样运行。
3. **有界、可自愈的等待。** 每个 pending 审批都有硬截止时间，且有多条独立的解决路径，使得丢消息、orchestrator 崩溃、容器被杀都不会让一段对话永久卡死。
4. **阻塞等待必须能在容器自身的存活机制下存活。** 一个静默阻塞 20 分钟的容器，不能被误判为卡死并回收。
5. **最小状态。** 4 个请求状态、一张策略表、一张请求表。无审计触发器、无 worker、无 proposal。

---

## 架构

```
        Agent ReAct loop（在沙箱容器内）
        ┌─────────────────────────────────────────────────┐
        │  LLM 发出工具调用: mcp__erp__refund {500}        │
        │                  │                               │
        │                  ▼                               │
        │     HookRegistry.on_pre_tool_use                 │
        │       ApprovalHookHandler                        │
        │        • 匹配租户策略（server+tool+条件）        │
        │        • 未命中 → 放行（return None）            │
        │        • 命中 → publish approval_request，       │
        │                 await 决策（≤ TIMEOUT）          │
        │            批准 → 放行，工具在 loop 内执行        │
        │            拒绝 → block(reason) → 变成 tool result│
        │            超时 → block(reason) → 变成 tool result│
        └───────────────┬───────────────────▲─────────────┘
                approval_request │           │ approval_decision
                                 ▼           │
        ┌─────────────────────────────────────────────────┐
        │  Orchestrator                                    │
        │   • 持久化请求（pending, expires_at）            │
        │   • 挂起该会话的 idle 回收                        │
        │   • 解析审批人（自审），投递到渠道               │
        │   • 决策/过期时：恢复 idle 回收                   │
        └───────────────┬─────────────────────────────────┘
                        │ 卡片+按钮 / WS 事件
                        ▼
                  Telegram / Web  →  审批人
```

两半，一次带超时的握手：

- **容器侧**是单个 hook handler：匹配策略、发一次请求、阻塞。它从不直连渠道，也从不带外执行任何东西。
- **Orchestrator 侧**持久化请求、路由给正确的人、把决策转发回去，并且——关键地——**在等待期间挂起该容器的 idle 回收**。

---

## 设计要点

以下是承重部分，其余都是机械工作。

### 1. 审批是 `PreToolUse` 的一种结果，且只作用于 MCP 工具

handler 跑在统一 hook 层内。非 MCP 工具（`Read`、`Bash`……）立即放行。对于 `mcp__server__tool` 调用，它用该调用的参数去求值租户策略；未命中放行，命中暂停。因为这道网关就是安全管线用的那个 backend 无关的 `on_pre_tool_use`，Claude 和 Pi 免费获得审批能力——没有任何后端专属代码。

### 2. 策略按租户、可条件化、fail-closed

一条策略按 **MCP server + tool 名（精确或 `*`）+ 结构化参数条件** 匹配（`{"field": "amount", "op": ">", "value": 100}`，可用 `and`/`or` 组合）。匹配器是容器与 orchestrator 共用的纯函数。若条件无法求值——缺字段、类型不符、表达式畸形——则 **fail-closed**（要求审批），绝不放行。

### 3. 等待在 loop 内、有界；清理由容器负责

hook 阻塞于 `asyncio.wait_for(decision, APPROVAL_TIMEOUT)`。在任何出口——批准、拒绝、超时、用户 Stop、异常——handler 的 `finally` 都会对任何仍未决的请求确定性地发出一个 cancel。容器是唯一确知"这一回合结束了"的一方，所以由它负责清理。阻塞等待还意味着**被批准**的调用直接在同一回合里继续——无需会话 resume，无需结果重新注入。

### 4. orchestrator 在等待期间挂起 idle 回收——而不是伪装存活

一个阻塞 20 分钟的容器没有任何输出，orchestrator 的 idle 机制本会把它当卡死回收。错误的修法是用心跳*假装*容器在忙。正确的修法是把真相告诉 orchestrator：这是一次已知的、有界的、合法的等待。收到 `approval_request` 时 orchestrator 为该会话**挂起** idle 回收；收到决策（或容器自身的 timeout-cancel）时**恢复**。容器自身的看门狗则不动，由一条启动不变式（`APPROVAL_TIMEOUT < IDLE_TIMEOUT + 30s`）保证——它确保审批总是先解决。

### 5. 并发与崩溃安全是一等公民

单个 assistant 回合可以并行发出多个工具调用，因此一段对话可能同时有多个 pending 审批。所以挂起被记录为一个 **pending 请求 ID 的集合**——idle 回收只在最后一个清空时恢复，绝不在第一个完成时恢复。又因为内存里的挂起状态只是缓存，**数据库才是权威**：重启时 orchestrator 重载 `pending` 行，要么重新装上其截止时间，要么将其标记过期并通知用户。任何单点故障——丢消息、容器被杀、orchestrator 重启——都不会让对话永久搁浅。

### 6. 通知用户是确定性的，不依赖 LLM

拒绝或超时时，`reason` 被喂回 agent 上下文，让它*能够*用自己的话解释——体验好，但不保证（尤其是无人值守的定时任务）。所以 orchestrator **还会**直接、无条件地通知用户，把审批卡片就地编辑成"已拒绝"/"已超时"。两条通道：经 LLM 的软通道负责自然措辞，经 orchestrator 的硬通道保证必达。

---

## 被否决的替代方案

### 保留 A 方案，只是精简它

否决。A 的沉重不是偶然的——它是带外执行的内在属性。只要有一个独立 worker 去重跑被批准的调用，你就继承了结果重注入、后端耦合、重投幂等、reconcile。砍状态并不能去掉 executor；只有把执行搬回 agent 的 loop 才能。我们选择删除 A，而不是精简它。

### 用心跳让容器在等待期间保持"存活"

否决。状态心跳不会重置 orchestrator 的 idle 计时器（progress 事件在那里被刻意设为惰性），即便会重置，它也是个谎言：它把一次合法等待伪装成活动，其正确性押在"N 个心跳全部准时到达"上。挂起/恢复把真相说一次，押在一次状态切换加一条静态不变式上。（见"设计要点 #4"。）

### 用一个布尔"审批 pending"标志代替集合

否决。一个回合内的并行工具调用可能同时开启多个审批。布尔标志会在第一个决策到达时清除并恢复 idle 回收，而其他审批还在等待——重新引入了挂起机制本就为防止的那个回收竞态。状态必须是按请求 ID 索引的集合。

### 让 agent 独自负责通知用户

否决其作为*唯一*通道。LLM 可能忘记、可能把结果表述得难以辨认、或根本没有回合可说（定时任务）。orchestrator 的确定性通知是保证；agent 的叙述是锦上添花。

---

## 已知约束

- **一个 pending 审批占住一个容器**，最长 `APPROVAL_TIMEOUT`。并发上限（`MAX_CONCURRENT_CONTAINERS`、`GLOBAL_MAX_CONTAINERS`）必须能吸收最坏情况，使审批不饿死普通运行。这是 loop 内模型的代价，是被刻意接受的取舍。
- **仅支持入参条件。** 策略基于调用自身的参数决策。跨调用或有状态的条件（"今天第三次退款"）按设计不在范围内。
- **仅 MCP 工具。** 内置工具（`Read`、`Edit`、`Bash`……）不受审批门控；它们由安全管线和容器加固治理。

---

## 相关文档

- [`9-hooks-architecture.md`](9-hooks-architecture.md) —— 审批所构建于的统一 `PreToolUse` 网关，及其继承的 Claude/Pi 桥接对等性
- [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md) —— 为何有两个后端，以及为何后端中立的审批机制很重要
- [`safety/safety-framework.md`](safety/safety-framework.md) —— 审批所并列的 `PRE_TOOL_CALL` 拦截路径（拦截 vs. 暂停-问人）
- [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md) —— 审批请求/决策握手所走的 NATS 主题
