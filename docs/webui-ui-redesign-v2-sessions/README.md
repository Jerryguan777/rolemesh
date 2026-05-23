# v2 UI Redesign Session Prompts

每个 `*.md` 是一份**自包含的 prompt**，直接喂给一个全新 Claude Code session 执行。沿用 v1.1 同款约定（详见 `docs/webui-backend-v1.1-sessions/README.md`）。

## 文件命名

```
v2-NN-<topic>.md
   ^^   ^^^^^^^
 session  描述
```

## 当前状态

| Session | 状态 | 详细度 |
|---|---|---|
| v2-00-foundations.md | finalized | 详细 |
| v2-01-chat-shell.md | finalized | 详细 |
| v2-02-settings-shell.md | DRAFT | stub，执行前 refresh |
| v2-03-coworker-wizard.md | DRAFT | stub，执行前 refresh |
| v2-04-activity-shell.md | DRAFT | stub，执行前 refresh |
| v2-05-approvals-popover.md | DRAFT | stub，执行前 refresh |
| v2-06-visual-polish.md | DRAFT | stub，执行前 refresh |

## 怎么用

新开 session：

```
请读取 docs/webui-ui-redesign-v2-sessions/<file>.md，按描述完成所有 PR。
完成后跑 Acceptance criteria，更新 docs/webui-ui-redesign-v2-plan.md 的状态表。
执行中发现需要拆 PR 或调整范围的情况，记录到该文件末尾的 Findings 段。
```

## Refresh 节奏（继承 v1.1 教训）

v1.1 13 个 session 总共 refresh 了 6 次（约 46%）。v2 这次**只详写前 2 个**（v2-00 / v2-01），其它 5 个 stub，执行前一一 refresh。原因：

- DRAFT 写得太早会被上游 session 落地的细节打脸
- 每次 refresh 成本 ~10 分钟；rewrite 整个 session 成本 ~1 小时
- v1.1 时所有 13 个一上来都详写，结果 6 个被 refresh 改了大半

## 不要做的事

- 不要一个 session 跑两个 prompt 文件（context 会乱）
- 不要把 `<file>.md` 删掉——它是历史档案，Findings 是下游 session 的输入
- DRAFT 的 prompt 不要在 v2-00/v2-01 执行期间提前批量改——等真开始时再 refresh
