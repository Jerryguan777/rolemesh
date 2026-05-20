# Session 03b — Skills per-tenant 迁移 + UI  `[DRAFT]`

| field | value |
|---|---|
| Phase | 3 |
| Prerequisites | 03a done |
| Estimated PRs | 3 |
| Estimated LOC | ~1300 |
| Status | not started — DRAFT |

> **DRAFT**：skills 表从 per-coworker 改 per-tenant 是数据形变。注意 `coworker_id` 列**保留双写期**，drop 同样推到独立 session。

## Goal

把 `skills` 表从"per-coworker"语义迁到"per-tenant catalog"+ `coworker_skills` 关系层。前端实现 skills 列表与文件树编辑器。INV-5（SKILL_MANIFEST_NAME 三处一致）的 TS 端完成。

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §3 Phase 3 Skills / §11 INV-5
2. [`docs/19-skills-architecture.md`](../19-skills-architecture.md)
3. 00a PR1 落地的 `SKILL_MANIFEST_NAME` Python 常量
4. 00b PR2 加的 `skills_tenant_name_unique` 约束
5. `src/rolemesh/db/skill.py` 现状

## Scope — PR sketch

### PR 1 — skills 表迁移（双写）

- `skills.coworker_id` 列保留 NULLABLE（双写期）
- 新 skills 写入：tenant 级，`coworker_id=NULL`
- 旧数据：保留原样（每个 skill 仍带 coworker_id）；通过 `coworker_skills` 表把它们 enable 给原 coworker
- 读取路径双写期：合并查询（tenant catalog = 所有 `coworker_id IS NULL` + 历史 per-coworker skills）
- 一次性 backfill script：把每个 per-coworker skill 复制成 tenant-level + 在 `coworker_skills` enable —— 慎重，先 dry-run（同名冲突走 03b 内决策）

### PR 2 — `/api/v1/skills/*` + 文件 endpoints

- 全套 CRUD（设计 §3）
- 文件 endpoint：GET/PUT/DELETE 单文件
- `DELETE skills/{id}/files/SKILL.md` 必须返 409 `SKILL_MANIFEST_PROTECTED`
- `/api/v1/coworkers/{id}/skills` 关系层 endpoint
- INV-5 TS 端：在 `web/src/api/` 加 `const SKILL_MANIFEST_NAME = "SKILL.md"`，并加 lint test 与 Python 常量/DB CHECK 一致
- pinned test：
  - SKILL.md 删除 → 409
  - DELETE skill 被 coworker enable → 409 RESOURCE_IN_USE

### PR 3 — Frontend skills 列表 + 文件树编辑器

- `#/skills` + `#/skills/:id`（分屏文件树 + 编辑器）
- 用 typed client
- 编辑器内不允许删 SKILL.md（前端 UI 禁用 + server 端 409 兜底）

## Acceptance criteria

- [ ] skills 双写一致；旧 coworker 启动后 skill 投影不退化
- [ ] INV-5 三处一致 lint test 绿（Python / DB CHECK / TS）
- [ ] SKILL.md 受保护（前端 UI + server 409）
- [ ] Phase 3 smoke 重跑通过（skill 投影到容器 tmpfs）
- [ ] `skills.coworker_id` 列**仍在**（drop 留独立 session）
- [ ] 更新 plan 状态

## Out of scope

- ❌ Drop `skills.coworker_id` 列 —— 等本 session 实跑稳定后另开 session（与 drop coworker.tools 类似，可合并到 03+ 那个 session 一起做）

## Open questions

1. **backfill 同名冲突**：如果 tenant 内多个 coworker 有同名 skill，backfill 到 tenant catalog 时违反 `skills_tenant_name_unique`。怎么处理？rename 加 coworker 前缀？合并？session 内问 reviewer
2. **DB CHECK constraint for `SKILL_MANIFEST_NAME`**：现有 schema 有没有 CHECK 用 `'SKILL.md'` 字面量？没有的话本 session 是否加？或者放 INV-5 测试时纯 grep 验证就行？

## Pitfalls

- per-coworker → per-tenant 不是简单复制——同 tenant 内同名要合并；先 dry-run 看冲突量
- 文件 PUT 必须校验 `SKILL_FILE_PATH_RE`（00a PR1 落的常量），不允许 `../` 等路径
- INV-5 测试不要太复杂——简单 grep + 文件读取断言三处字面量相同即可
- `coworker_skills` 关系层 enable=true/false 的 hot-load 走 `web.coworker.skills_changed` event（设计 §7）

## 执行前刷新清单

- [ ] 03a 完成？
- [ ] 现有 skills 数据量 + 冲突情况 dry-run 跑过？
- [ ] DB CHECK 加不加确认？

## Findings (after execution)

_(empty)_
