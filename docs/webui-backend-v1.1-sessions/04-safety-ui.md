# Session 04 — Safety UI 迁 v1  `[DRAFT]`

| field | value |
|---|---|
| Phase | 4 |
| Prerequisites | 03b done（不需要等 03+） |
| Estimated PRs | 2-3 |
| Estimated LOC | ~900 |
| Status | not started — DRAFT |

> **DRAFT**：safety 的 admin 端点已存在并工作。本 session 只是搬到 v1 + 前端整合到 `<rm-app-shell>`。

## Goal

把现有 `/api/admin/safety/*` 搬到 `/api/v1/safety/*`，前端从独立 admin 页迁到统一 shell 内的 `#/admin/safety/*`（保留路径，但壳是 `<rm-app-shell>`）。完成 v1.1 全套 UI 收敛。

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §3 Phase 4 / §6.1 路由
2. [`docs/13-safety-overview.md`](../13-safety-overview.md) + 14/15/16 各 safety 文档
3. `src/webui/admin.py` 中 safety 相关 endpoint
4. 现有前端 admin safety 页面位置

## Scope — PR sketch

### PR 1 — `/api/v1/safety/*` endpoints

- 6 个端点（设计 §3 Phase 4）全 GET（safety 是只读 + audit 视图）
- 复用现有 admin handler 逻辑（提取到 `rolemesh/safety_service.py` 共享）
- 双层防御 + RLS
- pinned test：RLS 隔离 + 响应 schema

### PR 2 — Frontend safety 页接入 shell

- 现有 safety 独立页拆掉，包进 `<rm-app-shell>`
- 路由保留 `#/admin/safety/rules` / `#/admin/safety/decisions`（已存在）
- typed client 调 v1 endpoint
- 现有交互不退化

### PR 3 (可选) — admin safety 端点 deprecation 标记

- `/api/admin/safety/*` response header 加 `Sunset` / `Deprecation`（RFC 8594）
- 文档标 deprecate

## Acceptance criteria

- [ ] `/api/v1/safety/*` 全 endpoint 工作
- [ ] 前端 safety 页在新 shell 下不退化
- [ ] **Phase 4 smoke**（设计 §10）：safety rule 触发 block → decision 落表 → UI 显示
- [ ] 全套测试通过
- [ ] 更新 plan 状态——**全部 13 session 完成**，v1.1 整体收尾

## Out of scope

- ❌ Safety rule 写入 endpoint —— admin only，保留原状
- ❌ Safety 规则 DSL 改动

## Open questions

1. **现有 admin 前端在哪个目录**：与 `<rm-app-shell>` 集成有无难点？
2. **`Sunset` header 的截止日期**：设计文档说 admin 保留 6 个月——具体日期？

## Pitfalls

- safety 业务复杂，不要在搬迁时顺手"重构"——只搬不改
- RLS 在 safety 表上的现状要先确认（18-rls-architecture.md），避免迁过来发现 admin 端绕过了 RLS
- decision 表数据量可能大——分页参数必须保留

## 执行前刷新清单

- [ ] Phase 3 完成？
- [ ] admin safety 现状跑过一遍熟悉？

## Findings (after execution)

_(empty — v1.1 收尾 session，整理整体回顾)_
