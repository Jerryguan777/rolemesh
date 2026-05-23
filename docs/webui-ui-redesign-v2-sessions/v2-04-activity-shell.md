# Session v2-04 — Activity shell (Safety decisions + Approval log; sans Runs)  `[DRAFT]`

| field | value |
|---|---|
| Phase | v2 cycle |
| Prerequisites | v2-03 done |
| Estimated PRs | 2 |
| Estimated LOC | ~500 |
| Status | not started — DRAFT |

> **DRAFT**：执行前 refresh。Activity Runs **不做**（locked decision #3）。

## Goal

新 `<rm-activity-shell>` 在 `#/activity` 路径下渲染，含 2 个 tab：

1. **Safety decisions** —— reskin v1.1 `<rm-safety-decisions-page>`
2. **Approval log** —— reskin v1.1 `<rm-approvals-page>` 的已处理视图（实时 inbox 在顶栏 popover，v2-05 做）

**Runs 标签页明确 cut**——跨 conversation 的 run 历史在 dev 阶段无用例，加端点 + 列表 + 分页是真 ~500 LOC 0 当前用户痛点。当前 chat-panel 内的 run 状态已经够（per-conversation 实时）。

## Required reading

- design §2 observe 模式 + §10.1 #3 (Runs 缺端点的讨论)
- v1.1 `<rm-safety-decisions-page>` (04 落地) + `<rm-approvals-page>` (03a 落地)
- v2-02 Settings shell 的 main area 模板（Activity shell 复用相同模板，但是顶栏 tab 切换不是 sidebar）

## Scope sketch

- PR 1 — `<rm-activity-shell>` 主壳：顶栏 tab (Safety decisions / Approval log) + 各 tab 内嵌组件 + 顶栏右上角 X 关闭按钮（→ 返 chat shell）
- PR 2 — 两个 tab 内组件 reskin（颜色 + 字体 + 卡片样式 → v2 tokens）；不改业务逻辑

## Open questions for refresh

- Activity shell 是 modal-over-chat 还是 full-screen 替换？设计原型 `.actshell` overlay 模式更友好（关闭直接回 chat） → 推荐 overlay
- "approval log" 与"approvals queue (顶栏 popover)" 区分 UI 上够清楚吗

## Findings

_(empty)_
