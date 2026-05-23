# Session v2-B — Coworker wizard + Models provider grouping + Credential per-provider extras  `[DRAFT]`

| field | value |
|---|---|
| Phase | v2 cycle |
| Prerequisites | v2-A done（tokens / dialog / wizard primitive / chat shell / settings shell 全就位） |
| Estimated PRs | 2-3 |
| Estimated LOC | ~700 |
| Status | not started — DRAFT |

> **DRAFT**：执行前 refresh，特别看 v2-A 落的 settings shell 内 Models page 是否已抽 provider grouping helper。

## Goal

v2 唯一有真新业务逻辑的 session：

1. **6 步 Coworker wizard**（用 v2-A 落的 `<rm-wizard>` primitive）—— `folder` 自动派生（locked decision #4）
2. **Models page provider grouping**：按 provider 分组 + 交叉 `GET /tenant/credentials` 算 ready/locked
3. **Credential per-provider extras**：`<rm-credential-dialog>` 按 provider 动态字段（解 blocking #2，Bedrock region 等）
4. **Wizard 内联补 credential**：选中模型 provider 缺凭据时弹 credential dialog 就地补，成功后当场解锁该行

## Required reading

- v2-A Findings（特别 Models grouping helper / wizard primitive 实际接口 / dialog primitive 用法）
- design §3 wizard 表 + §10.1 blocking #1 #2 #4 + §10.3 provider 切换是 list filter
- `src/webui/schemas_v1.py` —— `CoworkerCreate.folder` 必填 / `CredentialUpsert.extras: dict`
- v1.1 现有 coworkers-page 创建逻辑（v2-B 取代它）+ credentials-page (v1.1 02a)

## Scope sketch

- **PR 1** — 6 步 wizard 框架 + Identity / Engine / Model / Review 四步内容
  - Identity: name + auto-derived slug 实时显示 + advanced override + instructions
  - Engine: `GET /backends` 卡片
  - Model: filtered list + credential cross-check + inline "needs X credential" badge
  - Review: 草稿摘要 + Create button
- **PR 2** — `<rm-credential-dialog>` per-provider 动态字段 + Tools / Skills 两步绑定
  - Tools: MCP server multi-select + "+ Connect new server" 就地 dialog
  - Skills: skill multi-select + "+ New skill" 就地 dialog
  - Credential dialog 按 provider switch field schema (前端 hardcode；后端 schema 已支持任意 keys)
- **PR 3** (optional) — Models page provider grouping helper 提取到独立 module 供 wizard + Models page 复用

## Open questions for refresh

- Wizard 错误反馈 (inline 红字 vs banner)
- "needs X credential" → dialog → 解锁动画细节
- credential dialog 关闭返 wizard 的过渡

## Findings

_(empty)_
