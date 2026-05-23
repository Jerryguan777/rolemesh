# Session v2-C — Activity shell + Approvals popover + visual polish  `[DRAFT]`

| field | value |
|---|---|
| Phase | v2 cycle |
| Prerequisites | v2-B done（chat shell + settings shell + wizard 全功能就位） |
| Estimated PRs | 2-3 |
| Estimated LOC | ~600 |
| Status | not started — DRAFT |

> **DRAFT**：v2 收尾 session。执行前 refresh，看 v2-A / v2-B 留下的 polish backlog。

## Goal

v2 三件收尾：

1. **`<rm-activity-shell>`** (`#/activity/*`) —— overlay 形态，2 个 tab (Safety decisions / Approval log)。**无 Runs 标签**（locked decision #3）。两个 tab 各 wrap v1.1 现有组件 (`<rm-safety-decisions-page>` + `<rm-approvals-page>` 已处理视图)
2. **`<rm-approvals-popover>`** —— 顶栏 Approvals icon 点击弹出；实时 WS 订阅（用 v1.1 03a 落的 `event.approval.{required,resolved}` + v2-A 落的占位 badge）；inline approve/reject 用 v1.1 `<rm-inline-approval>` 组件
3. **Visual polish + token lint** —— grep 看是否有硬编码颜色 hex / 字体字面量；加 `web/scripts/lint-tokens-only.mjs` 防回退；7 个关键页 spot check 与 prototype 对照
4. **v2 整体 retro**（Findings 段）—— 类似 v1.1 04 retro 模式，给未来 v3 / 新 cycle 留经验

## Required reading

- v2-A + v2-B Findings
- design §2 observe 模式 + §6.3 I approval queue + §7 视觉语言
- v1.1 03a Findings (WS event 双发布 pattern: `web.approval.{required,resolved}.conv.<id>` + `.req.<id>`)
- v1.1 `web/src/components/inline-approval.ts` (03a 落) + `web/src/ws/v1_client.ts` (01c 落)

## Scope sketch

- **PR 1** — `<rm-activity-shell>` overlay + 2 tab (Safety decisions / Approval log)
  - 顶栏 tab 切换 + 右上 X 返 chat
  - 两 tab 内 wrap v1.1 现有组件（业务逻辑 0 改，只 reskin 卡片样式）
- **PR 2** — `<rm-approvals-popover>` + 实时 WS
  - 调 `GET /api/v1/approvals?status=pending&approver=me` 拿初始列表
  - 订阅 `event.approval.required` / `event.approval.resolved`
  - 每行用 `<rm-inline-approval>` 渲染
  - badge 角标 = popover items 数，real-time 同步
  - popover 列空 / 列 5 条 + "View all" link → `#/activity/approvals-log`
- **PR 3** — polish + token lint + spot check
  - grep 找硬编码颜色 / 字体；移到 tokens.css
  - `web/scripts/lint-tokens-only.mjs` 防回退
  - playwright 截图 7 个关键页 (chat shell / settings shell 各主页 / wizard / activity / popover) → 目测对照原型
  - pre-existing TS errors (`credentials-page.ts` / `mcp-servers-page.ts`，v1.1 03a 提的) —— 顺手修

## Open questions for refresh

- Activity shell modal-over-chat vs full-screen 替换 → 推荐 overlay (设计 §2 暗示)
- Approvals popover 永久订阅 vs 打开订阅 → 推荐永久（badge 才实时）
- popover empty state 文案
- v2 retro 写多详细（v1.1 04 retro 有 ~150 行；v2 比较小可以更简）

## v2 retrospective（Findings 末尾）

类似 v1.1 04 retro 位置，含：

- 3 个 session 各自 sized correctly？
- DRAFT refresh 实际节奏（目标 < v1.1 的 6 次）
- 13 条 locked decisions 事后看是否正确
- 与 v1.1 对比：v2 是否真应用了 retro 6 条 reusable lessons
- 对未来 v3 / 新 cycle 的建议

## Findings

_(empty — v2 收尾 session)_
