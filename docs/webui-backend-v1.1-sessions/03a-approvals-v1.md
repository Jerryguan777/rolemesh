# Session 03a — Approvals 迁 v1 + 多 user smoke  `[DRAFT]`

| field | value |
|---|---|
| Phase | 3 |
| Prerequisites | Phase 2 全 done |
| Estimated PRs | 2-3 |
| Estimated LOC | ~900 |
| Status | not started — DRAFT |

> **DRAFT**：本 session 是方案 A 多 bootstrap user 的第一次真业务用——alice 发起 / bob 审批 端到端跑。

## Goal

把现有 `/api/admin/approvals` / 审批业务迁到 `/api/v1/approvals/*`，前端实现 approvals 队列页 + 集成 INV-7 enum 翻译（01b 已落地的）+ 跑通多 user e2e smoke（INV-4 真实验证）。

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §3 Phase 3 / §4 WS approval events / §6.3 I / §11 INV-4 / INV-7
2. Phase 1 INV-7 实现（`src/rolemesh/approval/enum_translate.py`）
3. `src/rolemesh/approval/` 现状（engine + executor + notification）
4. 00a PR4 audit helper（`resolve_actor_user_id`）

## Scope — PR sketch

### PR 1 — `/api/v1/approvals/*` + `/api/v1/approval-policies/*`

- 端点（设计 §3 Phase 3）全实现
- `POST /approvals/{id}/decide` body `{action: "approve"|"reject", note?}` —— 走 INV-7 翻译层
- audit write 走 `resolve_actor_user_id`（INV-4）
- DELETE policy 时 pending requests 的 `policy_id` SET NULL（设计 §3 表格）
- pinned test：
  - alice approve / bob reject 各自 audit_log 写入 actor_user_id = 真 UUID
  - DELETE policy 时 pending requests 不阻塞 + policy_id 变 NULL

### PR 2 — WS approval events 完整接入

- `event.approval.required` server→client 推送（01b 已定义协议，本 session 让 engine 真发）
- `request.approval` / `event.approval.resolved` 双向往返
- pinned test：alice 发起 → bob 收到 WS event → bob 走 `request.approval` → engine 处理 → 双方收到 `event.approval.resolved`

### PR 3 — Frontend approvals 页面

- `#/approvals` 队列 + `#/approvals/:id` 详情
- WS event 触发实时刷新
- decide button 走 typed client POST
- 多 user smoke 手动跑（说明 `BOOTSTRAP_USERS` 切换两个 token 在两个 browser tab 演示）

## Acceptance criteria

- [ ] `/api/v1/approvals/*` 全 CRUD + INV-4 + INV-7 pinned test 绿
- [ ] WS approval events 双向工作
- [ ] **Phase 3 smoke**（设计 §10）：alice 在 tab 1 发起 → coworker 请求 approval → bob 在 tab 2 看到 → bob approve → coworker 继续 → audit_log 里 actor_user_id 是 bob 真 UUID
- [ ] Phase 1 e2e 不退化
- [ ] 更新 plan 状态

## Out of scope

- ❌ Skills per-tenant 迁移 —— 03b
- ❌ 复杂 policy DSL 演进（现有 policy 模型保持）

## Open questions

1. **`approval_policies` 现状**：表是否已存在？policy DSL 怎么表达？本 session 只搬 endpoint 还是顺便清理 DSL？
2. **`event.approval.required` 推送范围**：tenant 内 admin？policy 指定的 approver？policy 上层规则不变，但要明示

## Pitfalls

- audit FK 必须走 `resolve_actor_user_id`——直接写 bootstrap 字面量 UUID 会 FK 违例
- INV-7 翻译层必须包覆**两条路径**：HTTP `action` 与 WS `decision`
- approval engine 内部仍只见 `ApprovalOutcome`——任何 wire string 漏到 engine 是 bug

## 执行前刷新清单

- [ ] Phase 2 完成？
- [ ] `BOOTSTRAP_USERS` 多 user 实际跑过 Phase 1/2 smoke 没暴露 bug？
- [ ] 现有 approval engine 状态确认（无大改）

## Findings (after execution)

_(empty)_
