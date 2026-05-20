# Session 02c — credential_proxy user-mode + fake-vault e2e  `[DRAFT]`

| field | value |
|---|---|
| Phase | 2 |
| Prerequisites | 02a + 02b done |
| Estimated PRs | 3 |
| Estimated LOC | ~1100 |
| Status | not started — DRAFT |

> **DRAFT**：本 session 落地 plan critique §1 重点关注的"OIDC wiring 兜底"。
> 设计文档说 `auth_mode=user` MCP 路径只单测覆盖 e2e 推迟到 OIDC 分支——这个 session 反对那个简化，加一条**bootstrap-friendly fake-vault e2e**，保证 wiring 在 Keycloak 来之前就有 e2e 覆盖。

## Goal

1. credential_proxy 在 MCP 出站路径接 `auth_mode=user`：通过 `X-RoleMesh-Conversation-Id` header 查 conversation → user_id → vault → 注入 Bearer
2. token_vault 加 `exchange_for(audience)` stub（设计 §5.5 D3 接口预留）
3. 加 fake-vault dev mode：`BOOTSTRAP_USERS` 里的 user 可以注入静态 access_token，跑通完整 5 跳链路 e2e
4. `event.run.requires_reauth` 路径在 fake-vault 强制返 401 时能真触发

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §5.3 / §5.4 / §5.5
2. plan critique §1 / §2
3. `src/rolemesh/auth/token_vault.py`、`src/rolemesh/auth/oidc/*`、`src/rolemesh/egress/`
4. 02a Findings —— `mcp_servers.auth_mode` 字段使用
5. 02b Findings —— `coworker_mcp_servers` 是否带 auth_mode 覆盖

## Scope — PR sketch

### PR 1 — IPC 加 `X-RoleMesh-Conversation-Id` header + credential_proxy 反查

- Coworker 容器出站 MCP 调用必带 header（Pi / Claude SDK 各自的位置）
- credential_proxy 拦截 → 查 `conversations` → 拿 `user_id` → 查 vault → 注入 `Authorization: Bearer`
- `auth_mode=service` 路径不变
- `auth_mode=both`：优先 user token，无则 fallback service
- pinned test：单测 mock vault + mock MCP，验 header 正确注入

### PR 2 — token_vault 加 `exchange_for(audience)` stub + fake-vault dev mode

- vault 接口加：
  ```python
  async def exchange_for(self, user_id: str, audience: str) -> str:
      """RFC 8693 token exchange. D3 prod; stub now."""
      ...
  ```
  stub 实现：直接返 `get_access_token(user_id)`（D1 dev 行为）
- 新建 `src/rolemesh/auth/fake_vault.py`：
  - 当 env `FAKE_VAULT=1` 时启用
  - `BOOTSTRAP_USERS` spec 可加可选字段 `static_access_token`、`force_refresh_fail`
  - vault 接口对接 fake 实现：`get_access_token` 返 static；`force_refresh_fail=true` 时第 N 次调用返 401
- pinned test：fake-vault 行为
  - 注入静态 token → MCP 调用收到正确 Bearer
  - force_refresh_fail → MCP 调用收到 401 → 触发 reauth 路径

### PR 3 — Fake-vault e2e + reauth 路径端到端

- 起一个 mock MCP server（最简 echo MCP，校验 Bearer header 并 echo 回来）
- 配 `BOOTSTRAP_USERS` + `FAKE_VAULT=1` + 一个 `auth_mode=user` 的 mcp_server 指向 mock
- e2e 脚本（加入 `scripts/smoke_user_mode_mcp.sh`）：
  1. alice 起 chat → coworker 调 MCP tool → mock MCP 收到 Bearer + correct user → 返成功 → token stream 显示
  2. force fail 后 alice 再调 MCP → coworker 收到 401 → `runs.status='awaiting_reauth'` → WS event 触发 → 前端 banner 显示
- INV-6 路径 7（reauth）从"stub 触发"升级到"真触发"

## Acceptance criteria

- [ ] credential_proxy user-mode wiring 全链路工作
- [ ] `exchange_for` 接口存在 + stub 行为正确
- [ ] fake-vault dev mode 可启停
- [ ] `scripts/smoke_user_mode_mcp.sh` 通过
- [ ] INV-6 路径 7 在真触发场景下 status 写入正确
- [ ] reauth banner 端到端可见
- [ ] 更新 plan 状态

## Out of scope

- ❌ 真 IdP 集成（Keycloak / Okta） —— 推迟到 OIDC 分支
- ❌ refresh token 真正自动 refresh 逻辑（fake-vault 不实现真 OAuth flow）

## Open questions

1. **`X-RoleMesh-Conversation-Id` 在 Pi 与 Claude SDK 端的注入点**：两个 backend 出站 MCP 路径不同，要分别 patch
2. **mock MCP server 用什么实现**：Python `fastmcp` lib / 手写 HTTP server / Node `@modelcontextprotocol/sdk`？看现有 tests/ 有没有现成的 mock
3. **fake-vault 的 `force_refresh_fail` 触发方式**：spec 里写死 vs. runtime API toggle？前者简单，后者更灵活；推荐前者

## Pitfalls

- credential_proxy 的 `auth_mode` 决策必须在请求时 lookup（不能 cache）—— mcp_server.auth_mode 改动后要立即生效
- fake-vault 必须**生产代码绝不引用** —— 用 env flag + factory 隔离
- mock MCP server 启动端口冲突——用动态端口 + 通过 env 传给 webui
- INV-6 的 `awaiting_reauth` UPDATE 必须由 orchestrator 端写，不是 coworker 容器内写——后者死了状态就丢了

## 执行前刷新清单

- [ ] 02a + 02b 完成？
- [ ] `mcp_servers.auth_mode` 字段实际行为已确认？
- [ ] mock MCP server lib 选型确认？

## Findings (after execution)

_(empty)_
