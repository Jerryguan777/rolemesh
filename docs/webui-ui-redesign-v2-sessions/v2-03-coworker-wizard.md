# Session v2-03 — Coworker wizard + Models provider-grouped + Credential per-provider extras  `[DRAFT]`

| field | value |
|---|---|
| Phase | v2 cycle |
| Prerequisites | v2-02 done（Settings shell + Models page 已有 provider grouping helper） |
| Estimated PRs | 3-4 |
| Estimated LOC | ~1200 |
| Status | not started — DRAFT |

> **DRAFT**：执行前 refresh，特别注意 v2-02 落地的 Models grouping helper 接口。

## Goal

6 步 coworker 创建向导 (`<rm-coworker-wizard>`，用 v2-00 落的 `<rm-wizard>` primitive)，**两个解决 blocking 项**：

- `folder` 自动派生（locked decision #4：name → kebab-slug，advanced 区可改）
- Credential 按 provider 区分字段（locked / spec §10.1 #2：Bedrock region 等）

## Required reading

- design §3 wizard table（6 步具体内容）
- design §10.1 #1 (folder) + #2 (credential extras) + #4 (WS send) + §10.3 (provider 切换是 list filter)
- v2-02 Findings —— Models page 抽的 provider-grouping helper 实际签名
- v1.1 `src/webui/schemas_v1.py` —— `CoworkerCreate` / `CredentialUpsert` 字段（特别 `extras: dict`）

## Scope sketch

- PR 1 — Wizard 框架 + 6 步骨架（Identity / Engine / Model / Tools / Skills / Review）
  - Identity: name input + auto-derived slug 实时显示 + advanced override + instructions textarea
  - Engine: `GET /backends` cards
  - Model: filtered list by engine + credential cross-check + inline "needs X credential" with dialog
  - Tools: MCP server multi-select + "+ Connect new server" 就地 dialog
  - Skills: skill multi-select + "+ New skill" 就地 dialog
  - Review: 草稿摘要 + Create button
- PR 2 — Credential per-provider dynamic fields (`<rm-credential-dialog>` 按 `provider` switch field set)
  - anthropic / openai / google: 只 api_key
  - bedrock: api_key + region + (optional) access_key_id / secret_access_key
  - 字段 schema 在前端 hardcode；schema_v1 `extras: dict` 已支持任意键
- PR 3 — Wizard 与 chat shell 集成 (`#/manage/coworkers` 的 "+ New coworker" button 弹 wizard；Create 成功后跳 chat with new coworker)
- PR 4 (可选) — 编辑 coworker dialog（v1.1 已有 PATCH endpoint，但 v2 visual 重做）

## Open questions for refresh

- Wizard 拒绝 advance 时的错误反馈方式（inline 红字 vs 顶部 banner）
- "needs X credential" inline → 弹 dialog 之间的过渡（pop-over vs replace step）
- super_agent vs agent 选项是否暴露 advanced（locked decision #10 = 不暴露，但 v2-02/03 后如果发现确实需要可 reopen）

## Findings

_(empty)_
