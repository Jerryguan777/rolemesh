# Session 02c — credential_proxy user-mode + fake-vault e2e  `[REFRESHED 2026-05-21]`

| field | value |
|---|---|
| Phase | 2 |
| Prerequisites | 02a + 02b done |
| Estimated PRs | 3 |
| Estimated LOC | ~1000（去掉 exchange_for stub 后微调） |
| Status | not started |

> **Refresh 起源**：02a/02b 落地后大幅刷新了上下文（CredentialVault primitive 已就位、`list_coworker_mcp_configs` helper 可用、`subscribe_coworker_mcp_changed` 已 wire）。同时把原计划的 `exchange_for(audience)` stub **cut**——与 02a 同样的 "no-caller 反 over-engineering" 决策（详见下 Open Questions）。
>
> **本 session 落地 plan critique §1 重点**：不等真 Keycloak 来就把 `auth_mode=user` MCP 注入链路 e2e 通过。设计文档 §5.2.2 写"e2e 推迟到 OIDC 分支"——这个 session 反对那个简化，加 **bootstrap-friendly fake-vault e2e** 保证 wiring 真打通，避免 OIDC 分支合入那天才发现链路漏 hop。

## Goal

1. credential_proxy 在 MCP 出站路径接 `auth_mode=user`：通过 `X-RoleMesh-Conversation-Id` header 查 conversation → user_id → TokenVault → 注入 `Authorization: Bearer`
2. fake-vault dev mode：`BOOTSTRAP_USERS` spec 加可选 `static_access_token` / `force_refresh_fail` 字段，绕开真 IdP 让 OAuth-less dev e2e 跑通
3. credential_proxy 401 路径 wire 到 `terminate_run_via_reauth_required`（01b 落地的 terminator wrapper）：真触发 `runs.status='awaiting_reauth'` + WS `event.run.requires_reauth` 推给前端

## 概念澄清（避免混淆）

| Vault | 用途 | 用在哪 |
|---|---|---|
| **`TokenVault`** (`src/rolemesh/auth/token_vault.py`) | OIDC user refresh/access token | **本 session 主角** —— `auth_mode=user` MCP 注入用 |
| `CredentialVault` (`src/rolemesh/auth/credential_vault.py`，02a 落地) | LLM provider API key (Anthropic / OpenAI / ...) | 与本 session 无关——`auth_mode=service` 路径 02a 已用 |

两个 vault 共享 `derive_fernet_key` helper（02a 抽出）但完全独立——不要在本 session 把它们混用。`auth_mode=user` MCP **绝不**从 CredentialVault 取数据。

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §5.3 / §5.4（user-mode 链路 + 失败模式）/ §5.5（exchange_for 推迟，仅作历史参考）
2. plan critique §1（OIDC wiring 兜底原始论证）
3. `src/rolemesh/auth/token_vault.py` —— TokenVault 接口；本 session **不改其行为**，只在 credential_proxy 那端消费
4. `src/rolemesh/auth/bootstrap_users.py` —— 02a `BOOTSTRAP_USERS` 解析逻辑；fake-vault 字段扩展加在这里
5. `src/rolemesh/egress/` —— credential_proxy / forward_proxy / mcp_cache 实现
6. **02a Findings**：`mcp_servers.auth_mode` API 必填 + DB DEFAULT 'service' 双层防御；`auth_mode` 三值 `user / service / both`
7. **02b Findings**：`list_coworker_mcp_configs(coworker_id, tenant_id)` helper + `subscribe_coworker_mcp_changed` 已 wire；credential_proxy 想知道某 coworker 的 MCP 配置时走这个 helper
8. **01b 落地的 terminator wrappers** (`src/rolemesh/runs/terminators.py`)：特别是 `terminate_run_via_reauth_required(run_id, *, reason)` —— 本 session 的 401 路径调它
9. **01b + chore A 的 cancel pattern** (`src/rolemesh/orchestration/run_cancel_subscriber.py`)：reauth 路径 NATS event 怎么走可以照搬此模式

## Scope — PR breakdown

### PR 1 — IPC `X-RoleMesh-Conversation-Id` header + credential_proxy user-mode 反查

**Coworker 容器出站 MCP 请求必带 conversation_id header**：

- Pi runtime 注入位置：grep `src/pi/mcp/` 找 MCP client 出站 hook（应该有个 `request_middleware` 之类的扩展点）
- Claude Agent SDK 注入位置：现有 MCP transport 应该在 IPC 层就 inject——grep `mcp_transport` / `MCP_BASE_URL` 在 `src/rolemesh/ipc/` 找入口
- Header 名常量定义在一处：`src/rolemesh/egress/headers.py::ROLEMESH_CONVERSATION_ID_HEADER = "X-RoleMesh-Conversation-Id"`，两个 backend 共享 import
- 不要把 user_id 也塞 header——credential_proxy 自己查 conversation 反推 user_id，避免容器内伪造身份

**credential_proxy 拦截 + 注入**：

- 现有 credential_proxy 已经处理 `auth_mode=service`（02a 用 CredentialVault 拿真 key）
- 加 `auth_mode=user` 分支：
  1. 拿 `X-RoleMesh-Conversation-Id` header（无则 reject 400 `MISSING_CONVERSATION_ID`）
  2. 查 `conversations` 表拿 `user_id` + `tenant_id`（用 `admin_conn` 即可——proxy 是系统级 caller，不应受 RLS 限制；但 SELECT 要带显式 `WHERE tenant_id` 仍然满足 INV-1）
  3. 查 `TokenVault.get_access_token(user_id)` 拿真 token
  4. 注入 `Authorization: Bearer <token>` + 转发 upstream MCP
- `auth_mode=both`：优先 user token，**无 user token / vault 返 None 时** fallback `auth_mode=service` 路径
- 决策必须**每次请求 lookup**——`mcp_servers.auth_mode` 改动后立即生效（02a 已发 `egress.mcp.changed` event，proxy 已订阅）

**pinned test** (`tests/egress/test_credential_proxy_user_mode.py`)：

- happy path：注入 conversation → user → token → upstream MCP 收到正确 Bearer
- 缺 header → 400 `MISSING_CONVERSATION_ID`
- conversation 不存在 / user_id 为 NULL（system message 类）→ 400 + 结构化 error
- `auth_mode=both` + user token 存在 → 用 user token
- `auth_mode=both` + user token 缺 → fallback service
- mock TokenVault + mock upstream MCP——不要 mock conversation 查询（用真 testcontainer）

### PR 2 — Fake-vault dev mode（扩展 BOOTSTRAP_USERS）

**Background**：测 `auth_mode=user` 需要"用户登录后 vault 里有 token"。不接真 IdP 就要伪造这一步。Fake-vault 是 dev-only 兜底——`BOOTSTRAP_USERS` spec 加可选字段，token 落在 in-memory dict 而不是真 vault。

**spec schema 扩展**：

```jsonc
[
  {
    "token": "tok-alice",
    "user_id": "alice",
    "tenant": "default",
    "role": "owner",
    // 新增 (可选)
    "static_access_token": "fake-mcp-bearer-for-alice",
    "force_refresh_fail": false   // 设为 true 时 vault.get_access_token 返 None
  }
]
```

**实现**：

- 新建 `src/rolemesh/auth/fake_vault.py`：
  - `class FakeTokenVault` 实现 `TokenVault` 的相同接口（duck typing）
  - `get_access_token(user_id)`：从 spec dict 查 `static_access_token`；`force_refresh_fail=true` 时直接返 None
  - 没有真的 OAuth flow / refresh / HTTP——纯内存
- env `FAKE_VAULT=1` 时 webui + orchestrator startup hook 用 `FakeTokenVault` 替代 `TokenVault`（factory 模式）
- **生产代码绝不引用** `fake_vault` 模块：用 env flag + factory 隔离；CI lint 加一条 grep `from rolemesh.auth.fake_vault import` 在非 test 代码里禁止
- `bootstrap_users.py` 解析时把新字段一起 parse 进 spec dataclass（unknown_filter 已经允许 forward-compat，但显式加字段更清晰）

**pinned test** (`tests/auth/test_fake_vault.py`)：

- 注入 static token → `FakeTokenVault.get_access_token("alice")` 返预期值
- `force_refresh_fail=true` → 返 None
- 与真 `TokenVault` 接口签名严格一致（用 `inspect.signature` 比对）
- `FAKE_VAULT=0` / unset 时 factory 返真 `TokenVault`（不污染生产 path）

### PR 3 — Fake-vault e2e + reauth 真触发

**Mock MCP server**：

- **先 grep** `tests/` + `src/` 看有没有现成的 mock MCP（fastmcp / 手写 stub）—— Phase 1 测过 MCP 路径，可能有现成的
- 没有的话最简实现：~50 LOC `aiohttp` server，handle one tool (`echo`)，验 `Authorization: Bearer <expected>` header
- 启动用动态端口（`socket.bind(('', 0))`），把 URL 通过 env 传给 e2e 脚本

**E2E 脚本** (`scripts/smoke_user_mode_mcp.sh`)：

```bash
# 1. 起 mock MCP server (dynamic port → MOCK_MCP_URL env)
# 2. 配 BOOTSTRAP_USERS 含 alice + static_access_token="alice-fake-bearer"
# 3. FAKE_VAULT=1 起 webui + orchestrator
# 4. 创建 mcp_server: name=alice-mcp, url=$MOCK_MCP_URL, auth_mode=user
# 5. 绑 coworker → 这个 mcp_server (via /api/v1/coworkers/{id}/mcp-servers)
# 6. alice 起 chat → "Please call the echo tool with 'hi'"
# 7. 验：mock MCP server log 显示 Bearer alice-fake-bearer + echo 返回
# 8. 验：runs.status='completed'，token stream 含 echo 响应
```

**Reauth 路径真触发**：

```bash
# 续上面 e2e
# 9. PATCH bootstrap user spec → force_refresh_fail=true (重启 webui or runtime toggle)
# 10. alice 再起 chat → coworker 调 mcp tool → 401
# 11. credential_proxy 401 处理：
#     - 查 conversation → run_id（当前正在跑的 run）
#     - 调 terminate_run_via_reauth_required(run_id, reason="refresh_token_expired")
#     - WS event.run.requires_reauth 推到前端
#     - 前端 reauth banner 显示
# 12. 验：runs.status='awaiting_reauth'，event 真推送
```

**INV-6 路径 7 升级**：从 01b 的 stub 触发（fake 401 注入 helper）升级到**真触发**——credential_proxy 真碰到 upstream 401 走到 terminator 调用。`tests/runs/test_run_state_machine_all_paths.py` 中 path 7 测试可以保留 stub 版（独立单测），e2e 验证由 `smoke_user_mode_mcp.sh` 跑。

**Run_id 查找机制**（PR 1 + PR 3 共用）：

credential_proxy 拿到 conversation_id 后需要找当前正在跑的 run。两个选择：

- A. 查 `SELECT id FROM runs WHERE conversation_id = $1 AND status = 'running' ORDER BY started_at DESC LIMIT 1`
- B. NATS event：proxy 发 `egress.reauth_required.{conversation_id}`，orchestrator 端 subscriber 查 run_id + 调 terminator

推荐 A —— credential_proxy 已在 orchestrator 进程内（有 DB pool）；NATS round-trip 增加延迟没必要。如果发现 race（cancel + reauth 同时发生），terminator 的 `WHERE status='running'` 守卫自动 no-op，安全。

## Acceptance criteria

- [ ] credential_proxy `auth_mode=user` wiring 全链路工作（PR 1 pinned test 全绿）
- [ ] fake-vault dev mode 可启停，生产 path 不污染（PR 2 pinned test + lint）
- [ ] `scripts/smoke_user_mode_mcp.sh` 通过（PR 3 端到端）
- [ ] INV-6 路径 7 在真触发场景下 `status='awaiting_reauth'` + completed_at + reason 都写
- [ ] WS `event.run.requires_reauth` 真推送到前端（chat-panel 的 `rm-reauth-required` window event 触发；01c 已挂监听）
- [ ] `mcp_servers.auth_mode` 改动后下一个请求立即生效（不 cache）
- [ ] 更新 plan 状态

## Out of scope

- ❌ **`exchange_for(audience)` stub** —— 与 02a 时 cut rotation 同样的理由：v1.1 范围内 0 caller，未来 D3 prod 实现时按真需求定接口比凭空 stub 准；TokenVault 加无人调用的 method 是 over-engineering（详见下 Open Questions）
- ❌ 真 IdP 集成（Keycloak / Okta）—— 推迟到 OIDC 分支
- ❌ refresh token 真正自动 refresh 逻辑（fake-vault 不实现真 OAuth flow——D1 dev 行为）
- ❌ Audience-bound token（D2 / D3 方案）—— vault.exchange_for 一起推迟
- ❌ Scheduled run + user-mode MCP 拒绝路径——设计 §5.4 提到的 `NEEDS_USER_PRESENCE`，留到 Phase 2/3 真接 scheduler 时做
- ❌ WS disconnect-mid-turn 的 reauth path——Phase 1 smoke 发现 INV-6 happy-path 也有这个问题，proper fix 需要 orchestrator-side durable consumer，不在 02c 范围

## Open questions

仍需 session 内决策的：

1. **`exchange_for(audience)` stub 是否真要 cut**：我倾向 cut（refresh 时已默认 cut）——与 02a 时 cut rotation 一致的逻辑：no caller = 凭空设计接口比按真需求设计差。如果 session 跑过程中发现有真 caller（不太可能），再决定加。**这条已在 prompt 锁定为 cut，session 不要回头加**
2. **Mock MCP server 实现**：先 grep `tests/` + `src/` 找现有 mock；没有再决定 fastmcp / 手写 aiohttp / Node SDK。手写 aiohttp 应该最快（~50 LOC）
3. **`FAKE_VAULT` env 名**：vs 别的（如 `ROLEMESH_FAKE_VAULT` 更带 prefix）——session 内决定，记 Findings
4. **`force_refresh_fail` 触发方式**：spec 静态字段 vs runtime API toggle。**已锁定**：spec 静态字段（重启 webui 切换），runtime toggle 增加测试代码复杂度无价值
5. **Reauth 后端是否同时杀容器**：当前设计只 UPDATE status + 推 WS event。容器继续跑（下次 MCP 调用还会 401）。是否要像 cancel 一样 stop 容器？**默认不杀**（避免与 user 显式 cancel 语义混淆），但 session 可以决定加一个轻量"暂停接收新 turn" 的 flag

## Pitfalls

- **TokenVault ≠ CredentialVault**：本 session 用前者（OIDC user tokens），与 02a 的 LLM provider key vault 是两套——绝不混用
- **credential_proxy `auth_mode` lookup 必须每次请求**：`mcp_servers.auth_mode` 改动后立即生效；不要在 proxy 内部 cache 配置。已有 `egress.mcp.changed` event 维护内存 registry
- **Fake-vault 必须生产代码绝不引用**：env flag + factory 隔离 + CI lint
- **Mock MCP server 端口冲突**：动态分配，通过 env 传 URL
- **INV-6 路径 7 终态写入由谁负责**：credential_proxy 在 orchestrator 进程，直接调 `terminate_run_via_reauth_required(run_id, ...)`。**不要**在 coworker 容器内写 status——容器死了状态丢
- **`auth_mode=both` 的 fallback 顺序**：user → service。反过来会让本来配了 user token 的请求被 service token 覆盖
- **conversation_id header 信任问题**：容器内 agent 可以伪造任意 header。credential_proxy 只能信"这个 conversation 的当前 active container"——所以反查时**必须验** header 中的 conversation_id 真对应当前正在跑的 run 的容器。否则容器 A 可以用 conversation B 的 user_token。具体校验机制：proxy 查 `conversations` + `runs.status='running'` + 比对 orchestrator 端 active container 映射（chore A 已有的 `get_active_container_name`）
- **run_id 查找的 race**：如果 conversation 同时有两个 running run（不应该但理论上），proxy 用"最新 started_at"作 tie-break，与 INV-6 状态机一致

## 执行前刷新清单

- [ ] 02a + 02b 完成？（plan.md 显示 done）
- [ ] 现有 mock MCP server 实现确认（grep tests/ + src/）
- [ ] OIDC TokenVault env var 名 + 接口签名最近有改动吗？（02a 升 derive_fernet_key 时只改实现不破接口；最好再确认）
- [ ] `auth_mode=both` 实际是否有用例？如果项目内一直只用 user 或 service，可以推迟 both 路径到真需要时

## Findings (after execution)

_(empty — 重点记录：mock MCP 选型、conversation_id header 信任验证机制、reauth 是否杀容器、对 Keycloak 分支合入时的迁移路径)_
