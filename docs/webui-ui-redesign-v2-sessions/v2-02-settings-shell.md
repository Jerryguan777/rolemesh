# Session v2-02 — Settings shell + 7 block pages reskin  `[DRAFT]`

| field | value |
|---|---|
| Phase | v2 cycle |
| Prerequisites | v2-01 done |
| Estimated PRs | 3-4 |
| Estimated LOC | ~800 |
| Status | not started — DRAFT |

> **DRAFT**：执行前重新 read v2-00 / v2-01 Findings 并 refresh。

## Goal

新建 `<rm-settings-shell>` 替代 v1.1 `<rm-app-shell>` 在 `#/manage/*` 路径下的渲染。Sidebar 分组：

```
Coworkers                ← 置顶，一等公民
Building blocks
  · MCP servers
  · Skills
  · Models
  · Credentials
Governance
  · Safety rules
  · Approval policies
Workspace
  · General
  · Members
Account
  · Appearance
```

11 个内嵌页 reskin。chat shell 切到 Settings 通过顶栏 gear icon。

## Required reading

- v2-00 + v2-01 Findings
- design §2.2 + prototype `.spage` / `.snav` / `.spane` class
- v1.1 现有 coworkers-page / mcp-servers-page / models-page / credentials-page / skills-page / safety-rules-page / approvals-page (placeholder approval-policies)

## Scope sketch

- PR 1 — `<rm-settings-shell>` 主壳（sidebar + main area + 选中 entry 高亮）
- PR 2 — 4 个 building blocks page reskin（MCP / Skills / Models / Credentials），统一卡片 + hover 编辑按钮模板
- PR 3 — 2 个 Governance page reskin（Safety rules read-only list + Approval policies basic CRUD per locked decision #7/#8）
- PR 4 — Workspace + Account 3 个 page（General / Members / Appearance）—— v1.1 没有这些，新建占位 + Appearance 真做（system theme toggle）

## Open questions for refresh

- Models 页 provider 分组 + Credentials 交叉显示真做不做（设计 §3 第 3 步 wizard 也要这功能，应该在这里抽 helper） → v2-03 才落 wizard，可以本 session 抽 helper
- 旧 `<rm-app-shell>` 何时下线（v2-02 完工后立刻删，还是 v2-06 polish 时删）

## Findings

_(empty)_
