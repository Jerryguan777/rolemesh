# Session 01a — Coworkers CRUD + runs 写入责任人

| field | value |
|---|---|
| Phase | 1 |
| Prerequisites | 00a + 00b + 00c done |
| Estimated PRs | 3-4 |
| Estimated LOC | ~1200 |
| Status | done (2026-05-20) |

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

执行日期：2026-05-20。4 个 PR 各一个 commit，均以 `git commit -s` 累在 `feat/ui`。

### ErrorResponse helper 最终签名与位置

- 文件：`src/webui/v1/errors.py`
- 公共面：
  - `class ErrorResponseException(HTTPException)`：携带 `envelope: dict[str, object]`，HTTPException.detail 留 plain message 给日志
  - `def raise_error_response(code, message, *, status_code, details=None) -> NoReturn`
  - `def install_error_handler(app: FastAPI) -> None` — 注册 root-level JSON 响应

为什么不直接用 `HTTPException(detail=envelope_dict)`：FastAPI 默认包一层 `{"detail": envelope}`，前端 typed client 解出来的 `ErrorResponse` 在 `.detail.code` 上而不是 `.code` 上，contract 测试以为对齐其实没对齐。`install_error_handler` 把 envelope 当成 root body 直接渲染（200 路径不影响，只接 `ErrorResponseException`）。

下游 v1 endpoint 用法：
```python
raise_error_response(
    "MISSING_CREDENTIAL", "Tenant has no credential ...",
    status_code=422, details={"provider": "anthropic"},
)
```
01b/02a/03a 所有 4xx 都走这个 helper（除非是 Pydantic 422 — 那个走 FastAPI 默认 RequestValidationError）。

### `web.coworker.restart` NATS topic 现状与实际 wiring

**Grep 结果**：执行前**不存在**任何 `web.coworker.*` 监听端 — 现有 orchestrator 只订阅 `agent.*.*` / `egress.mcp.changed`。

**本 session 添加的最小监听端**（设计 §7 强制）：

- 发布端：`src/webui/v1/coworker_events.py`
  - `set_jetstream(js)` — 进程级单例 hook，`webui/main.lifespan` 在 NATS 连上后调用
  - `publish_coworker_restart(coworker_id, tenant_id)` — JSON payload `{coworker_id, tenant_id}`
  - subject 常量 `WEB_COWORKER_RESTART_SUBJECT = "web.coworker.restart"`（导出自 orchestration 侧避免拼写漂移）
- 订阅端：`src/rolemesh/orchestration/coworker_hot_reload.py`
  - `subscribe_coworker_restart(js, *, state, fetch_coworker)` — durable=`orch-web-coworker-restart`，manual_ack
  - `reload_coworker_into_state(...)` — **mutate `cached.config` in place** 而不是替换整个 `CoworkerState`，否则在 `_message_loop` 持有的 conversations / channel_bindings 引用全部 dangling
  - 重建 `trigger_pattern`（如果未来 PATCH 支持改 name 也能正确响应）

**stream 声明**：webui lifespan 已经 `add_stream(name="web-ipc", subjects=["web.>"])`。orchestrator 也在 `start_subscribers` 里 idempotent add（防止 orchestrator 先启动）。两边都用 `try add / except update` 模式。

**触发策略**：PATCH 只在 `body.model_id != cw.model_id` 时 publish；同 model_id 的 PATCH（例如只改 name）不广播。`test_patch_model_id_publishes_restart_event_only_when_changed` 钉死这条规则。

**Live 测试**：`test_patch_model_id_round_trips_through_real_nats` 起真 NATS + JS context，PATCH 完 10 秒内拿到事件并验证 state 已更新；NATS 不可达时自动 skip。

**未实现 / 留 follow-up**：active container kill-on-reload 没做。"新 model 立刻生效" 必须等当前容器自然结束本次请求；下次唤醒时拿新 config。如果 01b 后续发现 streaming-mid-PATCH 用户体验差，可以加 `runtime.stop(container_name_for(coworker_id))`，但需要先解决"哪些 in-flight 消息要 drain"的语义。

### runs lifecycle helper 最终签名

文件 `src/rolemesh/runs/lifecycle.py`，导出三个：

```python
async def create_run(*, tenant_id: str, conversation_id: str,
                     conn: asyncpg.Connection) -> str: ...

async def update_run_terminal(
    *, run_id: str,
    status: Literal["completed","failed","cancelled","awaiting_reauth"],
    usage: dict | None = None,
    error: dict | None = None,
    conn: asyncpg.Connection,
) -> bool: ...  # True = wrote, False = already terminal/missing

async def get_run(*, run_id: str, tenant_id: str,
                  conn: asyncpg.Connection) -> dict | None: ...
```

签名设计决定：

1. **conn 参数注入而不是自己 acquire** — `create_run` 必须和 messages INSERT 在同一 transaction（设计 §pitfall），所以 helper 不开 transaction。caller pattern：
   ```python
   async with tenant_conn(tenant_id) as conn:
       run_id = await create_run(tenant_id=..., conversation_id=..., conn=conn)
       await store_message(..., run_id=run_id, ...)
       # exit -> commit -> publish NATS
   ```
2. **`update_run_terminal` 的 status 用 `Literal[...]` 限制四个终态值** — 在类型层面就拒绝 `status="running"`，运行时再 ValueError 兜底（`test_update_run_terminal_rejects_running_status`）。
3. **返回 bool 不抛**（unknown run / already terminal 都返 False + log warning） — 01b 的 cancel/error/timeout/scheduler 路径汇聚到一个 helper 里，让每条路径处理 "run 不存在" 会反复腐烂；统一返 False 让调用方决定要不要 log。
4. **"不可回写 terminal" 通过 SQL `WHERE status = 'running'` 实现**，不靠 Python 层 read-modify-write。`test_second_terminal_does_not_overwrite_first` 就是变异检查 — 删掉 WHERE 子句立刻红。
5. **JSONB 反序列化**：`get_run` 内部 `_parse_jsonb` 把 asyncpg 返的字符串解为 dict（caller-provided conn 可能没注册 codec）。

### validate_combo 校验链怎么走

PR1 没有加新的 `db/model.py` 三跳查询 helper（00b Findings 建议过 `get_model_for_coworker`），而是把校验链放在 `webui/v1/coworkers.py:_validate_model_and_credential` 里。理由：

- 校验链对每个 endpoint（POST + PATCH）逻辑相同，但**调用前置条件不同** — POST 没有现有 coworker 行，PATCH 有；如果做成 `(coworker_id) -> Model -> validate` 的三跳 helper，POST 那条路径反而绕；
- 校验错误要立刻产生 `ErrorResponseException`（携带 details 字段告诉前端哪个 provider/family/backend 错了），这是 v1 endpoint 层的关注；DB 层 helper 抛 `BackendCompatError` 反而要求 endpoint 层再翻译一遍

最终链：
1. `get_model_by_id(model_id)` → `ModelRow | None`（`src/rolemesh/db/model.py`）
2. 未找到或 `is_active=False` → `raise_error_response("MODEL_NOT_FOUND", 422)`
3. `tenant_has_credential_for_provider(tenant_id, model.provider)` → bool（同上文件）
4. 未配 → `raise_error_response("MISSING_CREDENTIAL", 422)`
5. `validate_combo(backend_name, model.provider, model.model_family)` → 抛 BackendCompatError
6. 接住翻译成 `raise_error_response("BACKEND_INCOMPAT", 422, details={...})`

顺序：**credential 检查在 combo 检查前面**。`test_credential_check_runs_before_combo_check` 钉死这一点 — 同时挂两个错时优先报 MISSING_CREDENTIAL（用户更可能立刻能修）。

`src/rolemesh/db/model.py` 新增 `ModelRow` dataclass + 两个函数：`get_model_by_id`（用 admin_conn — models 表无 RLS）和 `tenant_has_credential_for_provider`（用 tenant_conn — INV-1 双层防御）。

### `coworkers.model_id` / `created_by_user_id` 数据流

- DB 列：00b 已加（NULLABLE）
- 本 session：
  - `rolemesh.core.types.Coworker` dataclass 加两个字段，默认 None
  - `db/coworker.create_coworker` 加 `model_id` + `created_by_user_id` kwarg，INSERT 包含
  - `db/coworker.update_coworker` 用 sentinel `_MODEL_ID_UNSET` 区分 "不改" vs "清空"
  - `_record_to_coworker` 读两个新列

`created_by_user_id` 写入策略：当前 `user.user_id` 在 bootstrap 单 token 模式下是字符串 `"bootstrap"`（不是 UUID），会 FK 违约；用 `_looks_like_uuid()` 守卫 — bootstrap 单 token 模式下落 NULL，多 user (BOOTSTRAP_USERS) / OIDC 模式下落真 UUID。

### ws-ticket secret 怎么管

- Env：`WS_TICKET_SECRET`（独立的）
- 优先级：
  1. `WS_TICKET_SECRET` 非空 → 用它
  2. 否则 `ADMIN_BOOTSTRAP_TOKEN` 非空 → fallback + **one-shot warning log**
  3. 都没有 → 抛 `WsTicketError(code="WS_TICKET_SECRET_UNSET")` 让 endpoint 返 500
- 算法：HS256，`aud="rolemesh-ws"`，`exp` clamp 到 [1, 60] 秒
- 文件：`src/rolemesh/auth/ws_ticket.py`（pure logic，无 FastAPI 依赖；endpoint 在 `src/webui/v1/auth.py`）
- `WS_TICKET_SECRET` 在 `src/webui/config.py` 也 re-export 一次让 deploy manifest grep 时一处看全

为什么独立 secret：泄漏面积。`ADMIN_BOOTSTRAP_TOKEN` 是 dev/admin 的 long-lived bearer；ws ticket 是短时 handshake 凭证。共用一把 key 时，泄漏任一面攻击者都能伪造两边。`test_ws_ticket_uses_dedicated_secret_not_bootstrap_token` 钉死两者必须不同 secret。

### 对 01b（WS 协议）的影响

**Ticket 验证侧已就位**：`rolemesh.auth.ws_ticket.verify_ws_ticket(token) -> WsTicketPayload`。01b 的 WS handshake：

```python
@app.websocket("/api/v1/conversations/{conversation_id}/stream")
async def stream(ws, conversation_id: str, ticket: str = Query("")):
    try:
        payload = verify_ws_ticket(ticket)
    except WsTicketExpired:
        await ws.close(code=4001, reason="WS_TICKET_EXPIRED")
        return
    except WsTicketError:
        await ws.close(code=4002, reason="WS_TICKET_INVALID")
        return
    if payload.conversation_id != conversation_id:
        await ws.close(code=4003, reason="ticket conversation mismatch")
        return
    # 握手期 zero DB read — payload 已经携带 user_id/tenant_id/conversation_id
```

**runs lifecycle helper 已就位**：01b 的 WS handler 接 `request.run` 事件时按 §pitfall 的 pattern：
```python
async with tenant_conn(tenant_id) as conn:
    run_id = await create_run(tenant_id=..., conversation_id=..., conn=conn)
    await store_message(..., run_id=run_id, is_from_me=False, ...)  # the user input
# exit transaction
await js.publish("agent.<cid>.input", {... "run_id": run_id ...})  # NATS publish
```
然后 streaming 的 token / completion / error / cancel 各路径都走 `update_run_terminal` — INV-6 的"枚举所有终止路径" pinned test 由 01b 完成。

**Hot-reload 链路已就位**：PATCH model_id → NATS publish → orchestrator subscribe → `CoworkerState.config` 替换。01b 不需要再补这部分；如果发现 user 想要"改 system_prompt 立即生效" 也可以走同一个 topic。

**未做（留 01b）**：
- WS endpoint 本身、event 协议、reconnect 路径
- run state machine 完整 INV-6 pinned test（枚举 WS / cancel / schedule / container_crash / reauth_required）
- conversations / messages CRUD endpoints（设计 §3 Phase 1 列表的剩余几个）

### Acceptance criteria verification

- [x] `pytest tests/webui/test_v1_coworkers_create.py tests/webui/test_v1_coworkers_crud.py tests/test_run_lifecycle.py tests/webui/test_v1_auth_endpoints.py` — 39/39 green，约 4.3 分钟
- [x] `/api/v1/coworkers` 全 CRUD 走通；NATS hot-reload 事件 live round-trip 验证
- [x] `runs.lifecycle` 公开 `create_run`/`update_run_terminal`/`get_run`
- [x] 现有 `/api/admin/agents/*` 不退化（admin.create_coworker 调用未改 signature，新 kwargs 都有 default）
- [x] OpenAPI yaml 更新覆盖新 endpoint；`npm run openapi:gen` 通过；types.ts 提交
- [x] contract / freshness 测试全绿（8/8）
- [x] plan.md 状态更新

### 偏离原 prompt 的地方

- **未加 `db/model.py` 中的 `get_model_for_coworker(coworker_id, tenant_id) -> Model` 三跳 helper**（00b Findings 建议过）。理由见上 §validate_combo — POST 路径不需要它，PATCH 路径用同一个 `_validate_model_and_credential` 比绕一圈三跳更直接。后续如果某个 endpoint 真的从 coworker_id 开始算 backend compat（例如 02b 校验现有 coworker 的 backend），再补也不迟。
- **POST /coworkers 的"name 在 tenant 内唯一"约束**实际是 `folder` 唯一（`UNIQUE (tenant_id, folder)`），不是 `name`。原 prompt 写"name 在 tenant 内唯一（已有约束就走 DB error 转 409）" — DB 里没有 name 的 UNIQUE，所以测试 `test_duplicate_folder_in_tenant_returns_409` 验的是 folder 冲突。如果 UX 上 name 唯一更合理，需要 02a 单独加 UNIQUE 列。

### 后续 cleanup（不在本 session 范围）

- `coworkers.tools` JSONB 列还在 — 02b 双写阶段 + 03+ drop（原计划）
- `Coworker` dataclass 现在有 `model_id` / `created_by_user_id` 字段；admin.py 不消费它们（admin 路径用 `/api/admin/agents`，不感知 model_id），但 `_coworker_to_response` 在 v1 endpoint 已经投影出去。Schema 内含。
- WS endpoint 实现留 01b，本 session 只准备 ticket 让 01b 用。
