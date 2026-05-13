# 审批模块架构

本文档介绍 RoleMesh 的人工介入审批模块——一种允许管理员在不修改权限模型的前提下，将特定的外部 MCP 工具调用置于审核步骤之后才能执行的机制。

文档涵盖了为什么该模块是策略驱动而非权限驱动、考虑过哪些设计以及为何被否决、容器侧 hook 与 orchestrator 侧引擎之间的职责拆分，以及状态机所提供的并发与崩溃恢复保证。

目标读者：添加新审批流程（多步审核、策略模板）、调试某次决策为何未触发、接入需要审批拦截的新 MCP server、或将该模块移植到不同进程拓扑的开发者。

---

## 背景：权限不等于审批

RoleMesh 已经具有一个四字段 `AgentPermissions` 模型（`data_scope`、`task_schedule`、`task_manage_others`、`agent_delegate`，详见 [`6-auth-architecture.md`](6-auth-architecture.md)）。它回答的是"这个 agent 究竟能不能使用这个工具？"——一个在工具注册时刻评估的二元答案。

该模型对另一类问题保持沉默：**"这个 agent 能不能在没有人类先把关的情况下，立即做这件特定的事情？"** 考虑下列情形：

- agent 可以调用执行退款、调整价格、修改访问授权的 MCP 工具。ERP/CRM server 本身没有任何授权上下文；它信任 JWT 所声称的调用者。
- 一位租户管理员可能乐意让 agent 在无监督下*读取* CRM 记录，但希望对任何超过 \$1000 的退款进行人工审批。
- 风险并不是"流氓 agent"（那由 `AgentPermissions` 覆盖）；而是"合理但判断失误的 agent"——例如为了关闭 200 条投诉而退款 200 笔订单，但实际上只有 3 条投诉是真实有效的。

未能解决该形态问题的方案：

- **把审批硬编码进 MCP server。** 无法扩展——每个 MCP 厂商都得实现我们的审批语义。
- **在 `AgentPermissions` 上用更细粒度的标志位。** 决策是按调用而非按工具进行的，并且依赖运行时参数（`amount > 1000`）。布尔标志无法表达"仅当金额超过阈值 X 时阻止"。
- **把每次 MCP 调用都接入手动 UI。** 破坏使用体验——没人愿意为了让 agent 可用，去审批 50 次只读的 CRM 查询。

我们构建的模块是**策略驱动**的：管理员以 `(mcp_server, tool_name, condition_expr)` 和一组特定审批人为索引，编写声明式规则。hook 系统决定哪些调用命中。当不存在任何策略时，该模块带来零运行时开销且 agent 行为按位一致——这是被测试套件钉死的属性。

---

## 设计目标

1. **未启用时零影响。** 一个没有审批策略的租户必须观察到与未引入该模块前完全一致的行为。`ApprovalHookHandler` 不会被注册，agent 层面不会为审批创建任何 NATS 订阅，热路径上也不会有额外的数据库查询。
2. **单实现的策略匹配器。** 容器侧和 orchestrator 侧代码导入**完全同一个文件**（`src/agent_runner/approval/policy.py`），因此 hook 和引擎绝无可能对"这次调用是否匹配？"产生分歧。
3. **决策/执行解耦。** REST decide 端点在写库 + NATS 发布之后约 100ms 内返回。实际的 MCP 执行在独立的工作进程中运行。50 个动作的批量不会占用审批人的 HTTP 连接。
4. **原子性状态转换。** 两位审批人在同一瞬间点击 Approve：恰好一位胜出，另一位看到 409。两个工作进程认领同一条已批准请求：恰好一位执行。两者都是单条 SQL 语句，无需 advisory lock。
5. **关口失败即拒。** 如果 hook 系统在检查策略时崩溃，该调用必须被阻止而非放行。这利用了现有 hook 系统的失败即拒契约（参见 [`9-hooks-architecture.md`](9-hooks-architecture.md) §"Fail-close vs fail-safe"）。
6. **只增不改的审计。** 应用层从不在 `approval_audit_log` 上暴露 update 或 delete。每次状态转换都是一条审计记录；没有任何一行会被重写。
7. **Stop 按钮联动。** 在某个 agent 回合上按下 Stop 会取消该回合下挂起的审批，避免审批人对已废弃的工作作出操作。
8. **两种 agent 后端，一份审批代码。** 不存在后端特异的分支。该模块挂入统一的 hook 系统。

---

## 已考虑的备选方案

### 方案 A——在每个 MCP server 内部强制执行审批

把审批要求下推：MCP server（ERP、CRM 等）自行实现"需要人工签字"的流程，并向 agent 返回一个待审批结果。RoleMesh 只是把它呈现出来。

**优点**

- 不需要 RoleMesh 侧的状态。代码路径最简。
- 即使 agent 绕过了 RoleMesh 的 hook 也能生效。

**缺点**

- 每个 MCP 厂商都要实现我们的审批 UX，并且要了解 RoleMesh 用户。
- 各 server 之间不一致——自研 ERP 和第三方 CRM 会用不同方式通知审批人。
- MCP 协议没有"挂起，稍后再来"这种标准——agent 不得不轮询。

**否决。** MCP server 应当对其运行的部署环境保持无知。审批是 RoleMesh 的概念。

### 方案 B——在 `AgentPermissions` 内部审批

在 `AgentPermissions` 上添加一个 `approval_required: dict[str, list[dict]]` 字段，列出按工具的策略。决策在容器侧 `PreToolUse` 时刻进行，往返一个最小化的 orchestrator 端点，内联等待。

**优点**

- 与现有权限模型统一。
- 不需要新表。

**缺点**

- 把两个正交的概念耦合在一起："是否允许 agent" 与 "这次具体调用是否需要审核"。管理员切换 `task_schedule` 时，存在意外触及审批语义的风险。
- 内联等待意味着 agent 回合会阻塞数分钟等审批人决策——在高负载下白白占着一个容器槽位。
- 没有批量审批。每次工具调用都要单独审核。

**否决。** 这两类关注点应当保持分离。`AgentPermissions` 保持布尔且快速；审批是一个有状态的旁路。

### 方案 C——通过外部审核工具的旁路

与现有的审批平台集成（例如某个 ChatOps bot、独立的 GRC 系统）。发出 webhook，等待回调。

**优点**

- 复用现有的审核 UX。

**缺点**

- 在功能上线第一天就引入外部依赖。
- 难以关联"审批 X 是 coworker Z 上 job_id Y 的"——审批平台不为 agent 运行建模。
- 无法做 Stop 级联：外部系统不知道 agent 回合已被中止。

**v1 否决。** 后续可以加一层适配器（既发出事件*又*创建一个本地审批请求来镜像外部状态），但核心模块必须自己拥有这一原语。

### 方案 D——策略驱动、DB 持久化、hook 拦截（选中方案）

- Postgres 中的声明式策略，索引为 `(mcp_server, tool_name, condition_expr, priority)`。
- 容器上的 `PreToolUse` hook 针对调用的参数评估策略。
- 命中 → 阻止调用，向 orchestrator 发布一个 auto-intercept IPC，orchestrator 创建一行待审批记录并通知审批人。
- 批准决策 → 发布 `approval.decided.<id>` → Worker 异步拾取，原子认领该行，POST 到凭据代理，将结果报告写回到发起会话。

**优点**

- 策略具有声明性，便于审计。
- 审批人面对的是一份刻意的摘要（在 agent 使用 `submit_proposal` 时带有理由），而非原始工具载荷。
- 解耦执行不会阻塞决策处理函数。
- 无策略时零影响。
- 通过 `submit_proposal` 可以做批量审批。

**缺点**

- 非平凡的状态机（10 种状态、审计行的 3 类参与者）。
- 需要容器在线以发布 auto-intercept（实践中不是问题——hook 只在容器运行期间触发）。

**选中。** 这是本文档其余部分所描述的形态。

---

## 每租户默认值

租户行携带两个设置，可在无需任何按策略配置的情况下塑造审批行为：

### `approval_default_mode`——当一个 proposal 没有匹配任何策略时会发生什么

| 取值 | 当无策略匹配时的行为 |
|---|---|
| `auto_execute`（默认） | 创建请求，立即发布 `approved`，Worker 无监督执行。遗留模式——为现有部署保留行为。 |
| `require_approval` | 以 `skipped` 创建请求；Worker 永远看不到它；起源端收到一条"无法继续"的消息。当"没有策略"应被视为"配置缺口而非白名单"时使用。 |
| `deny` | 以 `rejected` 创建请求并附系统说明；Worker 仅投递拒绝通知。默认拒绝的姿态。 |

通过 `update_tenant(tenant_id, approval_default_mode=...)` 或 REST `PATCH /api/admin/tenant` 进行配置。引擎在每次 proposal 时读取当前值，因此变更立即生效（无需重启）。

### `APPROVAL_FAIL_MODE`——容器启动时 DB 不可达

由 `container_executor` 在策略加载抛出异常时读取的环境变量。

| 取值 | DB 故障时的行为 |
|---|---|
| `closed`（默认） | 重新抛出异常；agent 容器不启动。策略故障**绝不允许**让每次工具调用悄无声息地无监督运行。 |
| `open` | 容器启动但不加载任何审批策略；记录一条警告。遗留行为；在故障期间租户更看重 agent 可用性而非审批覆盖率时可以接受。 |

---

## 两种入口路径

调用进入"待审批"请求有**两种**方式。两者最终都进入同一个引擎状态机。

### 路径 1：自动拦截（PreToolUse hook）

非主动发起。agent 决定调用 `mcp__erp__refund(amount=5000)`。hook 匹配到一条 `condition_expr={"field":"amount","op":">","value":1000}` 的策略，向 orchestrator 发布一条 `auto_approval_request` NATS 任务，并返回一个 `block` 裁决，其原因说明了发生了什么。agent 看到一个工具错误，通常会告诉用户"我先等审批"。

### 路径 2：主动 proposal（`submit_proposal` 工具）

agent 主动调用 `mcp__rolemesh__submit_proposal`，附带 `rationale` 和一批动作。这条路径适用于：

- 多个相关动作应一并审批（"为这次风暴中断给这 10 笔订单退款"）。
- agent 拥有审批人会想要的上下文（"为什么"）。
- 已有策略，但即使条件可能不匹配，agent 仍希望加入人工判断。

**没有匹配策略**的 proposal 仍然会被记入审计，但短路至 executed——即使一个 proposal 被无条件放行，审计轨迹也必须连续。

两条路径共享：

- 相同的引擎方法（`handle_proposal`、`handle_auto_intercept`）
- 相同的状态机
- 相同的审计形态

它们的区别在于：

- `source` 列（`proposal` vs `auto_intercept`）
- 去重行为（auto-intercept 在 5 分钟窗口内基于 `action_hash` 去重；proposal 从不去重——每次调用都是明确的）
- `created` 审计的 `actor_user_id`：proposal 记录发起用户；auto-intercept 为 NULL（系统转换）

---

## 架构

### 进程拓扑

```
       ┌──────────────────────────────────────────────────┐
       │              Agent Container                     │
       │                                                  │
       │  ApprovalHookHandler  (PreToolUse, fail-close)   │
       │      matches policy → publish auto_approval_… on │
       │      agent.<job>.tasks                           │
       │                                                  │
       │  submit_proposal tool → same NATS subject with   │
       │      {"type": "submit_proposal"}                 │
       └────────────────────────┬─────────────────────────┘
                                │ NATS agent-ipc
                                ▼
       ┌──────────────────────────────────────────────────┐
       │                 Orchestrator                     │
       │                                                  │
       │  process_task_ipc → _IpcDepsImpl.on_proposal     │
       │                   → _IpcDepsImpl.on_auto_intercept
       │                                                  │
       │  ApprovalEngine                                  │
       │    handle_proposal / handle_auto_intercept       │
       │      uses policy.py (same file as container)     │
       │      resolves approvers, creates row, audits,    │
       │      notifies via ChannelSender                  │
       │                                                  │
       │    handle_decision (REST entry)                  │
       │      atomic decide → audit → publish             │
       │      approval.decided.<id>                       │
       │                                                  │
       │    cancel_for_job / expire_stale / reconcile     │
       │                                                  │
       │  ApprovalWorker                                  │
       │    (durable JetStream consumer)                  │
       │      claims approved rows atomically             │
       │      POSTs each action to credential proxy       │
       │      writes audit + result, notifies channel     │
       │                                                  │
       │  run_approval_maintenance_loop (30 s cadence)    │
       │    expire_stale_requests + reconcile_stuck       │
       └────────────────────────┬─────────────────────────┘
                                │ HTTP
                                ▼
       ┌──────────────────────────────────────────────────┐
       │           Credential Proxy / Egress Gateway      │
       │   /mcp-proxy/<server>/   forwards to upstream    │
       │   with the user's IdP token injected             │
       └──────────────────────────────────────────────────┘
```

### 文件布局

```
src/agent_runner/
  approval/policy.py               # pure-function policy matcher (zero deps)
  hooks/handlers/approval.py       # ApprovalHookHandler (PreToolUse)
  tools/rolemesh_tools.py          # submit_proposal tool
  tools/context.py                 # ToolContext + user_id

src/rolemesh/
  approval/
    types.py                       # dataclasses mirroring the 3 tables
    engine.py                      # ApprovalEngine
    executor.py                    # ApprovalWorker (consumer + HTTP)
    notification.py                # target resolver + message formatters
    expiry.py                      # maintenance loop entry point
  ipc/
    nats_transport.py              # approval-ipc stream
    task_handler.py                # submit_proposal / auto_approval IPC routes
  db/
    approval.py                    # CRUD on approval_policies / requests / audit
    schema.py                      # DDL for the three approval tables
  main.py                          # wire engine + worker + maintenance
  agent/container_executor.py      # load policies into AgentInitData

src/webui/
  admin.py                         # REST: policies CRUD + approvals CRUD + decide
  schemas.py                       # Pydantic request/response models
  main.py                          # attach ApprovalEngine to admin module
```

---

## 状态机

```
                 ┌──────────┐
                 │ pending  │
                 └─┬─┬──┬─┬─┬──┐
       ┌───────────┘ │  │ │ │  └──────┐
       │             │  │ │ │         │
   approved      rejected │ │         │
       │             │    │ │         │
       │             │ expired        │
       │             │    │ cancelled  │
       │             │    │ │         │
       │             │    │ │   skipped (no approver found)
       │                                    ▲
       ▼                                    │
   executing ──► executed                   │
       │     └─► execution_failed           │
       └─► execution_stale  (maintenance    │
                             loop catches   │
                             hung executing)│
                                            │
   proposal with no matching policy ────────┘
   short-circuits: pending → approved → executed
   (no approver involvement; full audit trail)
```

### 终态状态

| 状态 | 来自 | 由谁触发 |
|---|---|---|
| `rejected` | pending | 审批人（原子 SQL） |
| `expired` | pending | 维护循环 |
| `cancelled` | pending | Stop 级联 |
| `skipped` | pending | 引擎，当 `resolve_approvers` 返回 `[]` |
| `executed` | executing | Worker，所有动作都成功 |
| `execution_failed` | executing | Worker，任意动作失败 |
| `execution_stale` | executing | 维护循环，超过 5 分钟宽限 |

### 关键不变量

1. **`pending → approved | rejected` 是原子的并且只赢一次。** SQL 是 `UPDATE … WHERE id = $1 AND status = 'pending' AND $user_id = ANY(resolved_approvers) RETURNING *`。两个并发审批人：恰好一位拿到返回行；另一位拿到 None，引擎将其转译为 `ConflictError` → HTTP 409。按同一规则，外部人也拿到 None；引擎通过读取该行来消歧：仍是 `pending` → `ForbiddenError` → HTTP 403。
2. **`approved → executing` 是原子的并且只赢一次。** 同一 CAS 模式。如果有两个 Worker 订阅（例如灰度发布期间），仅有一个执行。
3. **`pending → cancelled` 是被过滤的。** `cancel_pending_approvals_for_job` 只移动 `status = 'pending'` 的行。同一 job 中已批准的行不会被触碰——用户无法通过 Stop 取消批准。
4. **`resolved_approvers` 是一份快照。** 在创建时刻捕获。之后编辑策略的 `approver_user_ids` 不会扩大或缩小已经打开的请求的决策者范围。
5. **审计只增不改。** `db/approval.py` 暴露 `write_approval_audit` 和 `list_approval_audit`。API 表面没有 update 或 delete——若要新增其中任一，必须直接动 DB 模块。

---

## 单实现策略匹配器

拆分进程架构中一种常见的失败模式是：容器代码与 orchestrator 代码"理应"在匹配语义上保持一致，但因为是由不同 PR 维护的不同文件而发生漂移。结果是：hook 阻止了一次引擎认为不匹配的调用，反之亦然。

我们让容器和 orchestrator **导入同一个文件**，从而消除这类 bug：

```python
# src/agent_runner/hooks/handlers/approval.py (runs in container)
from agent_runner.approval.policy import compute_action_hash, find_matching_policy

# src/rolemesh/approval/engine.py (runs in orchestrator)
from agent_runner.approval.policy import (
    compute_action_hash,
    find_matching_policies_for_actions,
    find_matching_policy,
)
```

该模块是零依赖的标准库（没有 DB、没有 NATS、没有 `rolemesh` 包导入）。它有两项职责：

- `evaluate_condition(expr, params) -> bool`——条件 DSL 评估器。
- `find_matching_policy(policies, server, tool, params) -> dict | None`——由 hook 调用一次，由引擎在每个批量动作上各调用一次。

单实现这一不变量由一个 grep 级别的验收测试钉死：

```bash
grep -r "def find_matching_policy" src/ | wc -l
# must be 1
```

引擎仍然会在自己这一侧重新匹配一次——不是因为不信任 hook，而是因为在容器快照（job 启动时加载）和拦截（数秒或数分钟后触发）之间，策略集可能已经变化。引擎使用 `get_enabled_policies_for_coworker` 重新读取当前状态，若不再有策略匹配，则丢弃该请求。

### 条件 DSL

声明式、可 JSON 序列化，能干净地放入 `condition_expr JSONB` 列：

```json
{"always": true}

{"field": "amount", "op": ">", "value": 1000}

{"and": [
  {"field": "amount", "op": ">", "value": 100},
  {"field": "currency", "op": "==", "value": "USD"}
]}

{"or": [
  {"field": "amount", "op": ">", "value": 10000},
  {"field": "priority", "op": "==", "value": "critical"}
]}
```

支持的运算符：`==`、`!=`、`>`、`>=`、`<`、`<=`、`in`、`not_in`、`contains`。

失败模式语义（由 `test_approval_policy.py` 钉死）：

- 缺失字段 → 条件返回 `False`（不匹配）。调用方不会意外地以"字段是否存在"作为门槛。
- 类型不匹配（例如 `"100" > 50`）→ `False`，不会抛出 `TypeError`。配置错误的策略对 hook 层而言失败即拒（hook 层本身就是失败即拒，因此该调用仍会被阻止——双重保险）。
- 未知运算符 / 未知表达式形状 → `False`。
- 空 `{"and": []}` / `{"or": []}` → `False`。空连接子几乎总是配置错误；我们选择了比 Python 的 `all([]) == True` 更安全的解释。

### 为什么不用 Python `eval` / CEL / JSONPath

- `eval`：明显的注入风险。
- CEL / JSONPath：完整的表达式语言；对于我们需要的形态过于重型。为一项功能引入依赖会强迫审查者去学习它。如果用例溢出 DSL，我们可以迁移——表面很小且可替换。

---

## 身份与幂等性

### 审批请求上的四个标识

每行携带多个看起来相似但含义不同的 ID。混用它们正是 schema 故意要钉住的一类 bug：

| 字段 | 含义 | 可空？ | 用途 |
|---|---|---|---|
| `policy_id` | 哪条规则匹配（或无匹配 proposal 的兜底占位） | 否 | 审计 / 管理 UI |
| `user_id` | agent 当时正在执行其回合的用户 | 否 | 传给 MCP 的 `X-RoleMesh-User-Id`、起源会话查找 |
| `resolved_approvers[]` | 被允许点 Approve/Reject 的用户 | 否（可能为空 → skipped） | `POST /decide` 上的授权 |
| `actor_user_id`（审计行） | 引发此特定转换的人 | 是（系统转换为 NULL） | 取证 |

PR 中的规格文本记录了这些规则，但测试把它们钉得更紧。如果你不小心把某次转换的 `actor_user_id` 从 NULL 改成了一个用户 ID，`test_auto_intercept_created_audit_has_null_actor` 会失败。

### Action hash：一个字段，两个职责

`action_hashes[]` 是与 `actions[]` 平行的数组。每个元素是规范化 JSON `{"tool": tool_name, "params": params}` 在 `sort_keys=True` 下的 SHA-256。它做两件事：

1. **MCP 幂等性上下文。** 在凭据代理调用上发送（参见下文"凭据代理集成"）。
2. **自动拦截去重。** `find_pending_request_by_action_hash` 按租户 + action_hash + 5 分钟窗口查询待审批行。防止 agent 在紧密循环中重试被阻止的调用时，hook 创建出 50 条待审批请求。

跨键序的确定性是一个必需的不变量：

```python
a = compute_action_hash("refund", {"amount": 100, "currency": "USD"})
b = compute_action_hash("refund", {"currency": "USD", "amount": 100})
assert a == b
```

另外：显式 `None` 与缺失字段不能碰撞（`{"amount": None}` 与 `{}` 必须产生不同的哈希）。否则一个 agent 在新增一个 null 参数后，会悄无声息地复用一条早已存在的审批。

---

## 通知流程

通知与状态转换有意被设计为分离的关注点。`ApprovalEngine` 通过注入的 `ChannelSender` 协议调用 `channel_sender.send_to_conversation`；它不知道 channel 网关、Telegram、Slack 或 WebUI 的存在。

目前只有两个进程持有网关句柄：

- orchestrator 进程：使用真正的 `_OrchestratorChannelSender`，通过 `conversations` 表和 coworker 状态缓存把 `conversation_id → (binding_id, chat_id)` 映射起来。
- WebUI 进程：使用空操作的 `_WebuiNoopChannel`。WebUI 的 REST decide 端点向 NATS 发布；orchestrator 的 `ApprovalWorker` 接收到 decided 事件，并负责该决策所隐含的任何通知。

这就是为什么引擎对**批准和拒绝两种决策**都发布 `approval.decided.<id>`（参见 `handle_decision`）。早期草案仅在批准时发布，并通过直接调用 `channel_sender.send_to_conversation` 发送拒绝消息；当 REST 处理函数运行在没有网关的进程里时，这种做法行不通。

### 目标解析

`NotificationTargetResolver.resolve_for_approvers` 走以下链路：

1. 如果设置了 `policy.notify_conversation_id` 且该会话仍存在，则使用它。
2. 每位审批人与该 coworker 已有的会话——把通知出现在他们日常工作的 channel 中。
3. 起源会话，作为最后兜底。

在 v1 中，`cancel` / `expire` / `reject` 通知仅发往起源会话，并发送新消息，而不是编辑前一条消息。编辑 Telegram / Slack 消息以及推送 WebSocket 状态更新是未来工作；规格刻意将其推后以缩小范围。

---

## 凭据代理集成

Worker 不直接调用 MCP server，而是访问凭据代理的 `/mcp-proxy/<server_name>/`。代理注入用户的 IdP token 并转发至实际的 MCP server；用户身份通过 `X-RoleMesh-User-Id` header 携带，代理在转发前会将其剥离。完整机制——TokenVault、`auth_mode`（`user` / `service` / `both`）、以及为何剥离 user-id header——见 [`6-auth-architecture.md`](6-auth-architecture.md) 和 [`7-external-mcp-architecture.md`](7-external-mcp-architecture.md)。

审批特定的契约是**幂等键**：

```
X-Idempotency-Key: <request_id>:<action_index>
```

这里**故意不**使用 `action_hash`（`sha256(tool, params)`）。两个租户用相同参数调用同一工具会产出按位相同的哈希，一个尊重幂等性的 MCP server 会把租户 A 的缓存响应返回给租户 B——形成跨租户的数据泄露。`<request_id>:<action_index>` 形式按审批请求（UUID）唯一，因此按租户、按执行也唯一。`action_hash` 保留它的另一个角色（引擎内部的 auto-intercept 去重键），其消费者是引擎自身——没有跨租户暴露。

### 顺序的、尽力而为的批量执行

每个动作一次 JSON-RPC 调用：

```json
{
  "jsonrpc": "2.0",
  "id": <i+1>,
  "method": "tools/call",
  "params": {"name": "<tool>", "arguments": <params>}
}
```

某个动作失败**不会**让批量短路。每个动作的结果都被记录在 `audit.metadata.results[i]` 中。最终批量状态：若所有动作都成功则为 `executed`；否则为 `execution_failed`。

每动作错误被分为三类——HTTP 传输失败、JSON-RPC 应用错误（HTTP 200 且 `error` 被设置）、aiohttp 异常（超时 / 连接复位）。早期 Worker 版本将应用错误误判为成功；`tests/approval/e2e/test_e2e_mcp_application_error.py` 钉住了修正后的行为。

**为什么不并行？** MCP server 经常带有副作用（执行退款、写入账本）。并行执行会丢失顺序——审计日志的消费者无法重建哪个副作用先发生。大多数批量是 1-5 个动作；串行很少能带来延迟上的收益。

---

## 崩溃恢复与协调

维护循环处理三种失败模式：

### 1. Worker 错过了 `approval.decided.<id>` 的发布

该行永远停留在 `approved`。症状：审批人点了按钮，UI 变绿，但什么都没执行。检测：`list_stuck_approved_approvals(older_than_seconds=60)`。处置：重新发布 NATS 事件。Worker 的原子认领保证了重复投递是安全的——第一个落地的重发是唯一会进行转换的那一个。

### 2. Worker 在认领后、完成前崩溃

该行永远停留在 `executing`。检测：`list_stuck_executing_approvals(older_than_seconds=300)`——5 分钟是宽限期。处置：转入 `execution_stale`，向起源会话发送一条保守的"可能已部分执行"警告，需要人工介入排查。我们**不**自动重试：半完成的批量是危险的（一笔已经入账的退款，被盲目重试会重复发放）。

v1 不持久化每动作的进度。该通知刻意保持简短，因为我们无法区分"完成动作 0，在动作 1 上崩溃"与"任何动作落地前就崩溃"。如果批量取证变得关键，自然的扩展是在 `approval_requests` 上增加 `execution_progress JSONB` 列以及 Worker 中按动作的 append 调用——被标记为未来工作，不作投机性建设。

### 3. 容器在持有 pending 审批时崩溃

该行保持 `pending` 直到其 `expires_at` 截止时间。过期循环（`engine.expire_stale_requests`）会捕获它，转入 `expired`，并通知起源会话。使用带 CAS 守护的 SQL（`expire_approval_if_pending`）确保并发 decide 不会被踩——若审批人恰在截止时刻点击了 Approve，两条 UPDATE 中只有一条会胜出。

### 为什么是一个合并循环而非两个

过期与协调按相同节奏运行（30 s）。把它们捆绑成 `run_approval_maintenance_loop` 意味着一个后台任务、一次共享 DB 池获取周期，以及关闭时的单一取消点。如果将来节奏发散，再拆即可。

---

## Stop 级联

当用户点击 Stop 时，后端发出 `StoppedEvent`，agent 容器仍存活以便后续跟进（完整的 stop 语义见 [`11-steering-architecture.md`](11-steering-architecture.md)）。审批级联挂入该信号：NATS 桥在每次 `StoppedEvent` 上以"发送即忘"的方式发布 `approval.cancel_for_job.<job_id>`。orchestrator（在 `approval-ipc` 上 `durable="orch-approval-cancel"`）调用 `engine.cancel_for_job(job_id)`，它会：

1. 将该 job 的 pending 行移到 `cancelled`（UPDATE 过滤 `status='pending'`）。
2. 为每个被取消的请求写入一条 `cancelled` 审计记录。
3. 向每个被取消请求的起源会话发送一条取消通知。

发布之所以是**发送即忘**，因为：

- 容器不知道审批模块是否被部署。在零策略租户中没有 pending 行，也没人会对该发布作出反应；将其视为硬依赖会把每次 Stop 与审批的可用性耦合起来。
- 在较旧的 orchestrator 上可能不存在 `approval-ipc` 流。`try/except` 包装让 Stop 在任何情况下都能工作。

---

## REST API

`src/webui/admin.py`：

```
GET    /api/admin/approval-policies        ?coworker_id&enabled
POST   /api/admin/approval-policies
GET    /api/admin/approval-policies/{id}
PATCH  /api/admin/approval-policies/{id}
DELETE /api/admin/approval-policies/{id}

GET    /api/admin/approvals                ?status&coworker_id
GET    /api/admin/approvals/{id}           (includes audit_log)
GET    /api/admin/approvals/{id}/audit-log
POST   /api/admin/approvals/{id}/decide    {action, note?}
```

### 状态码契约

| 场景 | 状态码 | 原因 |
|---|---|---|
| 被授权的审批人对 pending 请求做出决策 | 200 | happy path |
| 不在 `resolved_approvers` 中的用户做决策 | 403 | `ForbiddenError` |
| 对已结案的请求做决策 | 409 | `ConflictError` |
| 对另一个租户的请求做决策 | 404 | 不泄露跨租户信息 |
| 引擎未接入时做决策 | 503 | 部署配置错误 |
| 对不存在的请求做决策 | 404 | — |

503 之所以刻意优先于"成功"或"500"：decide 是控制面操作，管理员需要知道他们的部署缺少引擎，而非去排查为什么后台 worker 从未触发。

---

## 数据库 schema

### `approval_policies`

声明式规则。索引为 `(tenant_id, coworker_id, mcp_server_name, tool_name)`。`coworker_id = NULL` 表示"租户范围"。`condition_expr JSONB` 持有 DSL。在 `enabled=TRUE` 上有部分索引，因为被禁用的行从不参与匹配。

### `approval_requests`

所有进行中和历史的请求。关键列：

- `status TEXT CHECK (status IN (...))`——状态机的取值域在 `APPROVAL_STATUSES` 中被镜像。更改取值域需要同时更新约束和 Python 集合；有一个 schema 合理性测试会比对二者。
- `action_hashes TEXT[]`——与 `actions JSONB` 数组平行。
- `resolved_approvers UUID[]`——可决策者的快照。
- 在 `(status, ...)` 上的五个部分索引覆盖热点查询（pending + expired + approved + executing + 按 job 的 pending）。

### `approval_audit_log`

只增不改。`actor_user_id UUID REFERENCES users(id)` 可空，因为系统转换没有 actor。`metadata JSONB` 自由格式；Worker 在终态转换时把 `{"results": [...]}` 塞进这里。

DDL 位于 `src/rolemesh/db/schema.py` 的 `_create_schema()`。审批表与现有表一起使用 `CREATE TABLE IF NOT EXISTS` 添加，因此全新的数据库在首次启动时会自动建好。租户范围的表绑定了 RLS——参见 [`4-multi-tenant-architecture.md`](4-multi-tenant-architecture.md)。

---

## 零影响保证

非审批基线测试数量（在模块落地前统计）必须在模块合并后仍然全部通过。提供这一保证的属性：

1. 当 `get_enabled_policies_for_coworker` 返回空列表时，`container_executor.py` 中 `AgentInitData.approval_policies = None`。该字段在通信协议中可空。
2. 仅当 `init.approval_policies` 为真时，容器的 `main.py` 才注册 `ApprovalHookHandler`。空列表 → 无 handler。
3. 没有 handler 时，`PreToolUse` 的 hook 链不变。
4. `submit_proposal` 在 `TOOL_DEFINITIONS` 中，所以 agent 在每次运行中都能看到它——但没有策略时，agent 没有理由调用它；在无匹配策略下调用它，会创建一条仅审计的记录然后继续。
5. orchestrator 始终运行 `ApprovalEngine` 和 `ApprovalWorker`。两者在启动时订阅 NATS 流。在零策略租户中这些流什么都收不到；订阅本身开销很小。
6. 30 秒的维护循环查询 `list_expired_pending_approvals`、`list_stuck_approved_approvals`、`list_stuck_executing_approvals`。是对空表的三次有索引的 `WHERE status = '...'` 扫描。对热路径无影响。

整套现有测试套件原样通过；这就是该保证的可执行形式。

---

## 测试策略

该模块遵循项目级的测试理念（对抗式、最少 mock、当真实依赖代价低时优先集成测试）。

- **纯函数匹配器**（`test_approval_policy.py`）——变异思维。对每个运算符的每个边界、每种失败模式（缺失字段、类型不匹配、空连接子）、跨键序的哈希确定性、显式 None 的哈希不碰撞。把源代码中的 `<` 改成 `<=` 会至少导致一个测试失败。
- **DB CRUD**（`test_db.py`，通过 testcontainers 使用真实 Postgres）。竞争安全：两个审批人并发决策、两个工作进程并发认领、cancel-for-job 不触碰已批准行。Schema 合理性：`APPROVAL_STATUSES` 集合与 CHECK 约束的取值域一致。
- **引擎**（`test_engine.py`，真实 Postgres + 假发布者/channel/resolver）——端到端的状态机。每条审计行的 `actor_user_id` 由断言钉死，因此把"proposal 创建者"从用户改成 NULL（或反过来）的重构会失败。
- **Worker**（`test_executor.py`，真实 Postgres + 充当凭据代理的 aiohttp 测试服务器）——执行流程、部分失败、重投递下的去重、无 conversation ID 的拒绝路径。
- **REST API**（`test_api.py`，真实 Postgres + 通过 `ASGITransport` 的 httpx `AsyncClient`）——状态码契约、跨租户 404、无引擎的 decide → 503。
- **中止级联**（`test_abort_cascade.py`）——pending 被取消，approved 被保留。
- **Hook handler**（`test_approval_handler.py`）——非 MCP 工具和内置 rolemesh 工具的透传规则、畸形 MCP 名称不会崩溃、发布的 NATS 载荷携带身份。
- **跨后端一致性**（`test_approval_parity.py`）——无论通过 Claude 桥还是 Pi 桥接入，同一个 `ApprovalHookHandler` 产生相同的 block 裁决和 NATS 发布。

所有测试位于 `tests/approval/`（DB 支持 + API）和 `tests/test_agent_runner/`（容器侧、无 DB）。`tests/ipc/` 持有 IPC 派发器的路由测试。

---

## 已知缺口与未来工作

- **审批人 UI。** REST 端点已存在；用于审核 / 决策审批的 WebUI 前端在本轮并未实现。当前 UX：审批人在自己的 coworker 聊天中收到通知，点击链接到 `${WEBUI_BASE_URL}/approvals/<id>`，但该页面尚未渲染。
- **丰富的策略模板。** DSL 接受任意条件，但对常见用例（"退款 > \$1k"、"任何生产写入"）没有模板库。管理员需要手写原始 JSON。
- **MCP 工具参数自省。** 管理 UI 在编写 `condition_expr` 时无法建议有效的字段名；管理员必须从 MCP server 的文档中了解其工具 schema。
- **多步 / 多审批人工作流。** v1 每个请求只有一位审批人。"需 2/3 同意"或"审批人链"会需要 schema 改动（每审批人决策表）。
- **容器层的策略热加载。** 策略在容器启动时被加载到 `AgentInitData`。某次回合中作的编辑要等到下一轮才生效。引擎在自己这一侧使用实时数据重新匹配，所以被禁用的策略会在回合中途被尊重；但*新*策略在容器重启之前不会开始拦截。
- **DB 故障时的静默失败即拒。** 当 `APPROVAL_FAIL_MODE=closed`（默认）且 orchestrator 在容器启动时无法加载租户的策略快照时，agent 的产生会被拒绝。该拒绝以 `ERROR` 级别带结构化字段记录下来，但**没有主动推送**给租户拥有者 / 管理员——没有邮件、没有聊天内消息、没有 metric 计数、没有 health-check 端点。用户只看到"agent 无响应"；管理员必须实时跟踪日志，或者已经接好外部日志告警，才能察觉。现实失败模式：PG 故障切换（20-30 s）、应用用户权限被吊销、连接池耗尽。对自托管 / 单团队部署而言属轻微；对多租户 SaaS 而言会变成运维上重要的问题。若/当 RoleMesh 朝 SaaS 方向走，正确的修法是一个专门的 health 端点 + 自动在租户拥有者的会话中投递"审批子系统不健康"消息，和/或递增一个 Prometheus 计数器。被记录为 v1 的有意省略，而非 bug。
- **Stop → proposal 竞争留下孤儿 pending 行。** NATS 不保证跨 subject 的有序性，因此 `approval.cancel_for_job.J` 可能在 `submit_proposal` 通过 `agent.J.tasks` 抵达之前到达。`cancel_for_job` 找不到可取消的；随后到来的 proposal 会创建一行其 agent 回合已被停止的 pending 记录。该行会被常规过期循环回收（每条策略都有 `auto_expire_minutes`，默认 60 分钟），审批人通常会注意到那条过时通知。我们没有加 `cancelled_jobs` 跟踪表，因为现实危害（一个孤儿请求等待最多一小时，期间审批人可能会注意到它已过时）被判断为小于"第二张状态表 + 在每条创建路径上加检查"的成本。`tests/approval/e2e/test_e2e_race_stop_vs_proposal.py` 记录了当前行为以及过期路径的回收。若自动审批人进入视野，重新审视该问题。
- **执行重试。** 在 v1 中 `execution_stale` 是终态。未来迭代可以加一个管理员"重试"端点，它会重新发布 `approval.decided.<id>`——得益于 action-hash 幂等性上下文是安全的，但前提是 MCP server 尊重它。
- **取消 / 过期通知的编辑。** v1 发送新消息；为这些转换做编辑或 WebSocket 状态推送被推迟。
- **REST 分页。** 列表端点上限为 100 条；尚不支持游标。

---

## 添加一条策略的快速参考

```
POST /api/admin/approval-policies
Content-Type: application/json
Authorization: Bearer <admin-token>

{
  "mcp_server_name": "erp",
  "tool_name": "refund",
  "coworker_id": "<coworker-uuid>",            // or omit for tenant-wide
  "condition_expr": {
    "and": [
      {"field": "amount", "op": ">", "value": 1000},
      {"field": "currency", "op": "==", "value": "USD"}
    ]
  },
  "approver_user_ids": ["<user-uuid-1>", "<user-uuid-2>"],
  "auto_expire_minutes": 60,
  "post_exec_mode": "report",                  // v1 only accepts "report"
  "priority": 10,
  "enabled": true
}
```

该策略在下一次 agent 运行时生效（策略在 job 启动时通过 `get_enabled_policies_for_coworker` 加载）。orchestrator 侧引擎在每次拦截时也会重新读取策略，因此禁用一条策略对运行中的容器会立即生效。

---

## 相关文档

- [`6-auth-architecture.md`](6-auth-architecture.md)——`AgentPermissions`（"agent 究竟能不能做这件事"的关口）、`TokenVault`、OIDC 集成
- [`7-external-mcp-architecture.md`](7-external-mcp-architecture.md)——凭据代理机制、`auth_mode`、`/mcp-proxy/<server>/` 路由
- [`9-hooks-architecture.md`](9-hooks-architecture.md)——`PreToolUse` hook 契约、失败即拒纪律
- [`11-steering-architecture.md`](11-steering-architecture.md)——触发审批取消级联的 Stop 信号
- [`4-multi-tenant-architecture.md`](4-multi-tenant-architecture.md)——审批表上的 RLS 规则
