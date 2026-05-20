# Session 02a — Models + Credentials + MCP CRUD  `[DRAFT]`

| field | value |
|---|---|
| Phase | 2 |
| Prerequisites | Phase 1 全 done（含 e2e smoke 通过） |
| Estimated PRs | 3-4 |
| Estimated LOC | ~1200 |
| Status | not started — DRAFT |

> **DRAFT**：本 prompt 在 Phase 1 完工前可能需要刷新。**执行前必读**：
> - 末尾"刷新清单"
> - Phase 1 三个 session 的 Findings 段
> - 检查 plan critique §5 / §8 的开放问题是否已解决

## Goal

落地 `/api/v1/models`（只读）、`/api/v1/tenant/credentials`、`/api/v1/mcp-servers` 全套 CRUD。**不**实现 user-mode credential 注入（02c 做）。本 session 后 coworker 能配真 credential + service-mode MCP 跑起来。

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §3 Phase 2 / §7 hot-load 矩阵 / §8 credential 行
2. Phase 1 Findings
3. `src/rolemesh/egress/` —— 现有 credential_proxy / mcp_cache 实现
4. `src/rolemesh/db/` —— `mcp_servers` 表（00b 已建）的现有 CRUD（如有）

## Scope — PR sketch

### PR 1 — `/api/v1/models` + `/api/v1/tenant/credentials`

- `GET /api/v1/models?provider=&family=` —— 列表，公开（已 seed）
- `GET /api/v1/models/{id}`
- `GET /api/v1/tenant/credentials` —— **永不返真 key**，只返 `provider / credential_ref / created_at / updated_at`
- `PUT /api/v1/tenant/credentials/{provider}` —— body 含真 key，写入 secret store + DB 只存 ref
- `DELETE /api/v1/tenant/credentials/{provider}` —— 如果有 coworker 在用返 409 `RESOURCE_IN_USE`
- 修改 credential 后 publish NATS event → orchestrator 重启用该 provider 的 coworker（plan critique §5 缺的）

### PR 2 — `/api/v1/mcp-servers` CRUD

- 全套 CRUD + RLS + 双层防御
- `auth_mode` 字段必须能存 `user / service / both`
- `tool_reversibility` JSONB 直接透传 IPC（00a INV-2 已确保兼容）
- DELETE 被引用返 409
- hot-reload 走现有 `egress.mcp.changed` event（已实现）

### PR 3 — `/api/v1/coworkers/{id}/mcp-servers` 关系层

- POST 绑定 / DELETE 解绑 / PATCH 改 `enabled_tools`
- `enabled_tools` 三态：NULL=全启用 / []=全禁 / [...]=白名单
- 写入路径同时发 `web.coworker.mcp_changed` event
- pinned test 覆盖三态语义

### PR 4 — Frontend：Models / Credentials / MCP 页面

- 三个对应路由的页面实现（设计 §6.3 D/E/F）
- 复用 00c shell + typed client
- Credentials 页面绝不显示真 key —— form 只用于 PUT，已有的不回填

## Acceptance criteria

- [ ] 全 endpoint 单测 + RLS 隔离测试
- [ ] Phase 2 smoke 部分（service-mode MCP 路径）：配真 credential → 绑 MCP → coworker 用 MCP tool 成功
- [ ] DELETE 被引用 → 409 + 结构化 error
- [ ] Credentials 响应**永不**包含真 key（pinned test 验证）
- [ ] 更新 plan 状态

## Out of scope

- ❌ user-mode credential 注入 —— 02c
- ❌ `coworker.tools` 双写 —— 02b
- ❌ Models admin 写入 endpoint —— v2

## Open questions

1. **secret store 选什么**：HashiCorp Vault / AWS Secrets Manager / sealed-secrets / 本地加密文件？项目里现有怎么做？
2. **credential 更新触发的 coworker 重启范围**：是 tenant 内全部使用该 provider 的，还是只重启 idle 的（避免打断进行中 run）？

## Pitfalls

- `tenant_model_credentials.credential_ref` 不能是明文 key——必须是 secret store 的 key
- `GET /api/v1/tenant/credentials` 的 response_model 严格禁列 key 字段
- `mcp_servers.auth_mode` 默认值要给（设计 §2.1 没默认，决定 `service` 还是必填）

## 执行前刷新清单（DRAFT 状态）

- [ ] Phase 1 三个 session 已完成？
- [ ] secret store 方案确认了？
- [ ] credential 重启策略确认了？
- [ ] 与 02b（tools 双写）做不做并行 PR 协调过了？
- [ ] 现有 `mcp_servers` 表是否已有部分 CRUD？避免重复

## Findings (after execution)

_(empty)_
