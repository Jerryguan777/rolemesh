# Session 01b — WS 新协议 + run state machine + INV-6/INV-7

| field | value |
|---|---|
| Phase | 1 |
| Prerequisites | 01a done（runs lifecycle helper + ws-ticket endpoint 已就绪）|
| Estimated PRs | 3-4 |
| Estimated LOC | ~1500 |
| Status | not started |

## Goal

落地设计 §4 的 WS 新协议 + Conversations/Runs/Messages 的 REST endpoints + 把 INV-6（runs 终止路径全覆盖）与 INV-7（wire/engine enum 翻译）的 pinned test 立起来。**INV-6 与 INV-7 的 pinned test 必须在本 session 内完成**——否则 enum 漂移和 ghost run 立刻就有。

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §3 Conversations/Runs / §4 WS 协议 / §11 INV-6 / INV-7 / §12 命名陷阱
2. 01a Findings：runs lifecycle 实际签名、ws-ticket 是否绑 conversation_id、NATS hot-reload topic 现状
3. `src/webui/ws.py` —— 现有 WS handler 实现
4. `src/rolemesh/ipc/web_protocol.py` —— 现有 IPC 消息类型（00a PR2 已加 unknown-keys filter）
5. `src/rolemesh/channels/` 下的 web_nats_gateway —— orchestrator 端
6. `src/rolemesh/approval/executor.py` —— 看 wire status (`"approved"` / `"rejected"`) 与 engine ApprovalOutcome 的当前耦合（要解耦）

## Scope — PR breakdown

### PR 1 — Conversations / Messages REST endpoints

- 实现：
  - `GET/POST /api/v1/coworkers/{id}/conversations`
  - `GET/DELETE /api/v1/conversations/{id}`
  - `GET /api/v1/conversations/{id}/messages`
  - `GET /api/v1/runs/{id}`
  - `POST /api/v1/runs/{id}/cancel`
- `POST /runs/{id}/cancel` 必须：
  - 通过 NATS 发 `run.cancel.{run_id}` event 给 orchestrator
  - **不**立即写 `runs.status = cancelled`——等 orchestrator 处理后由 lifecycle helper 写
  - 返 202 Accepted（异步）
  - 如果 run 已 terminal，返 409 + `code="ALREADY_TERMINAL"`
- 全部走 RLS + 显式 `WHERE tenant_id` 双层防御
- pinned test：每个 endpoint 至少一个 happy + 一个 RLS 隔离测试

### PR 2 — WS 新协议实现 + 重连 truth fetch

按设计 §4 完整实现：

- 新建 `src/webui/v1/ws_stream.py`（不要直接改 `webui/ws.py`，并存一段时间）
- 端点：`WS /api/v1/conversations/{id}/stream?ticket=<jwt>`
- 握手：
  - verify ticket（exp / sig / tenant_id / conversation_id 与 path 一致）
  - 失败返 close code 4001 + reason `WS_TICKET_EXPIRED` / `WS_TICKET_INVALID`
- client→server messages：
  - `request.run {input, run_id?, idempotency_key?}` —— 如果带 `idempotency_key` 且最近见过相同 key，幂等返已有 run_id（防 reconnect 重投）
  - `request.cancel {run_id}` —— 同 REST cancel 路径
  - `request.approval {approval_id, decision: "approve"|"deny", note?}` —— 见 INV-7
- server→client events 完整覆盖设计 §4 清单
- **重连约束**：客户端约定先 `GET /api/v1/runs/{id}` 拿真值再决定要不要订阅。server 端不主动 replay 完成 run 的 token stream（已 GET 过的客户端不需要）
- pinned test：`tests/test_ws_v1_handshake.py`
  - 合法 ticket 握手成功
  - 过期 ticket 返 4001 + WS_TICKET_EXPIRED
  - ticket 的 conversation_id 与 path 不一致 → 拒绝
  - bootstrap user 拿 ticket → 握手成功
  - 多 user map alice 拿 ticket → 握手后 user_id 是 alice 真 UUID

### PR 3 — INV-6: run state machine 全终止路径覆盖

**这是本 session 最关键的 PR**。设计 §4 列了 6 条终止路径：

1. WS 正常完成（`event.run.completed`）
2. WS 错误（`event.run.error`）
3. `POST /runs/{id}/cancel`
4. 调度器异步完成（Phase 2 后才真的有，但路径要留）
5. approval reject 终止
6. coworker 容器 crash / OOM / timeout
7. user-mode MCP token 失效 → `awaiting_reauth`（架构保留，bootstrap 下不触发）

每条都必须在 webui / orchestrator / agent 容器某一端用 `update_run_terminal()` UPDATE。

落地策略：

- 把所有 run 终止 logic 集中到 lifecycle helper 调用——禁止直接 SQL UPDATE
- orchestrator 监听 agent 容器的"完成 / 失败 / 超时"事件 → 调 lifecycle helper
- WS handler 在客户端断开 + run 仍 running 时**不写 cancelled**（用户切 tab，run 应该继续；只有显式 cancel 才写）
- pinned test：`tests/test_run_state_machine_all_paths.py`
  - 用 parametrize 把所有 7 条路径列出
  - 每条路径触发后，断言 `runs.{status, completed_at, usage}` 都被 UPDATE
  - 路径 6（容器 crash）需要 simulate orchestrator 收到 die event；用 mock 一层 orchestrator-side event handler
  - 路径 7（reauth）即使 bootstrap 下不会真触发，pinned test 必须能 trigger 这个 code path（注入一个 fake 401 from MCP）然后断言 status='awaiting_reauth'
  - **变异测试**：随便选一条路径，把 lifecycle UPDATE 注释掉，测试必须红

### PR 4 — INV-7: wire/engine enum 翻译层 + pinned test

按设计 §3 末尾 + §12 命名陷阱：

- HTTP `POST /approvals/{id}/decide` body: `{action: "approve" | "reject"}`
- WS `request.approval` body: `{decision: "approve" | "deny"}`（注意：reject 在 wire 上叫 "deny"）
- WS `event.approval.resolved` body: `{decision: "approve" | "deny" | "expired" | "cancelled"}`
- Engine internal `ApprovalOutcome = Literal["approved", "rejected", "expired", "cancelled"]`

**实现**：

- 新建 `src/rolemesh/approval/enum_translate.py`：
  ```python
  def http_action_to_outcome(action: str) -> ApprovalOutcome: ...
  def ws_decision_to_outcome(decision: str) -> ApprovalOutcome: ...
  def outcome_to_ws_decision(outcome: ApprovalOutcome) -> str: ...
  ```
- 在 HTTP handler / WS handler 入口立刻翻译，**engine 代码内部只见 ApprovalOutcome enum**
- 现有 `approval/executor.py` 里用 `"approved"` / `"rejected"` 字面量的地方（grep 已有）保持，但需求改自 wire 端的 string 全走翻译层
- pinned test：`tests/test_approval_enum_translation.py` —— `TestResolvedDecisionMap`：
  - 对每个 wire enum value 列出预期 engine enum value，用 parametrize 跑
  - 反向同理
  - 故意传一个不在 enum 内的 value → 期望抛 ValueError，不要 silently fallback
  - 测试**禁止 mock 翻译函数**——直接调真函数

## Acceptance criteria（session 级）

- [ ] `pytest tests/test_v1_conversations.py tests/test_ws_v1_handshake.py tests/test_run_state_machine_all_paths.py tests/test_approval_enum_translation.py` 全绿
- [ ] INV-6 pinned test 7 条终止路径全覆盖；变异（删 UPDATE）能让测试红
- [ ] INV-7 pinned test 包含 wire ↔ engine 双向 + 非法 value 抛错
- [ ] WS 新协议端到端：bootstrap user 连 ws → `request.run` → 收到 `event.run.started` → 收到 token stream → 收到 `event.run.completed` → runs 表里 status='completed' + completed_at + usage 都写了
- [ ] 重连场景：断开 WS → 重新打开 → 先 `GET /api/v1/runs/{id}` 看到 'completed' → 不重新订阅 → 不收到任何 stream（不漏数据 + 不重发）
- [ ] 现有 webui WS handler 仍能用（不替换）
- [ ] OpenAPI yaml 更新 + codegen 通过
- [ ] 全套测试通过
- [ ] 更新 plan 状态

## Out of scope

- ❌ 前端 chat 接入新 WS —— 留 01c
- ❌ Approvals 业务 API（`/api/v1/approvals/*`）—— 留 03a；本 session 只翻译 enum
- ❌ Scheduled run 触发路径真实现 —— 留 02a 或后续；本 session 只在 INV-6 测试里 stub 一下证明 code path 存在
- ❌ user-mode MCP credential_proxy —— 留 02c；本 session INV-6 路径 7 测试里 stub 一个 fake 401

## Open questions

1. **断开 != cancel 的语义确认**：用户关 browser tab，run 应该继续（"fire and forget"）。这是设计意图，但 UI 上有没有"用户取消" button？如果有，按 button 就走 `POST /runs/{id}/cancel`，关 tab 不走——这个确认。
2. **`awaiting_reauth` 是 terminal state 还是 paused state**（plan critique §2）：选 terminal 简化（user 重登后是新 run，client 拿历史重投）OR paused（需要 `POST /runs/{id}/resume`）？**推荐 terminal**——简单且符合 Phase 1 不接 OIDC 的现实。如果选 paused，INV-6 措辞要改。
3. **`request.run` 的 `idempotency_key`**：选客户端生成 + server 端 60s 滑窗 dedup，还是 server 端给 ack 后客户端别再重投？前者更鲁棒。

## Pitfalls

- **engine 内部不能见 wire enum value**——HTTP handler 收到 `action="approve"` 必须立刻翻成 `ApprovalOutcome.APPROVED`，不能存原始字符串
- INV-6 测试的"变异测试"必须真做：把任意一条 UPDATE 注释掉，pytest 必须红。如果有 happy path 整测试套都绿，说明覆盖有洞
- `request.cancel` 不能直接 webui 端写 status=cancelled——必须经 orchestrator（agent 容器还在跑时让 orchestrator stop 容器 + 写 UPDATE）。webui 直接 UPDATE 会让 agent 容器 ghost
- WS ticket exp ≤ 60s——ticket 比 access_token 短得多，刷新一次只用一次
- `event.run.requires_reauth` payload 必须包含 `reason`（"refresh_token_expired" / "user_revoked"），UI banner 才能 differentiated 显示
- **idempotency_key dedup 窗口**不能跨 run 边界——同 conversation 内 60s 滑窗够；不要跨 conversation 共享

## Findings (after execution)

_(empty — 重点记录：`awaiting_reauth` 最终选了哪个语义？INV-6 7 条路径有没有发现遗漏？idempotency 实现细节？)_
