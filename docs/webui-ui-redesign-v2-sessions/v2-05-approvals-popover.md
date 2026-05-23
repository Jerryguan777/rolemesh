# Session v2-05 — Top bar Approvals popover (real-time WS inbox)  `[DRAFT]`

| field | value |
|---|---|
| Phase | v2 cycle |
| Prerequisites | v2-04 done |
| Estimated PRs | 1-2 |
| Estimated LOC | ~400 |
| Status | not started — DRAFT |

> **DRAFT**：执行前 refresh，特别看 v2-01 顶栏 Approvals badge 的占位实现。

## Goal

顶栏 Approvals icon 点击 → 弹 popover 显示当前 user 待审批的 pending approvals（实时刷新走 WS）。badge 角标显示数字。

复用 03a 落的 `event.approval.required` / `event.approval.resolved` WS event 总线。inline approve/reject button 在 popover 内调 `POST /api/v1/approvals/{id}/decide`。

## Required reading

- design §2.1 顶栏 + §6.3 I approval queue
- v1.1 03a Findings —— WS approval events 双发布 subject pattern (`web.approval.{required,resolved}.conv.<id>` + `.req.<id>`)
- v1.1 `web/src/components/inline-approval.ts` (03a 落地的独立组件) —— popover 内每行直接复用
- v1.1 `web/src/ws/v1_client.ts` —— event 总线 + reconnect 协议
- v2-01 顶栏 Approvals badge 占位实现

## Scope sketch

- PR 1 — `<rm-approvals-popover>` 组件：
  - 顶栏 button → 弹 popover
  - 内部调 `GET /api/v1/approvals?status=pending&approver=me` 拿初始列表
  - 订阅 `event.approval.required` (新 row) + `event.approval.resolved` (移除已 decided row)
  - 每行用 `<rm-inline-approval>` 渲染
  - badge 角标 = popover items 数；real-time 同步
- PR 2 (可选) — WS subscribe scope 优化：popover 关闭时是否仍订阅（用户在 chat 里改另一个 conv 的 approval）

## Open questions for refresh

- popover 永久订阅 vs 打开时订阅：永久订阅 badge 才实时（推荐永久）
- popover 列空时显示什么 (empty state)
- popover 显示限 5 条 + "View all in Activity" link？还是无限滚？推荐前者

## Findings

_(empty)_
