# Session 01b — WS 新协议 + run state machine + INV-6

| field | value |
|---|---|
| Phase | 1 |
| Prerequisites | 01a done（runs lifecycle helper + ws-ticket endpoint 已就绪）|
| Estimated PRs | 3-4 |
| Estimated LOC | ~1500 |
| Status | done (2026-05-20) |

## Goal

落地设计 §4 的 WS 新协议 + Conversations/Runs/Messages 的 REST endpoints + 把 INV-6（runs 终止路径全覆盖）的 pinned test 立起来。**INV-6 的 pinned test 必须在本 session 内完成**——否则 ghost run 立刻就有。

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §3 Conversations/Runs / §4 WS 协议 / §11 INV-6 / §12 命名陷阱
2. 01a Findings：runs lifecycle 实际签名、ws-ticket 是否绑 conversation_id、NATS hot-reload topic 现状
3. `src/webui/ws.py` —— 现有 WS handler 实现
4. `src/rolemesh/ipc/web_protocol.py` —— 现有 IPC 消息类型（00a PR2 已加 unknown-keys filter）
5. `src/rolemesh/channels/` 下的 web_nats_gateway —— orchestrator 端

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
- server→client events 完整覆盖设计 §4 清单
- **重连约束**：客户端约定先 `GET /api/v1/runs/{id}` 拿真值再决定要不要订阅。server 端不主动 replay 完成 run 的 token stream（已 GET 过的客户端不需要）
- pinned test：`tests/test_ws_v1_handshake.py`
  - 合法 ticket 握手成功
  - 过期 ticket 返 4001 + WS_TICKET_EXPIRED
  - ticket 的 conversation_id 与 path 不一致 → 拒绝
  - bootstrap user 拿 ticket → 握手成功
  - 多 user map alice 拿 ticket → 握手后 user_id 是 alice 真 UUID

### PR 3 — INV-6: run state machine 全终止路径覆盖

**这是本 session 最关键的 PR**。设计 §4 列了这些终止路径：

1. WS 正常完成（`event.run.completed`）
2. WS 错误（`event.run.error`）
3. `POST /runs/{id}/cancel`
4. 调度器异步完成（Phase 2 后才真的有，但路径要留）
5. coworker 容器 crash / OOM / timeout
6. user-mode MCP token 失效 → `awaiting_reauth`（架构保留，bootstrap 下不触发）

每条都必须在 webui / orchestrator / agent 容器某一端用 `update_run_terminal()` UPDATE。

落地策略：

- 把所有 run 终止 logic 集中到 lifecycle helper 调用——禁止直接 SQL UPDATE
- orchestrator 监听 agent 容器的"完成 / 失败 / 超时"事件 → 调 lifecycle helper
- WS handler 在客户端断开 + run 仍 running 时**不写 cancelled**（用户切 tab，run 应该继续；只有显式 cancel 才写）
- pinned test：`tests/test_run_state_machine_all_paths.py`
  - 用 parametrize 把所有终止路径列出
  - 每条路径触发后，断言 `runs.{status, completed_at, usage}` 都被 UPDATE
  - 路径 5（容器 crash）需要 simulate orchestrator 收到 die event；用 mock 一层 orchestrator-side event handler
  - 路径 6（reauth）即使 bootstrap 下不会真触发，pinned test 必须能 trigger 这个 code path（注入一个 fake 401 from MCP）然后断言 status='awaiting_reauth'
  - **变异测试**：随便选一条路径，把 lifecycle UPDATE 注释掉，测试必须红

## Acceptance criteria（session 级）

- [ ] `pytest tests/test_v1_conversations.py tests/test_ws_v1_handshake.py tests/test_run_state_machine_all_paths.py` 全绿
- [ ] INV-6 pinned test 全部终止路径覆盖；变异（删 UPDATE）能让测试红
- [ ] WS 新协议端到端：bootstrap user 连 ws → `request.run` → 收到 `event.run.started` → 收到 token stream → 收到 `event.run.completed` → runs 表里 status='completed' + completed_at + usage 都写了
- [ ] 重连场景：断开 WS → 重新打开 → 先 `GET /api/v1/runs/{id}` 看到 'completed' → 不重新订阅 → 不收到任何 stream（不漏数据 + 不重发）
- [ ] 现有 webui WS handler 仍能用（不替换）
- [ ] OpenAPI yaml 更新 + codegen 通过
- [ ] 全套测试通过
- [ ] 更新 plan 状态

## Out of scope

- ❌ 前端 chat 接入新 WS —— 留 01c
- ❌ Scheduled run 触发路径真实现 —— 留 02a 或后续；本 session 只在 INV-6 测试里 stub 一下证明 code path 存在
- ❌ user-mode MCP credential_proxy —— 留 02c；本 session INV-6 reauth 路径测试里 stub 一个 fake 401

## Open questions

1. **断开 != cancel 的语义确认**：用户关 browser tab，run 应该继续（"fire and forget"）。这是设计意图，但 UI 上有没有"用户取消" button？如果有，按 button 就走 `POST /runs/{id}/cancel`，关 tab 不走——这个确认。
2. **`awaiting_reauth` 是 terminal state 还是 paused state**（plan critique §2）：选 terminal 简化（user 重登后是新 run，client 拿历史重投）OR paused（需要 `POST /runs/{id}/resume`）？**推荐 terminal**——简单且符合 Phase 1 不接 OIDC 的现实。如果选 paused，INV-6 措辞要改。
3. **`request.run` 的 `idempotency_key`**：选客户端生成 + server 端 60s 滑窗 dedup，还是 server 端给 ack 后客户端别再重投？前者更鲁棒。

## Pitfalls

- INV-6 测试的"变异测试"必须真做：把任意一条 UPDATE 注释掉，pytest 必须红。如果有 happy path 整测试套都绿，说明覆盖有洞
- `request.cancel` 不能直接 webui 端写 status=cancelled——必须经 orchestrator（agent 容器还在跑时让 orchestrator stop 容器 + 写 UPDATE）。webui 直接 UPDATE 会让 agent 容器 ghost
- WS ticket exp ≤ 60s——ticket 比 access_token 短得多，刷新一次只用一次
- `event.run.requires_reauth` payload 必须包含 `reason`（"refresh_token_expired" / "user_revoked"），UI banner 才能 differentiated 显示
- **idempotency_key dedup 窗口**不能跨 run 边界——同 conversation 内 60s 滑窗够；不要跨 conversation 共享

## Findings (after execution)

### `awaiting_reauth` 实际实现细节

**选 terminal**（与 Open Question 2 锁定方向一致）。落地：

- `rolemesh.runs.lifecycle.TerminalStatus` Literal 直接列了
  `"awaiting_reauth"`；`_TERMINAL_STATUSES` frozenset 包含它。
- `update_run_terminal(status="awaiting_reauth", ...)` 与
  其它三种 terminal 状态共享同一条 SQL UPDATE（`WHERE
  status='running'` 不变；`completed_at = NOW()`）。
- 没有新增 `POST /runs/{id}/resume` 端点；不需要 paused state。
  User 重登后开一个**全新 run**，由 SPA 拿
  `GET /api/v1/conversations/{id}/messages` 历史回放 context。
- OpenAPI `RunStatus` enum 由原来的 `[queued, running, ...]`
  改为 `[running, completed, failed, cancelled, awaiting_reauth]`
  —— 我们的 lifecycle helper 从来不写 `queued`，留 enum 是
  误导。`tests/test_openapi_contract.py::test_run_status_enum_matches_lifecycle_terminal_set`
  把 enum 钉死。
- 错误体 `{code:"REAUTH_REQUIRED", reason: "refresh_token_expired"
  | "user_revoked"}`，`event.run.requires_reauth` 复用同样的
  `reason` 字段让 UI banner differentiated。

### 7 条终止路径的 wire 归属

落地集中在 `src/rolemesh/runs/terminators.py`——6 个命名 wrapper
都过 `update_run_terminal`，禁止直接 SQL UPDATE。每条路径的
"谁调 wrapper" 责任：

| # | 路径 | 调 wrapper 的进程 | 触发源 |
|---|---|---|---|
| 1 | WS completed | orchestrator | agent SDK `ResultMessage` → NATS `web.stream.*` "done" → orchestrator-side 收到 → `terminate_run_via_ws_completed`（**01b 提供 wrapper；orchestrator 端实际接线推到下游 session 的 NATS handler**）|
| 2 | WS error | orchestrator | agent SDK exception / NATS `safety_blocked` → `terminate_run_via_ws_error` |
| 3 | user cancel | orchestrator | webui POST/WS publish `web.run.cancel.{run_id}` → orchestrator subscriber stop 容器 + `terminate_run_via_user_cancel` |
| 4 | scheduled | scheduler（Phase 2） | scheduled job finish → `terminate_run_via_scheduled_completion(success=...)` |
| 5 | container crash | orchestrator (container monitor) | Docker / Pi runtime die event 非零退出 → `terminate_run_via_container_crash` |
| 6 | reauth | credential_proxy（02c） | 401 from token_vault → `terminate_run_via_reauth_required(reason=...)` |

**webui 端从不直接 UPDATE `runs`**——这是 01b 的硬底线。即使
是 WS request.cancel，webui handler 也只 publish 一条 NATS
`web.run.cancel.{run_id}`，orchestrator 接事件后才 UPDATE。
ghost container 风险关闭。

### `request.cancel` 经 orchestrator 的路径

- WebUI 端：
  - HTTP: `POST /api/v1/runs/{id}/cancel` 在
    `src/webui/v1/runs.py`，返 202，调用
    `webui.v1.run_events.publish_run_cancel(run_id, tenant_id,
    conversation_id)`。已 terminal 直接 409
    `code="ALREADY_TERMINAL"` 不发 NATS。
  - WS: `request.cancel` 在
    `src/webui/v1/ws_stream.py::_handle_request_cancel` 调同一个
    publisher。
- NATS subject: `web.run.cancel.{run_id}`，payload
  `{run_id, tenant_id, conversation_id}`。fit 进现有
  `web-ipc` JetStream stream（`subjects=["web.>"]`）——原 prompt
  字面 `run.cancel.{run_id}` 要新加 stream，我换成
  `web.run.cancel.{run_id}` 复用已有 stream。
- orchestrator 端订阅：留待后续 session（路径已通；wrapper
  函数 `terminate_run_via_user_cancel` 已就位）。
- "谁 stop 容器"：orchestrator 端的 NATS handler，下游 session
  连 container runtime 的 stop API。webui 永远不直接碰容器。

### idempotency dedup 实现

- 选 **in-memory dict + per-conversation `asyncio.Lock`**（不落
  DB，不挂 KV）。
- 实现：`src/webui/v1/idempotency.py` 模块单例 `cache`。
- 窗口：60 秒滑窗（per-conversation 内 key 去重；跨 conversation
  不共享）。
- 并发安全：每个 conversation 一把 `asyncio.Lock`；
  `lookup_or_remember` 在锁内调 `run_id_factory_async`，确保
  双 frame 同时到达不会双 INSERT + 双 publish。
- 重启丢一窗口：可以接受（窗口外 client 重投相当于新请求，
  正确行为）。
- `request.run` 强制要求 `idempotency_key`（缺则
  `PROTOCOL_MISSING_IDEMPOTENCY_KEY` 错；prompt 锁定）。

### 对 01c（前端 chat 接入新 WS）的影响

- 新 endpoint：`WS /api/v1/conversations/{id}/stream?ticket=<jwt>`，
  与旧 `/ws/chat?agent_id=&token=&chat_id=` 并存（旧端点不动）。
- 握手前 SPA 必须：
  1. `POST /api/v1/auth/ws-ticket {conversation_id}` → 拿
     `{ticket, expires_in_s}`。
  2. WS connect，ticket 作 query param。失败 close code
     4001/4002/4003/4004 区分错误。
- client→server frame 必填 `idempotency_key`（client 端
  `crypto.randomUUID()`）。漏了直接 `event.run.error
  code=PROTOCOL_MISSING_IDEMPOTENCY_KEY`。
- 重连约定：client 先 `GET /api/v1/runs/{id}`；
  terminal → 不订阅。server 不主动 replay；这是协议契约，没
  server 端代码支撑。
- event 名 schema：`event.run.started/token/completed/error/
  requires_reauth`，`idempotent: true/false` 标志新旧 run。
- 旧 `/ws/chat` text 协议（`{type:"text"|"thinking"|...}`）
  在 01c 完全切完之前**保留**，让回退快。

### 偏离原 prompt 的地方

- **NATS topic 名**：prompt 字面 `run.cancel.{run_id}`，实际
  用 `web.run.cancel.{run_id}`——理由如上（复用 stream）。
- **`MessageRole` enum**：prompt 没显式约束，yaml 由 4 种
  (`user/assistant/system/safety`) 收窄到 2 种
  (`user/assistant`)。`system`/`safety` 是 WS event 流的事，
  不入 persisted message。如果 02a/02b 要把 safety 决策入
  `messages` 表那时再开。
- **`engine.handle_decision` 签名改名 `action → outcome`**：
  比"只在 handler 翻译，engine 内部继续 wire enum"更彻底；
  blast radius 是 7 个 test 文件 (`sed` 替换)。值得，因为
  这才真正让 engine 内部不见 wire string。
- **schema migration（messages.conversation_id FK ON DELETE
  CASCADE）**：原 prompt 没列；DELETE conversation 的级联是
  设计 §3 "DELETE 语义" 表里写的，但 schema 历史版本没加
  CASCADE。01b 加了一个 idempotent ALTER 块（pg_constraint
  introspection）补上。这是 PR1 的隐藏血——`tests/webui/
  test_v1_conversations.py::test_delete_conversation_cascades_messages`
  钉住。

### 后续 cleanup / 留给下游 session

- **orchestrator-side 接 `web.run.cancel.*` 的 subscriber** 没
  落在 01b——01b 只 publish。下游 session（很可能 02 系列）需要
  在 orchestrator init 里注册一个 subscriber，stop 对应 agent
  容器 + 调 `terminate_run_via_user_cancel`。
- **WS handler 内的"哪个 run_id 是当前 active"逻辑**靠
  per-connection 变量，依赖"一个 conversation 同时只一个
  running run"（lifecycle helper 的 `WHERE status='running'` 已
  保证）。如果将来允许并行 run 这里要重做。
- **`event.run.tool_call` / `event.run.tool_result`**：协议设计
  里有，01b 没实现 forward 链路（orchestrator 端目前不发
  这两类 NATS 事件）。留待 agent SDK 接线时补。
- **WS endpoint 的 RLS 隔离测试** 因为 starlette TestClient + asyncpg
  pool 跨 loop 的限制走的是 `get_conversation` stub 路径——RLS
  本身在 PR1 的 REST endpoint 已有真测试覆盖；WS path 的 RLS
  最终依赖同一个 `get_conversation`，等 02c live smoke 跑实
  e2e 时一并验证。

### chore A: orchestrator-side cancel subscriber

01b 只 publish `web.run.cancel.{run_id}`，没人订阅，导致
POST `/api/v1/runs/{id}/cancel` 是"假成功"。chore A 补上订阅端：

- **Container 反查路径**：在
  `src/rolemesh/container/scheduler.py::GroupQueue` 加公开方法
  `get_active_container_name(group_jid) -> str | None`（~10 行）。
  `group_jid` 在 web 流里就是 `conversation_id`（验证于
  `src/rolemesh/main.py:920` 的 `register_process(conv.id, ...)`
  调用）。Subscriber 通过 `main.py` 闭包把 `_queue.get_active_container_name`
  注入，私有字段不外漏。
- **Subscriber 模块**：`src/rolemesh/orchestration/run_cancel_subscriber.py`
  - subject filter `web.run.cancel.>`，durable `orch-web-run-cancel`
  - `manual_ack=True`，`ConsumerConfig(ack_wait=30s, max_deliver=3)`
    —— 容器 stop 可能慢，ack_wait 默认几秒不够；max_deliver=3 防
    重试风暴
  - 失败 ack 用 `msg.nak()` 让 NATS 重投，不要 silent drop
  - Malformed payload（非 JSON / 字段缺失）当场 ack 不重投
- **顺序：先 stop 容器后 UPDATE runs**。理由：
  - stop 失败时（docker daemon 错 / 容器已不在）log warn + 继续 UPDATE，
    不让 SPA 卡在 "cancelling…" 状态
  - 已 terminal 时 lifecycle helper 的 `WHERE status='running'` 守卫
    让 UPDATE 自动 no-op
  - 注意：**已 terminal 的 run 仍然会调 runtime.stop**（防 ghost
    container；pinned test 钉住）
- **runtime.stop 失败的实际表现**：subscriber 不抛、不重投、
  状态机仍推进到 cancelled。这条由
  `test_runtime_stop_failure_still_advances_state_machine` 钉住。
- **Cancel vs Stop 按钮**（locked decision A）：
  - 现有 web Stop 按钮调 `interrupt_current_turn`——只中止本轮生成，
    容器保留（NATS `agent.{job_id}.interrupt`）
  - 新 `POST /api/v1/runs/{id}/cancel`（含 WS `request.cancel`）走
    `runtime.stop`——硬杀容器
  - 两套不同语义并存。**01c 前端必须**区分两个按钮：
    旧 Stop 按钮接旧 `/ws/chat` 的 `{type:"stop"}`，新 Cancel 按钮
    接 `POST /api/v1/runs/{id}/cancel`。**不要**把旧 Stop 按钮直接
    搬到新 endpoint——会让用户每次 Stop 都重建容器，体验回退。
- **JetStream stream 复用**：`web-ipc`（subjects `["web.>"]`）已在
  orchestrator boot 路径幂等注册（`main.py:1603` add → fallback
  update_stream），chore A 不另起 stream。
- **对 01c 的影响**：
  - 客户端 cancel 行为现在能真验收——`POST /cancel` 返 202 后
    几秒内 `GET /api/v1/runs/{id}` 可看到 `status='cancelled'` +
    `completed_at` 非 NULL
  - WS `request.cancel` 同样走通（publish 是同一个 publisher）
  - 上面的"Cancel vs Stop"区分对 SPA UX 是新约束
