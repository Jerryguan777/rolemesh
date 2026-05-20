# Session 01a — Coworkers CRUD + runs 写入责任人

| field | value |
|---|---|
| Phase | 1 |
| Prerequisites | 00a + 00b + 00c done |
| Estimated PRs | 3-4 |
| Estimated LOC | ~1200 |
| Status | not started |

## Goal

落 `/api/v1/coworkers/*` CRUD（不含 conversations/runs/messages，那部分 01b 做）+ 把 `runs` 表的"谁负责 INSERT row"问题彻底定下来并实现。**runs 表的写入责任人是 1a 必须定的决策**——01b 写 WS 协议时直接复用。

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §3 Phase 1 endpoints / §4 WS 协议（理解 runs 在端到端流程中的位置）/ §11 INV-6
2. [`docs/webui-backend-v1.1-plan.md`](../webui-backend-v1.1-plan.md) "下游 session prompt 刷新规则"
3. 00b Findings：确认 `coworkers.model_id` backfill 结果、`messages.run_id` FK 实际行为
4. `src/rolemesh/db/coworker.py` —— 现有 CRUD 模式
5. `src/webui/admin.py:440-690` —— 现有 `/api/admin/agents/*` 实现，新 v1 endpoint 要复用大量逻辑但走新 URL + 新 schema
6. `src/rolemesh/main.py:200-450`（粗位置）—— orchestrator 启动/重启 coworker 容器的链路
7. `src/rolemesh/core/backend_capabilities.py`（00a 落地的）—— 创建 coworker 时校验 model × backend 兼容

## Scope — PR breakdown

### PR 1 — `/api/v1/coworkers` GET/POST + 启动校验链

- 新建 `src/webui/v1/coworkers.py`（或 `src/webui/v1_coworkers.py` 视项目布局）
- 实现：
  - `GET /api/v1/coworkers` — 列出当前 tenant 的 coworkers
  - `POST /api/v1/coworkers` — 创建新 coworker
- POST 必须校验：
  - `model_id` 存在 + tenant 有对应 provider 的 credential（无则 `MISSING_CREDENTIAL` 422）
  - `backend × model.provider × model.family` 走 `validate_combo()`（不兼容则 `BACKEND_INCOMPAT`）
  - `name` 在 tenant 内唯一（已有约束就走 DB error 转 409）
- 复用现有 `db/coworker.py:create_coworker` —— 加 `model_id` 参数（已经 00b ALTER 加列）
- 用 typed response model + `/api/v1/backends` 一致的错误体（设计 §13）
- pinned test：`tests/test_v1_coworkers_create.py`
  - 不 mock DB（用 testcontainer）
  - 测合法创建 → 200
  - 测 missing credential → 422 + 正确 code
  - 测 incompatible backend/model → 422/400（按 00a Open question 决定）
  - 测同 tenant 重名 → 409
  - **测 RLS 隔离**：tenant A 看不到 tenant B 的 coworker（INV-1 端到端验证）

### PR 2 — `/api/v1/coworkers/{id}` GET/PATCH/DELETE

- 三个 endpoint 走同一个 `_get_coworker_or_404` helper
- PATCH 必须支持改 `model_id`，并触发 hot-reload event（设计 §7）：
  - 在 NATS 上发 `web.coworker.restart` event（topic 名按现有惯例）
  - orchestrator 已经监听这个 topic（如果没有，加一个 todo 进 Findings，留 01b 或单独 PR 补）
- DELETE 走级联（设计 §3 表格 — coworker 删除级联删 conversations/runs/messages，DB FK ON DELETE CASCADE 已配）
- pinned test：
  - GET 404 错误体
  - PATCH model_id 后能从 GET 看到 + 收到 NATS event
  - DELETE 级联（先建一个 conversation 再删 coworker，验 conversation 也删了）

### PR 3 — `runs` 表写入责任人 + lifecycle helper

**核心决策**（设计 plan critique §3）：

> **runs 行的 INSERT 必须由 trigger run 的服务在触发瞬间完成**：
> - WS 触发：webui WS handler INSERT，run_id 立刻返给客户端 + 通过 NATS forward 给 orchestrator
> - Scheduled 触发：scheduler 服务 INSERT
> - Agent 容器**不 INSERT，只 UPDATE 终态字段**（status / completed_at / usage / error）+ 写 messages.run_id

落地：

- 新建 `src/rolemesh/runs/lifecycle.py`：
  ```python
  async def create_run(
      tenant_id: str,
      conversation_id: str,
      *,
      conn: asyncpg.Connection,
  ) -> str:
      """INSERT runs row with status='running', return run_id."""

  async def update_run_terminal(
      run_id: str,
      *,
      status: Literal["completed", "failed", "cancelled", "awaiting_reauth"],
      usage: dict | None = None,
      error: dict | None = None,
      conn: asyncpg.Connection,
  ) -> None:
      """UPDATE runs row to terminal state. Idempotent + refuses to
      overwrite an existing terminal state (no resurrection)."""
  ```
- `update_run_terminal` 必须实现"terminal state 不可回写"规则——SQL `UPDATE ... WHERE status = 'running'`，affected_rows = 0 时不抛但 log warning（race 时正常）
- `messages.run_id` 写入：找到现有 message INSERT 路径（grep `INSERT INTO messages`），加 `run_id` 参数；对于没有 run 上下文的旧路径（如 system message）走 NULL
- pinned test：`tests/test_run_lifecycle.py`
  - 测正常 create → update_completed → status 是 completed
  - 测 create → update_completed → update_cancelled（race）→ 第二次 UPDATE affected_rows=0，status 还是 completed（不回写）
  - 测 create → update_failed → 同样不可回写
  - 测 `update_run_terminal(run_id="nonexistent")` 不抛但 affected_rows=0
  - **未覆盖的终止路径打 TODO 标签**（INV-6 真正完整在 01b 才能 close）

### PR 4 (可能合并进 1 或单独) — `/api/v1/auth/config` + `/api/v1/auth/ws-ticket` + `/api/v1/me`

WS 握手前置（设计 §3 / §4）。01b WS 实现依赖这三个 endpoint。

- `/api/v1/auth/config` — 返 auth mode + bootstrap 模式标识（不要泄露 token 字符串）
- `/api/v1/auth/ws-ticket` — POST，返 短期 JWT（exp ≤ 60s），payload 含 `user_id / tenant_id / conversation_id`
- `/api/v1/me` — 返当前 authenticated user 信息
- Ticket 必须签名（用现有 JWT key 或新生成一个 ws-only secret）
- pinned test：
  - bootstrap user 拿 ticket → 解码后 sub 是 bootstrap user
  - 多 user map alice 的 token → 拿到的 ticket sub 是 alice 真 UUID
  - 过期 ticket 不能用（01b 验证 verify 路径，本 session 至少测 issue 端 exp 设置）

## Acceptance criteria（session 级）

- [ ] `pytest tests/test_v1_coworkers_create.py tests/test_v1_coworkers_crud.py tests/test_run_lifecycle.py tests/test_v1_auth_endpoints.py` 全绿
- [ ] `/api/v1/coworkers` 全 CRUD 走通；NATS hot-reload event 发出
- [ ] `runs.lifecycle` API 暴露给 01b 使用
- [ ] 现有 `/api/admin/agents/*` 不退化（v1 不替换 admin，并存）
- [ ] OpenAPI yaml 更新覆盖新 endpoint；codegen 通过
- [ ] 全套测试通过
- [ ] 更新 plan 状态

## Out of scope

- ❌ Conversations / messages CRUD —— 留 01b
- ❌ WS 协议实现 —— 留 01b
- ❌ run status 在 WS 完成 / cancel / error 时的真实 UPDATE —— 这是 01b 的事；本 session 只准备 lifecycle helper
- ❌ Coworker UI 列表/创建向导 —— 留 01c

## Open questions

1. **`POST /api/v1/auth/ws-ticket` 的 body**：要不要 conversation_id？还是 ticket 不绑 conversation，WS 握手时再校验？前者更严格，后者更灵活。推荐前者——ticket 绑死 conversation_id 后，握手期不用再查 DB。
2. **`web.coworker.restart` NATS topic 是否已存在**？grep 现有 orchestrator 代码看，如果没有，本 session 加一个监听端 + 重启逻辑（不在 scope 但工作量小）还是留 TODO？
3. **`messages.run_id` backfill**：现有 messages 都没 run_id。要不要写一个 backfill script 把它们关联到一个"legacy run" sentinel？还是 NULL 即 legacy，后续 query 接受 NULL？推荐后者，简单。

## Pitfalls

- `create_run` 必须在 `conn.transaction()` 内调用，且要在 NATS publish 前 commit—— 否则 orchestrator 收到事件去查 DB 看不到这条 run
- `update_run_terminal` 的"不回写 terminal"规则 SQL 不能漏；如果只检查 affected_rows 而不带 `WHERE status = 'running'`，race 时会把 completed 改成 cancelled
- v1 endpoint 不要 import admin.py 的 helper（admin 用 6 个月后会删，v1 应独立）；复用就提取到 `rolemesh/coworker_service.py` 类的共享层
- backend 兼容校验在 POST 和 PATCH 都要做（PATCH 改 model_id 时旧 backend 可能不再兼容）
- WS ticket 不要复用普通 JWT secret——用独立 secret，泄露面积小

## Findings (after execution)

_(empty — 重点记录：ws-ticket 是否绑 conversation_id？NATS hot-reload topic 现状？run lifecycle helper 实际签名？)_
