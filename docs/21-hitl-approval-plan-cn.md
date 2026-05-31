# HITL 工具审批 — 实现计划与冻结契约（中文版）

> **开始任何 session 前请先完整阅读本文件。** 这是实现该 feature 的每一个
> Claude Code session 的共享参考。§3–§5 的冻结契约是让各 session 能独立推进的硬
> 接口 —— 未先更新本文件，不得偏离它。
>
> 本文是 `docs/21-hitl-approval-plan.md` 的中文译版。按 repo 惯例，下列术语保留
> 英文原词：**agent** / **subagent** / **Coworker** / **Orchestrator** /
> **hook** / **skill**。代码标识符、表名、字段名、NATS subject、配置项一律保留
> 英文原样。

## 0. 背景 —— 为什么推倒重来，为什么选 block-and-await

此前存在一套人工审批子系统（`feat/approval` → `feat/self-approval` v6.1），已于
2026-05-31 **整体删除**（PR #35，`feat/remove-approval`，约 25.8k 行）。删除的两
个原因：

1. **太重** —— 多审批人 fallback、职责分离（separation-of-duties）、一个把动作
   重新 POST 到 credential proxy 的 worker + executor、动作重放（action-replay）
   幂等、一个审计日志触发器。
2. **脱离 agent 的 ReAct 循环** —— 那个 hook **立即**返回 `block=True`，结束这一
   turn，由一个带外（out-of-band）worker 稍后重新执行动作。agent 从未在自己的推理
   循环内看到工具结果。

本次重新设计修复了根因。hook **就地阻塞**（最多 `await` 决策 `APPROVAL_TIMEOUT`）；
批准后返回 `None`，工具就在**同一个 container、同一个 turn** 内执行，于是 agent
拿到真实结果，正常继续它的 ReAct 循环。没有 worker、没有 executor、没有动作重放。

这笔交易换来的代价：等待期间一个 container 被**占用**至多 `APPROVAL_TIMEOUT`。这
个成本由 §8 的 idle 回收 / 超时 / 重启恢复机制承担（也是本 feature 最难的部分）。

**从零构建。不要恢复或拷贝任何被删除的审批子系统的代码**（它在 git 历史里，忽略
它）。把那些小而纯的部分重写一遍，能让架构保持干净、不沾染旧模型的假设。

## 1. 已锁定的决策

| 决策 | 取值 | 理由 |
|---|---|---|
| `APPROVAL_TIMEOUT` 默认值 | **300_000 ms（5 分钟）** | 审批人就是创建者（self-approval），秒级到分钟级即可解决。短超时把 container 占用成本降约 4×，且与 container watchdog 下限留有充足余量。 |
| Safety `require_approval` 边界 | **保持硬阻塞；不进入 HITL** | safety 裁决 `require_approval` 在 main 上已是 block 的别名。HITL 只服务 **MCP policy** 匹配。在代码/文档里显式声明这点，免得用户被两种 gate 类型搞混。 |

## 2. Scope 红线（对整个实现锁定 —— 不得扩张）

- 仅 MCP 工具（`mcp__*` 前缀）。其余一律立即返回 `None`（放行）。
- 仅 tenant 级 policy —— policy 上没有 coworker 维度。
- 仅结构化比较条件（无表达式语言 / 无 eval）。
- 无独立的审计日志表 —— 决策数据存在 `approval_requests` 行上。
- **不要**改 GroupQueue 的 key 模型 —— 复用既有 key 规则（§5）。
- **不要**改 Pi 内核 —— 用既有的 hook 桥接（§6 锚点）。
- 审批人 = 任务创建者（self-approval）。这是**防 agent 犯错的护栏**，不是授权 /
  职责分离控制。明说这点。

## 3. 冻结契约 —— IPC（NATS subjects）

所有审批流量都经由 Orchestrator 中转；container 从不直接和用户对话。三个 subject。
收到时**必须丢弃未知字段**（为滚动升级保留前向兼容）。

### 3.1 `agent.{job_id}.approval_request` — container → Orchestrator
```json
{
  "request_id": "uuid",
  "tenant_id": "uuid",
  "coworker_id": "uuid",
  "conversation_id": "uuid | null",
  "user_id": "uuid | null",          // 审批人 = 创建者；null => fail-closed 阻塞
  "job_id": "string",
  "policy_id": "uuid | null",
  "mcp_server_name": "string",
  "tool_name": "string",
  "params": { },                      // 工具调用参数
  "action_summary": "string",         // 给卡片用的简短可读摘要
  "requested_at": "iso8601",
  "expires_at": "iso8601"             // requested_at + APPROVAL_TIMEOUT
}
```

### 3.2 `agent.{job_id}.approval_decision` — Orchestrator → container
```json
{
  "request_id": "uuid",
  "decision": "approve | reject",
  "decided_by": "uuid",
  "note": "string | null"
}
```

### 3.3 `agent.{job_id}.approval_cancel` — container → Orchestrator
```json
{ "request_id": "uuid" }
```
从 container 的 `finally` 发出（幂等）。覆盖 reject / 超时 / 用户 Stop
（CancelledError）/ 异常 —— 任何 container 知道"这一轮结束了"的路径。

## 4. 冻结契约 —— DB schema

使用**真正的** RLS 模式（不是 "dual-pool" —— 这个名字在本代码库不存在）：对四种
DML 操作都用单一谓词 `tenant_id = current_tenant_id()`，角色为 `rolemesh_app`
（NOBYPASSRLS）/ `rolemesh_system`（BYPASSRLS）。模板见
`src/rolemesh/db/schema.py:1244`（角色）与 `:1399`（SELECT/INSERT/UPDATE/DELETE
policy 元组）。

### 4.1 `approval_policies`
```
id               uuid PK
tenant_id        uuid NOT NULL
mcp_server_name  text NOT NULL
tool_name        text NOT NULL           -- 精确名或 "*"
condition_expr   jsonb NOT NULL          -- 见 §7
enabled          bool NOT NULL DEFAULT true
priority         int  NOT NULL DEFAULT 0
created_at       timestamptz NOT NULL DEFAULT now()
updated_at       timestamptz NOT NULL DEFAULT now()
-- 索引: (tenant_id, enabled), (tenant_id, mcp_server_name, tool_name)
-- RLS: tenant_id = current_tenant_id()
```

### 4.2 `approval_requests`
```
id               uuid PK
tenant_id        uuid NOT NULL
coworker_id      uuid NOT NULL
conversation_id  uuid NULL
policy_id        uuid NULL
user_id          uuid NULL               -- 审批人 = 创建者；null => fail-closed
job_id           text NOT NULL
mcp_server_name  text NOT NULL
action           jsonb NOT NULL          -- { tool_name, params }
action_summary   text
status           text NOT NULL           -- pending|approved|rejected|expired|cancelled
decided_by       uuid NULL
note             text NULL
requested_at     timestamptz NOT NULL DEFAULT now()
expires_at       timestamptz NOT NULL
decided_at       timestamptz NULL
-- 索引: 在 (status) 上建 WHERE status='pending' 的偏索引; (job_id)
-- RLS: tenant_id = current_tenant_id()
-- DB 是权威源；内存里的 suspend state 只是缓存（见 §8 重启恢复）。
```

没有 `approval_audit_log` 表，没有 `resolved_approver_user_ids`（self-approval ⇒
审批人就是 `user_id`），没有 `action_hashes`（不做重放）。

## 5. 冻结契约 —— 配置与不变量

```
APPROVAL_TIMEOUT      core/config.py    300_000 ms（5 分钟）   container await 与 DB expires_at 共用此值
启动断言               core/config.py    APPROVAL_TIMEOUT < IDLE_TIMEOUT + 30_000   否则拒绝启动
```
在 `IDLE_TIMEOUT = 1_800_000 ms` 与 container watchdog 下限
`max(config_timeout, IDLE_TIMEOUT + 30_000)`（`container_executor.py:402`）下，该
断言保证审批 await 总在 container watchdog 之前触发 —— 于是 watchdog 永远不会抢先
打断一次审批。

**Queue key 规则**（复用，别另造）：`conversation_id or coworker_id`。既有实现：
`src/rolemesh/orchestration/task_scheduler.py:99-115`（`_compute_queue_key`）；
messaging 侧在 `main.py:792` 以 `conv.id` 为 key。container 和它的审批 suspend
state 必须落在同一个 `_GroupState` 条目上，所以用这条确切规则。

## 6. 已核验的代码锚点（已对照 main 确认 —— 可信赖）

这些是读代码核验过的；你不必重新发现。

| 关注点 | 位置 | 备注 |
|---|---|---|
| 统一 hook 接口 | `src/agent_runner/hooks/registry.py:46` `on_pre_tool_use` | async；返回 `ToolCallVerdict \| None` |
| Verdict 类型 | `src/agent_runner/hooks/events.py:35` `ToolCallVerdict(block, reason, modified_input)` | `None`=放行；`block=True`=拒绝 |
| Claude 接线 | `claude_backend.py:446` permission_mode=bypassPermissions；`:448` hooks；`:171` callback | hook 不受 permission_mode 影响仍会触发 |
| Pi 桥接 | `pi_backend.py:142-175` `handle_tool_call`；`:234` 注册 | block 裁决阻止执行；`modified_input` 在 `:166` 被丢弃（我们不需要它） |
| Idle 定时器 | `main.py:845-855` `_reset_idle_timer`（TimerHandle）；`core/config.py:43` IDLE_TIMEOUT=1_800_000 | |
| Progress 早返回 | `main.py:868-882`（"别碰 idle 定时器或 notify_idle"） | 状态心跳**不会**重置 idle —— 必须用显式 suspend |
| 回收路径 A | `main.py:854` idle 定时器 → `request_shutdown` | 独立 TimerHandle，不受 idle_waiting 门控 |
| 回收路径 B | `scheduler.py:262-267` `notify_idle` → 若 pending | 受 `idle_waiting` 门控 |
| 回收路径 C | `scheduler.py:196-205` `enqueue_task` → active && idle_waiting | 受 `idle_waiting` 门控（第 202 行） |
| `idle_waiting` 标志 | `_GroupState` `scheduler.py:44`；在 `:274/:379/:414` 置 False | |
| Container watchdog | `container_executor.py:399-402` `max(config_timeout, IDLE_TIMEOUT+30_000)` | |
| Sessions / resume | `db/schema.py:690` 表；`db/chat.py:483-510` 按 conversation_id get/set_session | 支持 "继续 → 重试工具 → 再触发 hook → 新审批" |
| RLS 模板 | `db/schema.py:1244` 角色，`:1399` policy 元组 | 单谓词 tenant 模式 |
| NATS 输入 | `agent_runner/main.py:371` KV "agent-init"（初始）；`:373` `agent.{job_id}.input` 订阅；`:405-407` 立即 ack | 无自定义 ack_wait → 阻塞期间无重投。**不要在被阻塞 container 依赖的任何订阅上把 ack_wait 设得小于 APPROVAL_TIMEOUT。** |
| v1 WS frame | `webui/schemas_v1.py:835-859` `WsClientFrameModel` 联合；`webui/v1/ws_stream.py` 分发 | 扩展 = 新 pydantic 成员 + 联合 + ws_stream 分支 + 发布 NATS + OpenAPI 重生成 |
| Telegram | `channels/telegram_gateway.py`（尚无 inline keyboard / CallbackQueryHandler —— 全新构建）；`db/chat.py:138` bot-token 的 binding | callback_data "apr:{uuid}"/"rej:{uuid}" ≈ 40B，在 Telegram 的 64B 上限内 |

### 已核验的并发模型（对 §8 很重要）
- `HookRegistry.emit_pre_tool_use` **串行**迭代 handler（`registry.py`）。我们只注
  册一个审批 handler，所以没问题。
- 但一个 turn 内的多个 `ToolUseBlock` 会被两个 backend **并发**分发（Claude SDK
  并行工具调用；Pi 对批次做 `asyncio.gather`）。所以**一个 turn 内可能同时有多个
  审批在 pending** → suspend state 必须是 `set[request_id]`，绝不能是 bool。

## 7. Policy 条件语言（纯函数，fail-closed）

`evaluate_condition(expr: dict, params: dict) -> bool`，位于
`src/agent_runner/approval/policy.py`。零外部依赖。container hook 和 Orchestrator
都用它。

```
{"always": true}
{"field": "amount", "op": ">", "value": 100}
{"and": [ ... ]}    {"or": [ ... ]}
```
运算符：`== != > >= < <= in not_in contains`。

**Fail-closed**：缺字段 / 类型不匹配 / 表达式畸形 / 任何异常 ⇒ 返回为"需要审批"
（即按匹配处理）。一个无法求值的 gate，必须倾向于询问人类。

匹配（`find_matching_policy`）：取该 tenant 所有 `enabled` 的 policy；匹配
`mcp_server_name` 且（`tool_name == "*"` 或精确相等）且条件为真；多个匹配 → 取
`priority` 最高，平手 → 取最新的 `updated_at`。

## 8. 最难的部分 —— idle suspend / resume / 重启恢复

状态心跳不起作用（progress 输出在 `main.py:873` 早返回，不碰 idle 定时器）。一次
合法、有界（≤5 分钟）的审批等待必须**显式 suspend** 回收，而不是伪造存活。

回收有**三**条路径（§6 A/B/C）。suspend 必须把三条都关掉。

### Suspend（Orchestrator 收到 `approval_request`）
1. 持久化 `pending` 行 + `expires_at`。
2. `idle_handle.cancel()`（关掉路径 A）。
3. 强制 `state.idle_waiting = False` + 断言（关掉路径 B/C —— 不要依赖隐式不变量）。
4. `awaiting_approval[key].add(request_id)` —— **是 `set`，不是 bool**（并发多审
   批；若在第二个还 pending 时因第一个决策就清空，会重新武装一整个 IDLE_TIMEOUT
   并误杀第二个）。
5. 给 UI 发一条 "⏳ 等待审批" 状态（与卡片并列）。
6. suspend 期间：任何路径（包括新的后续消息）都不得重新武装 idle；新消息照常入队，
   但不得重置定时器。

`key` = §5 的 queue key。`awaiting_approval` 是按它做 key 的共享 dict；**不要**为它
重构 GroupQueue 的 key 模型。

### Resume（Orchestrator 收到 `approval_decision` 或 `approval_cancel`）
1. `awaiting_approval[key].discard(request_id)`。
2. **当且仅当 set 现在空了** → 从现在起重新武装一整个 `IDLE_TIMEOUT`。
3. 若是决策：转发给 container（`approval_decision` subject）；hook 解除阻塞，正常
   流程恢复。

### 过期 watcher
container 被 SIGKILL 的兜底（container 的 `finally` 没机会运行）：Orchestrator 在
`expires_at` 把行置为过期，并触发硬通道（hard-channel）通知。

### 重启恢复（必须完整 —— `_groups` 是内存态，重启即丢）
处于审批等待的 container 会**挺过** Orchestrator 重启，所以恢复不只是重新加载行。
启动时扫描 `approval_requests WHERE status='pending'`，对每一行：
- **未过期** → 为该存活 container 重建**完整的** suspend state：重建它的
  `_GroupState` 条目，**重放 suspend 动作**（cancel idle_handle、强制
  `idle_waiting=False`、`awaiting_approval[key].add`），重新建立
  `approval_decision` 路由（subject 可从 `job_id` 推出），重建过期 watcher。如果
  只重新加载行却不重建 `_GroupState` + suspend，被恢复的 container 会被立刻回收。
- **已过期** → 标记 `expired` + 触发硬通道通知。

### 三层清理（正交，优雅降级）
1. 正常：`approval_decision`（approve/reject）。
2. container-end 兜底：来自 container `finally` 的确定性 `approval_cancel`（覆盖
   Stop / abort / 异常 / 超时）。
3. container-SIGKILL 兜底（finally 没运行）：Orchestrator 过期 watcher + 重启恢复。

### 决策竞态 / 幂等
迟到点击 vs 超时再批准：container 侧 Future 先到先得；Orchestrator 侧行级 `status`
转移幂等。两侧收敛一致。

## 9. 跨 session 追踪的已知风险

- **R1（S2，MVP 必须回答）：** 批准后，工具会针对一个经历了 ≤5 分钟阻塞的 MCP
  连接 / credential-proxy token 执行。被删除的 worker 在执行时重新取 cred；本模型
  不取。核实 MCP stdio/http 连接和 cred-proxy token 能挺过阻塞；定义并向用户呈现
  "批准后工具失败"的行为。这是相对旧模型唯一的功能性回归 —— 把它关掉。
- **R2（S3）：** 重启恢复必须为存活 container 重建 `_GroupState` + 重放 suspend，
  而不仅是重新加载 DB 行（见 §8）。
- **R3（已解决）：** 超时默认 = 5 分钟（§1）。
- **R4（已解决，S4 记录）：** safety `require_approval` 保持硬阻塞；HITL 仅用于
  MCP policy（§1）。
- **运维：** 每个 pending 审批占用一个 container ≤ APPROVAL_TIMEOUT。确认
  `MAX_CONCURRENT_CONTAINERS` / `GLOBAL_MAX_CONTAINERS` 有余量；这是可接受的权衡，
  但若触顶则 `log()` 出来。
- **已知上游问题（测试备注，非本次引入）：** Pi warm-idle 的后续消息投递有一个既
  有怪癖（消息卡在 `pending_messages` 而非经 `agent.{job_id}.input`）。测试"suspend
  期间来新消息"时把它当作已知量看待。

## 10. Session 计划

**分支：** 所有 session 都在 **`feat/hitl-approval-B`**（单一共享分支）上工作。
不要每个 session 都开 PR 或合 `main` —— 在这个分支上增量提交，**只在整个 feature
（S1–S5）完成后才合 `main`**。

每个 session：先读本文件；`git checkout feat/hitl-approval-B`；用
`git commit -s` 增量提交（无 Co-Authored-By）；代码与测试一起交付。因为各 session
共用一条分支，后续每个 session 先做 `git log --oneline` 看前面落了什么，并在
§3–§5 冻结契约之上构建，而不是重新推导接口。按项目测试理念写对抗式测试 —— 找真实
bug，不写镜像测试，最小化 mock。

```
S1（基础 + 冻结契约）
   ├──> S2（container 阻塞 hook）┐
   └──> S3（Orchestrator suspend/resume）┘──> S4（投递 + 通知 + E2E = MVP）──> S5（policy CRUD + 隔离 + 文档）
```
顺序关键路径：**S1 → S2 → S3 → S4 → S5**（5 个 session）。S1 冻结契约后 S2 与 S3
可并行（4 波）。**S3 风险最高，可能跨两个 session（S3 → S3-cont）；不要把没做完的
竞态工作推给 S4。** MVP = S1–S4。

### S1 — 基础 + 冻结契约 — 风险：低
- 表 `approval_policies` / `approval_requests` + RLS（真正的单谓词模式，§4）。
  `db/approval.py` 经 tenant_conn / admin_conn 做 CRUD。
- `agent_runner/approval/policy.py`：`evaluate_condition` + `find_matching_policy`，
  纯函数，fail-closed（§7）。
- 配置：`APPROVAL_TIMEOUT=300_000` + 启动断言（§5）。
- **确认/锁定 §3 IPC 字段 schema** 作为给 S2/S3 的契约。
- 测试：条件边界（空 params、缺字段、类型不匹配、嵌套 and/or、`always`）、
  fail-closed ⇒ 需要审批、priority/updated_at 平手裁决、跨 tenant RLS 读/写隔离。
- **Exit：** 纯函数 + RLS 测试转绿；契约确认。

### S2 — Container 阻塞 hook + IPC — 风险：中
- 审批 hook handler：非 `mcp__*` → 放行；匹配 → 发布 `approval_request` →
  `await` 决策 Future（受 APPROVAL_TIMEOUT 限界）→ approve ⇒ `None`，
  reject/超时 ⇒ `ToolCallVerdict(block=True, reason=...)`。
- `request_id → asyncio.Future` 映射；在 backend `start()` 订阅
  `approval_decision`；把决策路由回各 await 点（支持一个 turn 内并发多审批）。
- `finally` → 在每条退出路径（reject/超时/Stop/异常）上发幂等 `approval_cancel`。
- 在 container init 时加载 policy 快照。
- **R1：** 实测多分钟阻塞后 MCP 连接 + cred-proxy token 的有效性；定义"批准后工具
  失败"的 UX；把结论写下来。
- 测试：按 policy 放行/阻塞；超时→阻塞；Future 路由；并发双审批独立路由；`finally`
  在全部四条退出路径上发 cancel；非 mcp 前缀放行。
- **Exit：** 单 container 审批环路能对着 stub Orchestrator 跑通；R1 结论已记录。

### S3 — Orchestrator suspend/resume + sweep + 重启恢复 — 风险：高
- 严格按 §8 做 suspend / resume（基于 set、强制 idle_waiting=False + 断言、仅在
  set 清空时重新武装）。
- `awaiting_approval` 共享 dict 按 §5 queue key 做 key；不重构 GroupQueue。
- 过期 watcher；决策竞态幂等（Future 先到先得 + 行级 status）。
- **R2：** 完整重启恢复 —— 重新加载 pending ∪ 重建 `_GroupState` ∪ 重放 suspend ∪
  重建决策路由；已过期 → 标记 + 硬通知。
- 测试（聚焦定时器生命周期）：suspend→重新武装→正常拆解；suspend→container 超时→
  拆解；并发双审批 → 只有最后一个重新武装；重启恢复 → 存活 container 被重新接管而
  非被回收；无双重 cancel / 误清。
- **Exit：** 全部定时器生命周期 + 重启恢复测试转绿。若 session 末未转绿，在
  S3-cont 继续 —— 不要进 S4。

### S4 — 投递 + 双通道通知 + E2E（MVP）— 风险：中
- 目标解析：`conversation_id → channel_bindings → channel_chat_id`；无活跃
  conversation 的定时任务 → 回退到最近一个。
- Telegram：inline ✅/❌ 卡片 + `CallbackQueryHandler`，`callback_data`
  `apr:{request_id}` / `rej:{request_id}`。**全新构建**（main 上无 inline kb）。
  **IDOR 防护：** 审批人身份从认证握手（ticket + DB）解析，绝不信任 client 载荷。
- Web：新增 v1 WS client frame（pydantic 成员 + `WsClientFrameModel` 联合 +
  `ws_stream` 接收分支 + 发布 NATS + OpenAPI 重生成 + ts client）+ 推送审批事件；
  持久化定时任务的 web 通知（挺过断连）。
- 双通道结果：软（block `reason` → agent 上下文）+ 硬（Orchestrator 确定性地把卡片
  编辑为 "❌ rejected" / "⏰ expired"，无 LLM）。
- **R4：** 在代码/文档里一句话 —— safety `require_approval` 保持硬阻塞，不是 HITL。
- 验证：在 **Telegram 和 Web 两个通道**上做 `amount > 100` 的 self-approval 端到端
  （批准 → agent 拿到结果并继续；拒绝 → agent 拿到 block reason + 用户拿到硬通道
  卡片）；resume（"继续" → 重试工具 → 再触发 hook → 新审批）。
- **Exit：** MVP 在两个通道上端到端工作。

### S5 — Policy CRUD + 隔离加固 + 文档 — 风险：低
- Policy CRUD REST + Web UI 条件构建表单。
- attack-sim 跨 tenant 隔离：tenant A 看不到 / 批不了 tenant B 的审批（RLS + IDOR）。
- 完善本文档 + 一份 `-cn.md` 译版（按 repo 惯例保留英文术语）；记录 block-and-await
  vs 旧 block-and-replay 的差异、R1 结论、R2 恢复语义。
- **Exit：** 跨 tenant 隔离测试转绿；文档完整。

## 11. 实现产出（随各 session 落地逐步填入）

本 feature 在 `feat/hitl-approval-B` 上经 S1–S5 交付完成。本节把计划要求各 session
记录的跨 session 结论汇总于此，使文档对 review 与最终合 `main` 而言是自包含的。

### 11.1 block-and-await vs 被删除的 block-and-replay（核心差异）

| 方面 | 被删的 v6.1（block-and-replay） | 本次重设计（block-and-await） |
|---|---|---|
| 被 gate 调用时 hook 的返回 | **立即** `block=True`；agent 的 turn 结束 | 就地 `await` 决策（≤ `APPROVAL_TIMEOUT`） |
| 批准后工具在哪执行 | 一个带外 worker/executor 稍后**重新 POST** 动作 | 在**同一 container、同一 turn** 执行；agent 在其 ReAct 循环中拿到真实结果 |
| agent 对结果的视角 | 永远不在自己的推理中看到 | 内联看到并正常继续 |
| 活动部件 | worker + executor + 动作重放幂等 + 审计日志触发器 | 都没有 —— 一个会阻塞的 hook，一个 Orchestrator coordinator |
| 付出的代价 | 无（turn 已结束） | 一个 container 被**占用** ≤ `APPROVAL_TIMEOUT`；由 §8 suspend/回收机制承担 |

这笔交易是有意为之：占用一个 container，是为了给 agent 一个真实、在环内的工具结果，
而不是脱节的重放。§8 的 suspend / 过期 / 重启恢复机制让这种占用变得安全。

### 11.2 R1 结论 —— 批准后的工具调用能挺过阻塞吗？（已解决，S2）

**结论：在进程内能；有一个残留边界是环境相关的。**

- 阻塞是**协作式**的 —— hook `await` 一个 `asyncio.Future`；事件循环从不冻结，所以
  等待期间 MCP keepalive、NATS 决策投递、idle/中断轮询器都在继续运转。
- MCP 连接生命周期是 **container/turn 级**，不是 per-call（Claude 为整个
  `run_prompt` 注册 `mcp_servers`；Pi 为 container 生命周期复用
  `McpServerConnection`）。**我们的代码不会在阻塞期间关闭 MCP 连接。**
- **没有 container 持有的凭证 token 会在期间过期**：LLM 凭证由 credential proxy
  按请求注入（container 只持有 `ANTHROPIC_BASE_URL`，不持 bearer）；外部 MCP 认证
  用静态的 per-request `X-RoleMesh-User-Id` header。不存在哪个 container 内 token
  会在 5 分钟等待中失效。（这是相对旧模型——它在执行时重取凭证——的唯一回归，已关闭。）
- **残留（单测无法关闭）：** 一个*远端* MCP server 或中间件可能在阻塞期间丢弃空闲
  的 HTTP/SSE 会话。5 分钟超时使窗口很短；若在 staging 证实存在，缓解办法：调低
  `APPROVAL_TIMEOUT`、依赖 MCP client 的透明重连、或对被 gate 的 server 发 keepalive
  ping。对正确性而言都不必要。
- **"批准后工具失败" 的 UX：** 没有单独的"已批准但失败"的硬通道。批准后调用失败会
  走**普通工具错误路径**（Claude `PostToolUseFailure`；Pi 带 `is_error` 的
  `tool_result`）—— agent 在上下文中看到错误并上报/重试。重试若再触发 hook，会产生
  一个**新的**审批请求。

### 11.3 R2 恢复语义 —— 挺过 Orchestrator 重启（已解决，S3）

`_groups` 是内存态、重启即丢，但处于审批等待的 container **挺过** Orchestrator，所
以恢复不只是重新加载行。启动时 `recover_pending()` 扫描
`approval_requests WHERE status='pending'`（跨 tenant，经 `admin_conn`），对每一行：

- **未过期** → `adopt_orphan_container` 重建一个最小的 active `_GroupState`，
  **重放 suspend 动作**（cancel idle handle、强制 `idle_waiting=False`、
  `awaiting_approval[key].add`），恢复 `approval_decision` 路由（subject 由行的
  `job_id` 推出），并重新武装过期 watcher。不重建 `_GroupState` 的话，被重新接管的
  container 会被立刻回收。
- **已过期** → 标记 `expired` + 触发硬通道通知。
- 整个过程在双重运行下**幂等**。

**已知运维注意点：** 默认 Docker runtime 会在恢复之前，于
`_ensure_container_system_running` 中经 `cleanup_orphans` 强制删除 `rolemesh-`
container，所以在该部署下要重新接管的 container 已经没了。`recover_pending()` 在那
里**优雅降级** —— 过期 watcher 触发，行被标记 `expired`，`_reap_adopted` 清掉重建
的 state，于是 conversation 不会卡死。对那些重启期间保留 container 的 runtime/配置
而言，重新接管路径是正确的。（作为运维项追踪，非 MVP 阻塞项。）

### 11.4 R4（已解决，S4）：两种 gate 类型，保持区分

Safety `require_approval` **保持硬阻塞、不进入 HITL**。HITL **仅** gate tenant 的
MCP 工具 policy 匹配（block-and-await hook）。在 Orchestrator 的 MODEL_OUTPUT 裁决
分支（`verdict.action in ("block", "require_approval")`）与此处钉死。不要合并它们。

## 12. S5 交付物（本 session）

- **Policy CRUD REST** —— `GET/POST /api/v1/approval-policies`，
  `GET/PATCH/DELETE /api/v1/approval-policies/{id}`，封装 S1 的 `db/approval.py`
  helper；严格 tenant 作用域（RLS + 显式 `WHERE tenant_id`）。畸形的
  `condition_expr` 在 API 层被拒（422），靠新增的纯函数 `validate_condition_expr`
  （它是 lenient、fail-closed 的 `evaluate_condition` 的严格、写入期对应物）。
- **供 web 重连用的 pending 读** —— `GET /api/v1/approval-requests`（可选
  `conversation_id` 过滤）只返回调用方 tenant 的 pending 行；投影暴露工具名 + 摘要，
  绝不暴露原始 params。
- **SPA 审批卡片** —— `rm-approval-card` 渲染 `event.approval.requested`
  （摘要 + ✅/❌），经 `V1WsClient.sendApprovalDecision` 转发点击
  （`request.approval_decision` frame；身份在服务端盖章），并在
  `event.approval.resolved` 时就地更新。（重）连接时聊天面板从 REST 读重渲染在途
  卡片（实时推送是 fire-and-forget）。
- **Policy CRUD Web UI** —— `rm-approval-policies-page`（Settings → Governance 下），
  带结构化的 §7 条件构建器（`always` / 一层扁平的 `field op value` 叶子的
  `and`/`or`）。对扁平构建器而言过于复杂的已存表达式以只读方式打开，保存时原样保留。
- **跨 tenant attack-sim（S5 exit 准则）** —— REST 层测试证明：tenant A 用户拿着
  tenant B 的 id 做读/patch/delete 一律得到平直的 404（无写入、无存在性预言机）、
  list 永不泄漏、pending 读即便带外来 `conversation_id` 也仍是 tenant 作用域、且
  恶意载荷无法夹带 `tenant_id`/`id`。DB 层 RLS + WHERE 双保险在 S1 已证明；本次在
  其之上加了 HTTP 层。
- **文档** —— 本 §11/§12 收尾 + 一份 `-cn.md` 镜像。
