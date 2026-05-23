# Session 03+ — Absorbed, no longer needed  `[RETIRED]`

| field | value |
|---|---|
| Phase | n/a |
| Prerequisites | — |
| Estimated PRs | 0 |
| Estimated LOC | 0 |
| Status | retired — 2026-05-21 |

## 为什么 retire

原 03+ 的两个任务都被上游 session 吸收：

| 原任务 | 新归属 | 理由 |
|---|---|---|
| Drop `coworkers.tools` JSONB 列 | **02b 同 commit 完成** | greenfield 姿态下 02b 简化为"一次性 drop + 全 reader 切"单 PR；不再需要"stage 2 跑稳后 stage 3 drop"的 timing 约束 |
| Drop `skills.coworker_id` 列 | **03b 同 commit 完成** | 03b 做 skills per-tenant 迁移本来就要碰这块；greenfield 下 drop 与 reader 切可同 commit |

原计划的"三阶段下线 stage 3"是**production multi-tenant** 系统的保守 migration 模式（stage 1 双写 → stage 2 reader 切 → stage 3 drop 列要等长时间观察期）。在 greenfield 姿态下（dev DB 只有测试数据，可清可重建），三阶段安全网不 earn its keep——切换瞬间任何漏改的 reader 会立即在 dev smoke 暴露，不需要 prod-style "stage 2 跑一周才 drop"的兜底窗口。

## 历史档案

如果未来项目进入 production 阶段，需要真正的渐进式 schema 迁移（保留生产数据 + 不中断在线服务 + 多机滚动发布），可以回滚本文件的 git 历史：

```
git log --follow -- docs/webui-backend-v1.1-sessions/03plus-drop-tools-column.md
```

之前版本包含完整的 pre-flight grep / 双写一致性验证 / 长观察期 / DROP COLUMN 不可逆告警等 production-grade 检查清单。**那些内容仍然有效**——只是不在 v1.1 greenfield 范围内使用。

## 关联 commit

- `6c7f013 docs(v1.1): collapse 02b to single-PR drop under greenfield stance` —— 把 tools 列 drop 吸收进 02b 的决定
- `ba1c78f docs(v1.1): pivot 00b prompt to greenfield-schema stance` —— greenfield 姿态确立的源头（00b 的 pivot）

## 对 plan.md 的影响

`docs/webui-backend-v1.1-plan.md` 状态表里 03+ 行应该改为 `retired` 而不是 `not started`。02b / 03b 完成时应在各自 Findings 段确认"已含 drop 列"。
