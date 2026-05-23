# Session v2-06 — Visual polish + cross-page consistency  `[DRAFT]`

| field | value |
|---|---|
| Phase | v2 cycle |
| Prerequisites | v2-05 done（v2 全栈功能完工） |
| Estimated PRs | 1-2 |
| Estimated LOC | ~300 |
| Status | not started — DRAFT |

> **DRAFT**：执行前 refresh，看 v2-01 到 v2-05 留下的 polish backlog。

## Goal

v2 全栈完工后的视觉/体验收尾。**不加新功能**——专门 fix v2-01 到 v2-05 累积的小瑕疵 + 跨页一致性 + 各页与原型对照。

按 locked decision #6 "大致语言一致"——不做 pixel-perfect playwright diff，但要做 spot check 确保 token / 字体 / 卡片样式 across all pages 一致。

## Required reading

- v2-01 到 v2-05 全部 Findings 段（特别看每个 session "follow-up polish chore" 类备注）
- prototype 全 HTML（重点 `<style>` 段）作为视觉对照
- v1.1 `web/scripts/lint-no-admin-chat.mjs` —— pattern 借用做"lint-tokens-only"（禁止硬编码颜色字面量）

## Scope sketch

- PR 1 — 跨页 token / typography 一致性扫描 + 修：
  - grep `web/src/` 看是否有硬编码颜色 hex / RGB（应只在 tokens.css）
  - grep 看是否有 `font-family` 字面量（应只在 tokens.css）
  - 各页 spacing / border-radius / shadow 用 token 不用字面量
  - 加 `web/scripts/lint-tokens-only.mjs` 防回退
- PR 2 (可选) — 原型对照 spot check：
  - playwright 截图 7 个关键页（chat shell / settings shell 主页 / 4 个 block 页 / wizard 各步 / activity / approvals popover）
  - 与原型对应区域**目测对照**（不做 diff 自动化）
  - 列具体不一致项 + 修
- PR 3 (可选) — accessibility 收尾：
  - 键盘 navigation：tab 顺序 / Enter / ESC 都工作
  - 颜色对比度（dark mode 特别）
  - screen reader labels

## Open questions for refresh

- 视觉 spot check 由 v2-06 session 自己做，还是 user 手动 review？
- accessibility 范围（WCAG AA full 还是 minimal coverage）
- pre-existing TS errors（v1.1 03a Findings 提的 credentials-page.ts / mcp-servers-page.ts）—— v2 各 session reskin 时应该顺手修，但 v2-06 兜底确认是否真清干净

## v2 retrospective

v2-06 Findings 段是 v2 整体 retro 位置（类似 v1.1 04 Findings 的 13 session retro）。包括：

- v2 7 个 session 各自 sized correctly 与否
- DRAFT → refresh 实际节奏（目标 < v1.1 的 6 次）
- locked decisions 13 条是否事后看正确
- 与 v1.1 的对比：v2 是否真应用了 v1.1 retro 那 6 条 reusable lessons
- 对未来 v3 / 新 cycle 的建议

## Findings

_(empty — v2 收尾 session)_
