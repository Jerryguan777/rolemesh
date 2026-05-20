# Session 03+ — Drop `coworkers.tools` 列 + drop `skills.coworker_id` 列  `[DRAFT]`

| field | value |
|---|---|
| Phase | n/a（独立 session，跨 phase） |
| Prerequisites | 02b（tools 双写）+ 03b（skills 双写）都跑通过且**实跑稳定一段时间** |
| Estimated PRs | 2 |
| Estimated LOC | ~200（纯 migration + 清理） |
| Status | not started — DRAFT |

> **DRAFT + STRONG**：本 session 是数据形变 stage 3。**不要在 02b/03b 刚完工就开**——必须等 Phase 3 全部 e2e smoke 通过、生产/dev 实跑至少一周（如果项目有 staging，跑通 staging），确认双写 reader 切完真无回退后再开。
>
> 如果 grep 输出仍有任何残留 reader，**本 session 立刻中止**，回到 02b/03b 补 reader 切。

## Goal

Drop `coworkers.tools` JSONB 列与 `skills.coworker_id` 列。完成设计 §9.3 三阶段下线 stage 3。

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §9.3 stage 3
2. 02b Findings + 03b Findings —— 必须先确认两个 Findings 段都说"reader 切完，长时间无回退"
3. STEPS.md 中 02b / 03b 完成后的所有提交—— grep 一遍确认没引入新 reader

## Scope — PR sketch

### PR 1 — Pre-flight check

- 跑全仓 grep：
  ```bash
  grep -rn "coworker.*\.tools\b\|cw\.tools\b" src/ tests/ scripts/ container/ | grep -v __pycache__ | grep -v "coworkers\.tools[^=]" | grep -v "test_run_state_machine"
  ```
  必须为空，**除了 schema.py 写 column 定义、db/coworker.py 写入路径**
- 同样 grep `skills.coworker_id`：
  ```bash
  grep -rn "skills.*coworker_id\|skill\.coworker_id" src/ tests/ | grep -v __pycache__
  ```
  应只剩 schema.py 与 db/skill.py 写入路径
- 把两个 grep 输出贴到本 PR description，作为"确认无回退"证据
- pre-flight 不过就在 PR description 列残留 reader 让 reviewer 决定推迟

### PR 2 — Migration drop columns + 清写入路径

- `ALTER TABLE coworkers DROP COLUMN tools;`（CASCADE 必要时）
- `ALTER TABLE skills DROP COLUMN coworker_id;`
- 删除 db/coworker.py 中 tools JSONB 的写入逻辑
- 删除 db/skill.py 中 coworker_id 的写入逻辑
- schema.py 同步移除 tools / coworker_id 列定义
- 删除 backfill script（已完成历史使命）
- 跑全套测试 + Phase 1/2/3 smoke 重跑
- 跑 02b/03b 的双写一致性测试**改成"新写入只走关系表"测试**

## Acceptance criteria

- [ ] Pre-flight grep 全空（除 schema 和写入路径）
- [ ] DB migration 跑通
- [ ] 全套测试通过
- [ ] Phase 1/2/3 smoke 全过
- [ ] 更新 plan 状态

## Out of scope

- ❌ 任何业务变更
- ❌ 任何 API 变更

## Open questions

无（这是 mechanical 操作）

## Pitfalls

- DROP COLUMN 在 PG 是即时的且不可逆——必须先 pg_dump 备份再跑
- 如果 grep 漏了某个隐性 reader（典型：动态构造 SQL 字符串），生产会立刻爆。这是为什么 pre-flight + 长时间观察期必要
- 删完 column 后**禁止回滚到 stage 2**——回滚必须 restore from backup
- backfill script 删了，万一发现没切干净的 reader 就只能 hotfix

## 执行前刷新清单（强烈版）

- [ ] 02b 完成至少一周？
- [ ] 03b 完成至少一周？
- [ ] Phase 3 完整 smoke 至少跑过 2 次都过？
- [ ] 02b/03b Findings 段都说"reader 切完无问题"？
- [ ] Pre-flight grep 真的空？

任何一条 ❌ 就推迟。

## Findings (after execution)

_(empty)_
