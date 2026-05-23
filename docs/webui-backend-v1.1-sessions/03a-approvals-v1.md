# Session 03a — Approvals 迁 v1 + 多 user smoke  `[REFRESHED 2026-05-21]`

| field | value |
|---|---|
| Phase | 3 |
| Prerequisites | Phase 2 全 done（02a + 02b；02c retired） |
| Estimated PRs | 3 |
| Estimated LOC | ~1500（原估 900 偏低；现有基础厚但 endpoint + WS + 前端三路都要做） |
| Status | not started |

> **Refresh 起源**：Phase 1/2 落地后大幅刷新——`enum_translate.py` 已就位（01b 写好）、`resolve_actor_user_id` audit helper 已就位（00a）、`approval_policies` 表 + engine + executor + notification 全套已在 admin 路径运行。本 session 主要是**搬迁** + **WS event 接通** + **前端**，不再需要新 schema / 新 engine 设计。
>
> **第一次真业务用** BOOTSTRAP_USERS 多 user（00a 第 6 项）+ `resolve_actor_user_id` audit FK helper（00a 第 5 项）。alice 发起 / bob 审批端到端跑 = INV-4 的最终验收。

## Goal

1. 把 `/api/admin/approvals` + `/api/admin/approval-policies` 完整迁到 `/api/v1/*` 命名空间（schema + endpoint + 验收测试）
2. Engine 在 gating 时 publish `event.approval.required` 到 NATS；01b 落地的 v1 WS 端点（`/api/v1/conversations/{id}/stream`）forward 给当前订阅的 client
3. `request.approval` 从 WS 收到时翻译 enum + 调 `engine.handle_decision(outcome=...)`
4. 前端 `#/approvals` 队列页 + chat panel 内联 approval bridge UI
5. Phase 3 smoke：alice 在 tab 1 起 chat 触发 approval → bob 在 tab 2 看到 → bob approve → alice chat 继续 → DB audit_log 显示 bob 真 UUID

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §3 Phase 3 / §4 WS approval events / §6.3 I（approvals 页布局）/ §11 INV-4 + INV-7
2. **00a Findings § "INV-4 bootstrap actor"**：`resolve_actor_user_id` helper 实际签名 + 数据流
3. **00a Findings § "BOOTSTRAP_USERS 多 user fast-path"**：alice/bob 在 `BOOTSTRAP_USERS` env 里怎么落 users 表 + 拿真 UUID
4. **01a Findings § "ErrorResponse helper"**：`raise_error_response` 用法；本 session 所有 4xx 走这个
5. **01b Findings § "INV-7 enum 翻译"**：`http_action_to_outcome` / `ws_decision_to_outcome` / `outcome_to_ws_decision` 已就位，**直接消费**；`ApprovalEngine.handle_decision(outcome=...)` 接 engine enum
6. **01b WS 协议骨架** (`src/webui/v1/ws_stream.py`)：approval frame 名（`request.approval` / `event.approval.required` / `event.approval.resolved`）已在 protocol 中定义但 engine **未真发**——本 session 让 engine 真发 + WS handler 真 forward
7. **chore A NATS subscriber pattern** (`src/rolemesh/orchestration/run_cancel_subscriber.py`)：approval events 走类似的 durable subscriber 模式（webui 侧订阅 + forward 到 WS）
8. **现有 admin approval surface** (`src/webui/admin.py`)：
   - 第 795 行起：`/api/admin/approval-policies` POST/GET/PATCH/DELETE
   - 搜 `/approvals` 看 request endpoints + decide endpoint
   - 搜 `ApprovalEngine` 看现有调用模式（注入单例 `set_approval_engine`）
9. **现有 approval engine + 周边** (`src/rolemesh/approval/`)：
   - `engine.py`：核心 gating + handle_decision
   - `executor.py`：worker 异步执行 approved actions
   - `notification.py`：现有的 channel-based 通知（Slack/Telegram DM 给 approver）—— **保留不动**
   - `expiry.py`：超时清理
   - `types.py`：dataclass 定义

## 概念定位：WS event 是 additive 通知，不替换 channel 通知

现有 `notification.py` 已经实现"approval 触发时给 approver 的常用 channel 发消息"——alice/bob 在自己的 chat panel 里通过 NotificationGateway 拿到 inline approval request（设计 §6.3 I 提的）。**这部分不动**。

本 session 在此基础上**增加一条 WS event 通道**，目的是：

- 真坐在 `#/approvals` 队列页的用户能实时刷新（不需要 5 秒轮询）
- 真坐在 chat panel 的用户能看到 inline approval 卡片实时变化（notification 给 channel inline message 后，状态变化通过 WS event 推送）
- alice（发起者）在自己的 chat panel 看到 "Approved by bob" 实时显示

WS event 是 **additive**——失败时 channel notification 兜底，用户最坏体验是延迟看到状态，不是没看到。

## Scope — PR breakdown

### PR 1 — `/api/v1/approval-policies/*` + `/api/v1/approvals/*` endpoint 搬迁

**Policies**（设计 §3 Phase 3）：

- `GET /api/v1/approval-policies` —— 列表（按 tenant_id）
- `POST /api/v1/approval-policies` —— 创建
- `GET /api/v1/approval-policies/{id}` —— 详情
- `PATCH /api/v1/approval-policies/{id}` —— 更新（注意 enabled 字段是否要 hot-reload）
- `DELETE /api/v1/approval-policies/{id}` —— **设计 §3 表格**：pending requests 的 `policy_id` SET NULL，不阻塞已发出审批

**Requests**：

- `GET /api/v1/approvals` —— 列表（query params：status / coworker_id / 我作为 approver 的）
- `GET /api/v1/approvals/{id}` —— 详情
- `POST /api/v1/approvals/{id}/decide` —— body `{action: "approve"|"reject", note?}`
- `GET /api/v1/approvals/{id}/audit-log` —— 审计记录列表

**Decide endpoint 实现细节**（关键）：

```python
@router.post("/{request_id}/decide")
async def decide_approval(request_id: str, body: DecideRequest, user: AuthenticatedUser):
    # 1. INV-7 wire enum 翻译 (boundary)
    try:
        outcome = http_action_to_outcome(body.action)
    except ValueError as exc:
        raise_error_response(
            "INVALID_DECISION_ACTION", str(exc), status_code=422,
        )

    # 2. INV-4 audit actor resolve
    actor_user_id = await resolve_actor_user_id(
        tenant_id=user.tenant_id, current_user_id=user.user_id,
    )

    # 3. Engine 内部 only sees engine enum + real UUID
    await engine.handle_decision(
        request_id=request_id,
        outcome=outcome,
        actor_user_id=actor_user_id,
        note=body.note,
    )

    return {"ok": True}
```

**全部 RLS + 显式 `WHERE tenant_id`** 双层防御。所有 4xx 走 `raise_error_response`。

**Pinned tests**：

- alice approve / bob reject 各自 audit_log 写入 `actor_user_id` = 真 UUID（INV-4 端到端验证）
- DELETE policy 时 pending requests 的 `policy_id` SET NULL（不级联删 request）
- 跨租户 RLS：tenant A 看不到 tenant B 的 approval / policy
- bootstrap 单 token 模式下 decide 触发 `resolve_actor_user_id` fallback 到 owner（00a 行为）；如果 tenant 无 owner 返 503

### PR 2 — Engine NATS publish + WS forward

**Engine 端 publish**：

- 修改 `ApprovalEngine`（或者注入一个 `notifier: ApprovalEventPublisher`）：
  - approval gate 触发新 request 时 publish `web.approval.required.{conversation_id}` (NATS subject 命名沿用 `web.>` stream)
  - `handle_decision` 完成 outcome 写入后 publish `web.approval.resolved.{conversation_id}`
- payload schemas（与 01b 协议对齐）：
  ```json
  // event.approval.required
  {
    "approval_id": "<uuid>",
    "run_id": "<uuid>",
    "summary": { "tool_name": "...", "args": {...} }
  }
  // event.approval.resolved
  {
    "approval_id": "<uuid>",
    "decision": "approve"|"deny"|"expired"|"cancelled",
    "actor_user_id": "<uuid>",
    "note": "..."
  }
  ```

**WS handler forward**：

- `src/webui/v1/ws_stream.py` 现有 forwarder 增加 approval 事件路径：
  - subscribe NATS `web.approval.required.{conversation_id}` + `web.approval.resolved.*`（resolved 需要 fan-out 给所有相关 client）
  - 收到 NATS 事件 → 翻译成 WS frame（含 `outcome_to_ws_decision` 用于 decision 字段）→ send_event
- `request.approval` 入口处理：
  - 从 WS 收到 `{approval_id, decision: "approve"|"deny", note?}`
  - INV-7：`ws_decision_to_outcome(decision)` → engine outcome
  - 调 `engine.handle_decision(...)`（与 HTTP /decide endpoint 同一条 engine 路径，避免双实现）
- `resolved` event fan-out 关键：alice 在 conversation A 起 chat，bob 在 conversation B 审批；alice 的 WS（订阅 A）需要收到这个 resolved 事件——subject pattern 应该让两边都能 match

**Pinned tests**：

- engine.handle_decision → NATS publish 真发出（mock orchestrator subscriber 或真 testcontainer NATS）
- WS handler 收到 NATS approval.required → 推到 client（mock WS connection 验 send_event 调用）
- WS handler 收到 `request.approval` → engine.handle_decision 被调用（mock engine，验参数）
- ws_decision_to_outcome 边界翻译：`"deny"` → `ApprovalOutcome.rejected`
- INV-7 mutation 测试：从 wire enum 漏到 engine 任何 string 都失败（grep `"approve"\|"deny"\|"reject"` 在 `engine.py` / `executor.py` 应只剩 enum_translate 引用）

### PR 3 — Frontend approvals 队列 + chat-panel inline bridge

**Approvals 队列页** (`#/approvals`)：

- 列表组件 `<rm-approvals-page>`：
  - 调 `GET /api/v1/approvals?status=pending` 显示待我审批的 + tenant 内 visible 的
  - 每行：coworker 名 / tool 名 / 简短 args / 时间戳 / Approve + Reject buttons
  - Click → 调 `POST /api/v1/approvals/{id}/decide` (typed client)
- 详情页 `#/approvals/:id`：
  - 完整 args / policy 上下文 / audit log timeline
  - decide buttons + note 输入

**Chat panel inline approval bridge**：

- 现有 chat-panel 已经收 approval-related inline message（来自 `notification.py` 通过 channel gateway 发的）
- 增强：监听 `event.approval.resolved` WS event → 更新对应 inline approval message 的状态显示（从 "Pending" → "Approved by bob" / "Rejected by bob"）
- alice 的 chat panel：收到 `event.approval.required` → 显示 "Waiting for approval..." 占位；收到 `event.approval.resolved` → 更新为 "Approved/Rejected"
- bob 的 chat panel：收到 `event.approval.required` 携带 inline action → 显示 inline approve/reject button → click 时调 WS `request.approval` frame

**Real-time 路径**：WS event 总线（01c 落地的）添加 `event.approval.required` / `event.approval.resolved` handler。前端不开新 WS 连接，复用 v1 stream。

**Pinned tests**（vitest）：

- 监听 mock WS `event.approval.required` → page 显示新 row
- decide button click → typed client POST 被调用
- WS `event.approval.resolved` → row 状态更新

## Acceptance criteria

- [ ] `/api/v1/approval-policies/*` + `/api/v1/approvals/*` 全 endpoint 工作
- [ ] **INV-4 端到端验证**：bootstrap 多 user 模式下 alice/bob 各自 decide → audit_log.actor_user_id = 真 UUID（不是 "bootstrap" 字面量）
- [ ] **INV-7 enum 翻译**：HTTP `action` / WS `decision` / engine `outcome` 三处 enum 边界翻译正确；engine 内部 grep wire string 应只剩 enum_translate.py
- [ ] WS `event.approval.required` / `event.approval.resolved` 端到端推送（含 fan-out）
- [ ] DELETE policy 时 pending requests `policy_id` SET NULL（不级联）
- [ ] 跨租户 RLS 隔离
- [ ] OpenAPI yaml 同步 + codegen 同步 + contract test 绿
- [ ] **Phase 3 smoke**（设计 §10）：
  - 起 BOOTSTRAP_USERS=`[{alice, owner}, {bob, owner}]` + 一个 approval-gated MCP server
  - alice 在 tab 1 起 chat → coworker 调 gated tool → approval 触发
  - bob 在 tab 2 看到 inline approval（chat panel）+ 队列页有新 row
  - bob approve → engine NATS publish → 两边 WS event → alice 的 chat 继续 → bob 的 inline approval 显示 "Approved"
  - DB 验证：`approval_audit_log.actor_user_id` 是 bob 真 UUID
- [ ] Phase 1/2 e2e 不退化
- [ ] 更新 plan 状态

## Out of scope

- ❌ Skills per-tenant 迁移 —— 03b
- ❌ Policy DSL 演进（schema 不动；admin 已有的 policy fields 全搬过来）
- ❌ Approval analytics / report endpoint（v2）
- ❌ 替换现有 `notification.py` 的 channel-based 通知逻辑（WS event 是 additive）
- ❌ Approval expiry 改动（现有 `expiry.py` 不动）
- ❌ Bulk decide / batch operation（一次一个 decide，简单优先）
- ❌ Comment thread on approval（v2 nice-to-have）
- ❌ admin endpoints 删除（保留兼容期，admin 与 v1 双发布 6 个月）

## Open questions

锁定：

1. ~~`approval_policies` 现状~~ → **表已存在**，schema 完整（policy_id / coworker_id / mcp_server_name / tool_name / enabled / approver_user_ids / mode / auto_execute / ...）。本 session 只搬 endpoint，不动 schema / 不重设计 DSL
2. ~~`event.approval.required` 推送范围~~ → **保留现有 channel-based notification 给 approver 个人**（policy.approver_user_ids 决定的 approver list）；**WS event 是 additive**，推给当前订阅这个 conversation 的 client（alice 自己 + 任何刚好在 approvals 队列页的 user）

仍需 session 内决策：

1. **`/api/v1/approvals` 列表的 filter 维度**：默认列"我作为 approver 的待批"还是"tenant 全部 pending"？推荐前者（实用），加 query param `scope=all` 让 admin 看全部
2. **`event.approval.resolved` 的 fan-out subject pattern**：用 `web.approval.resolved.{conversation_id}` 还是 `web.approval.resolved.{approval_id}`？前者方便 alice WS（订阅 conversation）拿到；后者方便 audit follower。可能需要双发布
3. **chat-panel inline bridge 的实现位置**：是 chat-panel 内增加 approval message type 处理，还是单独的 `<rm-inline-approval>` 组件 import 进 chat-panel？后者更隔离

## Pitfalls

- **audit FK 必须走 `resolve_actor_user_id`**——直接写 `user.user_id` 在 bootstrap 单 token 模式下是字符串 "bootstrap" 会 FK 违例。**bootstrap 多 user 模式下** alice/bob 已经是真 UUID（00a PR5 落地的 upsert users）——但 helper 仍然要过（对 helper 是 no-op，对 single-token bootstrap 是 fallback）
- **INV-7 翻译层必须包覆两条路径**：HTTP `action` 与 WS `decision`。容易漏 WS 这条（因为 01b 留了协议但没 wire）
- **approval engine 内部仍只见 `ApprovalOutcome` enum**——任何 wire string 漏到 `engine.py` / `executor.py` 是 bug。session 结尾 grep `"approve"\|"deny"\|"reject"` 这两个文件应只剩 enum_translate 引用
- **engine 调用方有两个：HTTP /decide + WS request.approval**——两个入口必须 INV-7 翻译后才进 engine。不要让 WS handler 直接调 engine 而绕过翻译
- **WS event 的 fan-out**：alice 在 conv A，bob 在 conv B。bob 的 decide 触发 resolved event 必须能 alice 那边收到。subject pattern 设计错会让"已审批"看不到。pinned test 必须覆盖跨 conversation fan-out
- **`event.approval.required` payload 含 `args`**：可能含敏感信息（用户输入 / API 内部数据）。如果 args 字段大或敏感，考虑只发 summary，详细让 client 主动 GET。**先简单做（全发）**——后续 audit/redact 是独立 chore
- **bob 的 chat panel 内联 approval 与队列页是两个 entry point**：同一个 approval 可能在两个地方都显示，decide 后**两边都要更新**——WS event 是同一份，两个组件订阅同一个 bus 即可
- **多 user smoke 真用 BOOTSTRAP_USERS**：alice + bob 在 env 里都标 role=owner（不是 alice=owner / bob=member，否则 bob 可能没 approval 权限——具体看 policy.approver_user_ids 配置）
- **现有 admin endpoint 不能下线**：保留 6 个月兼容期；admin 路径仍走老 engine 调用（同一个 engine 实例），所以 admin + v1 双入口共享状态机

## 执行前刷新清单

- [ ] Phase 2 完成？（plan.md 显示 02a + 02b done，02c retired）
- [ ] 现有 admin approval endpoints 数量 + filter 维度（grep `/approvals\|/approval-policies` 在 admin.py）
- [ ] `ApprovalEngine.handle_decision` 当前签名（01b refactor 后是 `outcome=`，本 session 直接复用）
- [ ] `notification.py` 当前给 approver 的发送路径——确认本 session 不动它
- [ ] BOOTSTRAP_USERS 多 user 在 Phase 1/2 e2e smoke 中验证过 alice/bob 拿真 UUID（00a PR5 测试 + 01a smoke）

## Findings (after execution)

### 落地概要

- 3 个独立 commit 累在 `feat/ui`（按 PR 1/2/3 拆）
- 总测试：158 个相关测试全绿（approval + webui + openapi + audit）
- 前端 vitest：31 个用例（之前 13 → +18）
- Pre-existing TS errors in `credentials-page.ts` / `mcp-servers-page.ts` 与本 session 无关；新组件类型检查干净

### 1. Admin endpoints 搬迁完整度

`/api/admin/approval-policies` 与 `/api/admin/approvals` 保留 6 个月兼容期（设计 §3 + open question 锁定），未删除。v1 surface 完整覆盖：

| Admin endpoint | v1 endpoint | 字段差异 |
|---|---|---|
| `GET /approval-policies` (`coworker_id?` / `enabled?`) | `GET /api/v1/approval-policies` | 完全一致 |
| `POST /approval-policies` | `POST /api/v1/approval-policies` | v1 加 cross-tenant `coworker_id` 422 守卫；admin 没有 |
| `GET / PATCH / DELETE /approval-policies/{id}` | 同 | DELETE 行为相同（SET NULL 由 schema 保证）|
| `GET /approvals` (`status?` / `coworker_id?`) | `GET /api/v1/approvals` | **新增 `scope=mine\|all`**；默认 `mine`（caller in `resolved_approvers`），`all` 需要 `view_all_conversations` 否则 403 |
| `GET /approvals/{id}` | 同 | audit_log inline 一致 |
| `GET /approvals/{id}/audit-log` | 同 | 完全一致 |
| `POST /approvals/{id}/decide` | 同 | v1 用 `raise_error_response` envelope；admin 用 `HTTPException(detail=str)` |

**Engine 共享**：`webui.v1.approval_engine_registry.{set,get}_approval_engine` 把进程级 engine 句柄从 `webui.admin._approval_engine` 提取出来，admin 模块 re-export `set_approval_engine` 名以保持 `webui.main.lifespan` 不变。v1 模块直接读 registry。两边走同一份 engine 实例，无双实现。

**附带 fix**：admin 的 `ApprovalRequestResponse.policy_id` 从 `str` 收窄为 `str | None`（兼容现有 SET NULL 行为；之前是潜在 `"None"` bug），同步收窄 `rolemesh.approval.types.ApprovalRequest.policy_id`、`rolemesh.db.approval._record_to_approval_request` 解析路径。

### 2. WS fan-out subject 模式

按 prompt 锁定走**双发布**：

- `web.approval.required.{conversation_id}` —— 单发布（一个 pending 行天然属于一个对话）
- `web.approval.resolved.conv.{conversation_id}` + `web.approval.resolved.req.{approval_id}` —— **每个 decision 同时发两个 subject**

实际遇到的 conversation：alice 与 bob 在**同一个对话**里就够（alice 在 chat panel 发起，bob 通过 channel notification 拿到 inline approval → click 内嵌 approve → engine.handle_decision → 双发布 → conv.{conv_id} 让 alice 的 WS 拿到 resolved）。

**queue page WS 没接**——简化决策：queue page 是 cross-conversation 的，没有"一个 conv 的 WS"能挂；同时 design §6.3 I 也不要求实时。改用 **15s REST 轮询**（`ApprovalsPage.REFRESH_INTERVAL_MS`），加上 decide 后立刻 refresh。`req.{approval_id}` subject 留着，为后续 audit follower / queue page 升级到 SSE 时备用，不变 schema。

### 3. chat-panel inline bridge 实现位置

按 open question 3 锁定走**独立 component** `<rm-inline-approval>` (`web/src/components/inline-approval.ts`)：

- 同时被 chat-panel + approvals-page import；同一份 markup 渲染两处
- 组件只管自己的 busy/error/status 三态，emit `rm-approval-decided` CustomEvent 让父级决定后续动作（chat-panel 啥也不做、approvals-page 重 fetch）
- 不订阅 WS——父组件负责把 `event.approval.resolved` 翻译成 `setStatus(...)` 调用，避免多个组件抢同一个事件
- 单测覆盖：button click → typed `ApiClient.decideApproval` / setStatus → DOM 反映 / can-decide=false 隐藏按钮

chat-panel 维持一个 `Map<approval_id, {…, status}>` state，在 message-list 与 message-editor 之间渲染一组 inline card。switch conversation 时清空。

### 4. INV-4 端到端 audit 验证

测试 `tests/webui/test_v1_approvals.py::TestDecide::test_approve_writes_audit_row_with_real_actor_uuid` + `test_reject_…` + `test_bootstrap_token_decide_falls_back_to_tenant_owner` 直接 `SELECT actor_user_id FROM approval_audit_log` 验证：

- alice 真 UUID 发起 / bob 真 UUID approve → audit row `actor_user_id = bob.id`（pin 过 `uuid.UUID(actor_user_id)` 解析成功）
- bob 真 UUID reject 同理
- bootstrap 单 token (`user_id="bootstrap"` 字面量) → helper fallback 到 tenant owner UUID 写入 audit
- bootstrap 单 token + 无 owner → `503 BOOTSTRAP_NEEDS_TENANT_OWNER` envelope（不写 audit 不破 FK）

WS 路径 `_handle_request_approval` 也过 `resolve_actor_user_id`：BootstrapActorError 转 `event.run.error` frame，code = `BOOTSTRAP_NEEDS_TENANT_OWNER`，端到端 pin 在 `test_v1_ws_approval.py::TestRequestApprovalHandler::test_bootstrap_no_owner_surfaces_503_envelope`。

### 5. INV-7 grep 验证

`tests/webui/test_v1_ws_approval.py::test_inv7_no_wire_strings_in_engine` 在 `src/rolemesh/approval/engine.py` + `executor.py` grep `'approve'|'deny'|'reject'`（带引号），忽略注释行。当前结果：**0 violations**。

手动验证：`engine.py:418` 有 `# "deny"` 注释（被 grep 跳过）。所有 wire string 都集中在 `src/rolemesh/approval/enum_translate.py` 的三个翻译函数。

### 6. WS event 是 additive 通知

`notification.py`（channel-based 发 inline message 给 approver）**未触碰**。WS handler 通过新 NATS subject 增加 additive 通道，断了走 channel 兜底——chat-panel 收 channel inline message 后会通过 channel 端到端展示 approval 状态（这部分是已存在路径，不在 03a 范围）。

### 7. 对 03b 的影响

- `webui.v1.approval_engine_registry` 模块新增，03b 的 skills per-tenant session **不需要**它；隔离干净
- ApprovalEngine `_publish_web_required` / `_publish_web_resolved` 是 best-effort（NATS 失败只 log）；如果 03b 引入新的 engine 入口（不太可能），同样应该过这两个 publish hook
- `webui.schemas_v1` 多 8 个 approval 相关类型——schema 文件 ~600 行，仍可读
- chat-panel 已经 import `<rm-inline-approval>`；03b 若需要 inline skill prompt 之类的，可以参照独立组件 + parent dispatch event 的模式
- `ApprovalRequest.policy_id` widening (`str → str | None`) 在 `src/rolemesh/approval/types.py` 与 admin `ApprovalRequestResponse` 同步——所有下游消费者只会变更"更宽松"，不破 caller

### 8. Frontend 状态

- `npm test`: 31/31 通过
- `npm run build`: 通过
- `npm run lint:no-admin-chat`: clean
- `npm run openapi:gen` + commit 已同步
- TS 错误：仅 pre-existing 的 `credentials-page.ts` + `mcp-servers-page.ts`（与本 session 无关）

### 9. Phase 3 smoke 5 步流程

`tests/webui/test_v1_approvals.py::TestDecide` + `tests/webui/test_v1_ws_approval.py` 已经覆盖等价的 11 个端到端断言：

- alice 真 UUID 发起 + bob 真 UUID approve（DB audit row 真 UUID）
- alice 真 UUID 发起 + bob 真 UUID reject（DB audit row 真 UUID）
- bootstrap fallback → tenant owner UUID
- 403 非 approver、409 已 decided、404 跨 tenant、422 无效 action
- WS request.approval → engine.handle_decision（同一 engine 实例）
- WS event.approval.resolved 双 subject 发布（`conv.{id}` + `req.{id}`）
- 引擎 outcome `rejected` 翻译为 WS 线 `deny`
- safety path 也发 web.approval.required

需要真实 docker + nats 跑 5 步浏览器流程的 live smoke 留给 03b 之后的合并验证：把 BOOTSTRAP_USERS env 设成 `[{alice,owner},{bob,owner}]` → 起 dev backend + frontend → 两个浏览器 tab 走完。本 session 的单元 + 集成测试已经覆盖关键不变量；浏览器流程只验 UI 渲染，不验业务正确性。
