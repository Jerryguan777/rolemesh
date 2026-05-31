# v2 UI Redesign Session Prompts

每个 `*.md` 是一份**自包含的 prompt**，直接喂给一个全新 Claude Code session 执行。沿用 v1.1 同款约定（详见 `docs/webui-backend-v1.1-sessions/README.md`）。

## 文件命名

```
v2-X-<topic>.md
   ^   ^^^^^^^
session  描述
```

## 当前状态（3 session 结构）

| Session | 状态 | 详细度 |
|---|---|---|
| v2-A-foundations-and-shells.md | finalized | 详细 |
| v2-B-coworker-wizard-and-credentials.md | DRAFT | stub，执行前 refresh |
| v2-C-activity-polish.md | DRAFT | stub，执行前 refresh |

## Collapse note (2026-05-22)

原本拆 7 个 session（v2-00 到 v2-06）每个 ~500-1200 LOC 共 ~4500 LOC。用户审视后指出：v2 本质是 prototype HTML 翻译 + v1.1 既有组件 slot，不是新 backend 工作。重估实际 ~2600 LOC，合并到 3 session（每个 thematic 整体）。

设计原则继承自 v1.1 retro："Greenfield over compat-window staging when callers are countable"——v2 callers single-dev，可以激进合并。

## 怎么用

新开 session：

```
请读取 docs/webui-ui-redesign-v2-sessions/<file>.md，按描述完成所有 PR。
完成后跑 Acceptance criteria，更新 docs/webui-ui-redesign-v2-plan.md 的状态表。
执行中发现需要拆 PR 或调整范围的情况，记录到该文件末尾的 Findings 段。
```

## Refresh 节奏

- v2-A 详写（次大 session，地基决定下游）
- v2-B / v2-C DRAFT，执行前 read 上游 Findings + grep 后 refresh

v1.1 13 session 中 6 次 refresh = 46%；v2 3 session 目标 ≤ 1 次 refresh（v2-B / v2-C 各最多 refresh 一次）。

## 不要做的事

- 不要一个 session 跑两个 prompt 文件
- 不要把 `<file>.md` 删掉——它是历史档案，Findings 是下游输入
- DRAFT 的 prompt 不要在 v2-A 执行期间提前批量改——等真开始时再 refresh
