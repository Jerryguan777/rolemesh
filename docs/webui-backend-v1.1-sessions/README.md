# v1.1 Session Prompts

每个 `*.md` 文件是一份**自包含的 prompt**，可以直接喂给一个全新的 Claude Code session 执行。

## 文件命名规则

```
<NN><letter>-<topic-slug>.md
^^ ^^^^^^   ^^^^^^^^^^^^
phase letter  描述
```

- `00a` = Phase 0 第 1 个 session
- `03+` = Phase 3 之后的独立 session（不属于任何 phase）

## 文件结构

每个 session prompt 包含：

1. **Header** — phase / 前置 session / 估算 PR 数 / 状态
2. **Goal** — 1-2 句话说清楚做什么
3. **Required reading** — 进 session 前必须读的文件
4. **Scope (PR breakdown)** — PR 拆分建议
5. **Acceptance criteria** — 完工标准
6. **Out of scope** — 明确不做的事
7. **Open questions** — 必须问用户确认的（若有）
8. **Pitfalls** — 已知坑
9. **Findings** — 执行后补 — 给下游 session 参考

## 怎么用

新开一个 session，输入：

```
请读取 docs/webui-backend-v1.1-sessions/<file>.md，按描述完成所有 PR。
完成后跑验收清单中所有检查项，更新 docs/webui-backend-v1.1-plan.md 的状态表。
执行中发现需要拆 PR 或调整范围的情况，记录到该文件末尾的 Findings 段。
```

## Draft 标记

Phase 0+1（00a / 00b / 00c / 01a / 01b / 01c）= **finalized**，可以直接执行。

Phase 2+3+4 = **DRAFT**，执行前要 review。因为下游内容受上游 smoke 结果影响，今天写明天可能不准。每个 DRAFT prompt 末尾有"刷新清单"——执行前用户和 Claude 过一遍。

## 不要做的事

- 不要一个 session 跑两个 prompt 文件（context 会乱）
- 不要把 `<file>.md` 删掉——它是历史档案，Findings 段是下游 session 的输入
- Phase 2-4 的 prompt 不要在执行 Phase 0/1 期间提前批量改——等 Phase 0/1 真打完再刷
