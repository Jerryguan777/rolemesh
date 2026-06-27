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

一个阻塞在决策上的容器没有任何输出，orchestrator 的 idle 机制本会把它当卡死回收。错误的修法是用心跳*假装*容器在忙。正确的修法是把真相告诉 orchestrator：这是一次已知的、有界的、合法的等待。收到 `approval_request` 时 orchestrator 为该会话**挂起** idle 回收；收到决策（或容器自身的 timeout-cancel）时**恢复**。容器自身的看门狗（`TURN_INACTIVITY_TIMEOUT`）则不动：它在运行时把自己的上限下取整到 `APPROVAL_TIMEOUT + 30s`，因此无论各 timeout 如何配置都永远抢不掉一个 pending 审批。（这条运行时下限取代了旧的 `APPROVAL_TIMEOUT < IDLE_TIMEOUT + 30s` 启动不变式，后者已废弃。）

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

- **一个 pending 审批占住一个 turn 槽**（其容器仍在 processing），最长 `APPROVAL_TIMEOUT`。三级 turn 准入上限与全局存活容器上限（`GLOBAL_MAX_CONTAINERS`）必须能吸收最坏情况，使审批不饿死普通运行。这是 loop 内模型的代价，是被刻意接受的取舍。
- **仅支持入参条件。** 策略基于调用自身的参数决策。跨调用或有状态的条件（"今天第三次退款"）按设计不在范围内。
- **仅 MCP 工具。** 内置工具（`Read`、`Edit`、`Bash`……）不受审批门控；它们由安全管线和容器加固治理。

## 参考：冻结契约（Frozen Contract）

> 实现按章节引用的规范化接口。蒸馏自原始实现 plan;章节号(§3–§11)被保留,使代码中 `(docs/12-hitl-approval-architecture.md §N)` 的指针能解析到这里。上文是"为什么",这里是"确切形状"。

### §3 IPC 契约 —— NATS 主题

所有审批流量都经 orchestrator 中转;容器从不直接与用户对话。三个主题。接收时必须丢弃未知字段(滚动升级的前向兼容)。

**§3.1 `agent.{job_id}.approval_request` —— container → orchestrator**
```json
{
  "request_id": "uuid",
  "tenant_id": "uuid",
  "coworker_id": "uuid",
  "conversation_id": "uuid | null",
  "user_id": "uuid | null",          // approver = creator; null => fail-closed block
  "job_id": "string",
  "policy_id": "uuid | null",        // safety-rule bridge 时为 null(provenance 走 triggered_by,§11.4)
  "mcp_server_name": "string",
  "tool_name": "string",
  "params": { },                      // the tool call arguments
  "action_summary": "string",         // short human-readable summary for the card
  "requested_at": "iso8601",
  "expires_at": "iso8601"             // requested_at + APPROVAL_TIMEOUT
}
```
**§3.2 `agent.{job_id}.approval_decision` —— orchestrator → container**
```json
{ "request_id": "uuid", "decision": "approve | reject", "decided_by": "uuid", "note": "string | null" }
```
**§3.3 `agent.{job_id}.approval_cancel` —— container → orchestrator**
```json
{ "request_id": "uuid" }
```
从容器的 `finally` 发出(幂等):reject / timeout / 用户 Stop(CancelledError)/ exception —— 每一条"本轮结束"容器能确知的路径。

### §4 DB schema

单谓词 RLS(`tenant_id = current_tenant_id()`)覆盖全部四种 DML;角色 `rolemesh_app`(NOBYPASSRLS)/ `rolemesh_system`(BYPASSRLS)。**DB 是权威**;内存中的 suspend 状态只是缓存(见 §8 重启恢复)。容器在 init 时载入的 *policy snapshot* 即该表中该租户的 `enabled` 行。

**§4.1 `approval_policies`**
```
id   uuid PK            tenant_id uuid NOT NULL
mcp_server_name text    tool_name text         -- exact name or "*"
condition_expr  jsonb   -- see §7
enabled bool DEFAULT true   priority int DEFAULT 0
created_at / updated_at timestamptz
-- indexes: (tenant_id, enabled), (tenant_id, mcp_server_name, tool_name)
-- RLS: tenant_id = current_tenant_id()
```
**§4.2 `approval_requests`**
```
id uuid PK   tenant_id uuid NOT NULL   coworker_id uuid NOT NULL
conversation_id uuid NULL   policy_id uuid NULL
user_id uuid NULL          -- approver = creator; null => fail-closed
job_id text NOT NULL
mcp_server_name text   action jsonb        -- { tool_name, params }
action_summary text
status text            -- pending|approved|rejected|expired|cancelled
decided_by uuid NULL   note text NULL
requested_at timestamptz   expires_at timestamptz NOT NULL   decided_at timestamptz NULL
-- indexes: partial on (status) WHERE status='pending'; (job_id)
-- RLS: tenant_id = current_tenant_id()
```
无 `approval_audit_log` 表,无 `resolved_approver_user_ids`(self-approval ⇒ approver 即 `user_id`),无 `action_hashes`(无 replay)。

### §5 配置与不变量
```
APPROVAL_TIMEOUT          core/config.py   300_000 ms (5 min)   container await + DB expires_at 共用
TURN_INACTIVITY_TIMEOUT   core/config.py   420_000 ms (7 min)   每轮 watchdog 静默上限
```
容器 watchdog（container_executor.py）把每轮静默上限取 per-coworker `container_config.timeout` 覆盖值、否则取 `TURN_INACTIVITY_TIMEOUT`，再下取整到 `max(base, APPROVAL_TIMEOUT + 30_000)`。这条运行时下限保证审批 await 总是先于 watchdog 触发 —— 无论 `IDLE_TIMEOUT` / per-coworker 覆盖如何配置,watchdog 永远抢不掉一个审批。它取代了旧的 `APPROVAL_TIMEOUT < IDLE_TIMEOUT + 30_000` 启动断言（已移除），后者把审批安全耦合到了暖空闲存活时长上。

**队列 key 规则**(复用,勿重造):`conversation_id or coworker_id`。容器与它的审批 suspend 状态必须落在同一个 `_GroupState` 条目上,所以用这个确切规则。

### §6 并发模型

- `HookRegistry.emit_pre_tool_use` 串行迭代各 handler;只注册了一个审批 handler。
- 但一个 turn 内的多个 `ToolUseBlock` 会被两个后端**并发**派发(Claude 并行 tool calls;Pi 对批次 `asyncio.gather`)。所以**一个 turn 内可以同时有多个审批 pending** → suspend 状态必须是 `set[request_id]`,绝不能是 bool。

### §7 策略条件语言(纯函数,fail-closed)

`evaluate_condition(expr, params) -> bool`,位于 `agent_runner/approval/policy.py` —— 零外部依赖,由容器 hook 和 orchestrator 共用。
```
{"always": true}
{"field": "amount", "op": ">", "value": 100}
{"and": [ ... ]}    {"or": [ ... ]}
```
Ops:`== != > >= < <= in not_in contains`。**Fail-closed**:缺字段 / 类型不匹配 / 表达式畸形 / 任何异常 ⇒ 都需要审批。匹配(`find_matching_policy`):取该租户的 `enabled` 策略;server 匹配 AND(`tool_name == "*"` 或精确)AND 条件为真;多命中取 `priority` 最高,再取 `updated_at` 最新。写入时的严格伴侣 `validate_condition_expr` 在 API 层(422)拒绝畸形 `condition_expr`。

### §8 idle 挂起 / 恢复 / 重启恢复

一次有界的 ≤5 分钟审批等待必须**显式挂起** idle 回收,而非伪装存活(status 心跳不会重置 idle 计时器)。存在三条回收路径;挂起必须全部关闭。

- **Suspend**(收到 `approval_request`):持久化 `pending` + `expires_at`;cancel idle handle;强制 `idle_waiting = False`(+ 断言);`awaiting_approval[key].add(request_id)` —— 是 **set** 不是 bool;发一条"⏳ 等待审批"状态。挂起期间,任何路径(包括新的后续消息)都不得重新武装 idle。
- **Resume**(收到 `approval_decision` 或 `approval_cancel`):`discard(request_id)`;**当且仅当 set 此刻为空** → 从现在起重新武装一整个 `IDLE_TIMEOUT`;若是 decision,转发给容器。
- **Expiry watcher**:容器被 SIGKILL 的兜底 —— orchestrator 在 `expires_at` 将该行置为过期并触发硬通道通知。
- **重启恢复**:`_groups` 在内存中、重启即失,但持审批的容器会存活。启动时扫描 `approval_requests WHERE status='pending'`;逐行 —— **未过期** → 重建 `_GroupState`、replay suspend 动作、重建 `approval_decision` 路由(主题由 `job_id` 推导)、重新武装 expiry watcher(只 reload 不重建会让容器立刻被回收);**已过期** → 标记 `expired` + 硬通道通知。整个过程幂等。
- **三层清理**:(1) 正常 `approval_decision`;(2) 容器结束时 `finally` 发出的确定性 `approval_cancel`;(3) 容器被 SIGKILL → orchestrator expiry watcher + 重启恢复。
- **决策竞态 / 幂等**:容器侧 Future 先到先赢;orchestrator 侧行级 `status` 状态转移幂等;两侧收敛。

### §9 已知风险与"审批后存活"结论(R1)

**R1 —— 被门控的 tool call 能挺过这次 block 吗?能,且在进程内。** 该 block 是协作式的(hook `await` 一个 `asyncio.Future`;事件循环从不冻结,所以 MCP keepalive、NATS 决策投递、idle/中断轮询都在持续)。MCP 连接是容器/turn 级的、非每次调用,我方代码不会在 block 期间关闭它。无容器内持有的凭证会过期 —— LLM 凭证由 credential proxy 按请求注入(容器只持 `ANTHROPIC_BASE_URL`),外部 MCP 鉴权走静态的按请求头 `X-RoleMesh-User-Id`。残留(无法单测):*远端* MCP server 可能在等待期间断掉空闲 HTTP/SSE 会话;5 分钟上限把窗口压短,透明重连或更低 timeout 可缓解。"审批后工具失败"没有单独的硬通道 —— 它走**正常的工具错误路径**(Claude `PostToolUseFailure`;Pi 带 `is_error` 的 `tool_result`);重试若再次命中 hook,会产生一个**新的**审批请求。

**运营**:每个 pending 审批占住一个 turn 槽 ≤ `APPROVAL_TIMEOUT`;三级 turn 上限与 `GLOBAL_MAX_CONTAINERS` 必须留余量(被接受的取舍;触顶则 log)。

### §10 投递、策略 CRUD 与 SPA 界面

**(S4)投递与双通道通知。** 目标解析:`conversation_id → channel_bindings → channel_chat_id`;无活跃会话的定时任务回退到最近一个。**Telegram**:内联 ✅/❌ 卡片 + `CallbackQueryHandler`,`callback_data` 为 `apr:{request_id}` / `rej:{request_id}`;**IDOR 防护** —— approver 身份从 auth 握手(ticket + DB)解析,绝不信任客户端 payload。**Web**:一个 v1 WS 客户端 frame(pydantic 成员 + `WsClientFrameModel` union + `ws_stream` 接收分支 + NATS publish + OpenAPI 重生成 + ts client)推送审批事件。**双通道结果**:软(block `reason` → agent 上下文,用于自然措辞)+ 硬(orchestrator 确定性地把卡片就地改为 "❌ rejected" / "⏰ expired",无 LLM)—— 硬通道是投递保证。

**(S5)策略 CRUD 与 pending 读取。** REST:`GET/POST /api/v1/approval-policies`、`GET/PATCH/DELETE /api/v1/approval-policies/{id}`,严格租户隔离(RLS + 显式 `WHERE tenant_id`);畸形 `condition_expr` → 422,经 `validate_condition_expr`。`GET /api/v1/approval-requests`(可选 `conversation_id`)只返回调用方租户的 pending 行,暴露 tool 名 + summary,绝不暴露原始 params。**SPA**:`rm-approval-card` 渲染 `event.approval.requested`,经 `V1WsClient.sendApprovalDecision`(`request.approval_decision` frame,身份由服务端盖戳)中继点击,并在 `event.approval.resolved` 时就地更新;(重)连接时 chat panel 从 REST 读取重渲染在途卡片。策略 CRUD UI:`rm-approval-policies-page`(Settings → Governance),带一个结构化的 §7 条件构建器;过于复杂、平铺构建器装不下的存量表达式以只读方式打开。

### §11 实现产出

**§11.1 block-and-await vs 已删除的 block-and-replay。** 核心差异见上文"架构分叉":已删除的 v6.1 立即返回 `block=True`,由带外 worker 稍后 re-POST 该动作;本次重设计就地 `await` 决策,由**同一容器、同一 turn** 执行被批准的调用,使 agent 在自己的 ReAct loop 中拿到真实结果。代价 —— 占住一个容器 ≤ `APPROVAL_TIMEOUT` —— 正是 §8 机制使之安全的部分。

**§11.4 Safety→approval bridge(PRE_TOOL_CALL)。** 安全管线与 HITL 审批在唯一有意义的阶段相接 —— **PRE_TOOL_CALL** —— 此处 agent 已在自己容器内 block 在一个 tool call 上。那里 `pipeline_core` 把触发规则的 provenance(`firing_rule_id` / `firing_check_id`)盖到 verdict 上;遇到 `require_approval` verdict,容器 handler 构造 `triggered_by = {kind: "safety_rule", rule_id, check_id, stage}` 并经**共享的 `ApprovalAwaiter`** 发布 `approval_request` —— 与业务 hook 同一个 block-and-await 原语(`policy_id` 为 null;provenance 走 `triggered_by`)—— 并 block 在同一个 `APPROVAL_TIMEOUT` 上。orchestrator 持久化 `triggered_by`,并在 `event.approval.requested` WS 推送与 REST 投影上转发,使 SPA 渲染琥珀色"被某条安全规则暂停"横幅。批准 → 工具就地、同 turn 执行;拒绝 / 超时 / 取消 → block verdict 抵达 model。其他阶段保留对 `require_approval` 的硬拦截别名(INPUT_PROMPT / POST_TOOL_RESULT 没有干净的"批准后继续"语义;MODEL_OUTPUT 在 orchestrator 侧运行、没有在等待的容器)。SPA 凭 `triggered_by` 是否存在来区分两类 gate。

---

## 相关文档

- [`9-hooks-architecture.md`](9-hooks-architecture.md) —— 审批所构建于的统一 `PreToolUse` 网关，及其继承的 Claude/Pi 桥接对等性
- [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md) —— 为何有两个后端，以及为何后端中立的审批机制很重要
- [`safety/safety-framework.md`](safety/safety-framework.md) —— 审批所并列的 `PRE_TOOL_CALL` 拦截路径（拦截 vs. 暂停-问人）
- [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md) —— 审批请求/决策握手所走的 NATS 主题
