# Session 02b — `coworker.tools` 双写 + reader 切换  `[DRAFT]`

| field | value |
|---|---|
| Phase | 2 |
| Prerequisites | 02a done（`coworker_mcp_servers` 关系层 endpoint 已可用） |
| Estimated PRs | 2 |
| Estimated LOC | ~600 |
| Status | not started — DRAFT |

> **DRAFT**：reader 站点列表会随 Phase 1 / 02a 引入新 reader 而变化。执行前必须重 grep。
> **重要**：本 session **不 drop `coworkers.tools` 列**。drop 列必须等本 session 双写跑一段时间（建议至少跑通 Phase 3）后单独开 03+ session。

## Goal

落地设计 §9.3 的三阶段下线 stage 1 + 2：写入路径双写到 `coworker_mcp_servers`，所有 reader 切到新关系表。**保留 `coworkers.tools` JSONB 列**，stage 3 drop 留 03+。

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §9.3
2. 02a Findings —— 看 `coworker_mcp_servers` 关系层 endpoint 实际签名
3. Plan critique §6 —— grep 命令的收敛版本
4. 跑一次 grep（在 session 开始时）：
   ```bash
   grep -rn "coworker.*\.tools\b\|cw\.tools\b" src/ tests/ scripts/ container/ 2>/dev/null | grep -v __pycache__
   ```
   把输出贴到 session 开头作为 reader 清单 baseline

## Scope — PR sketch

### PR 1 — 写入路径双写

- 找到所有写入 `coworkers.tools` 的路径（`grep -rn "tools = " src/rolemesh/db/coworker.py` + admin.py 等）
- 每个写入路径同时：
  1. 旧：写 `coworkers.tools` JSONB
  2. 新：根据 `tools` 内容 INSERT/UPSERT/DELETE `coworker_mcp_servers` rows
- **依赖**：`tools` 里的 `McpServerConfig.name` 必须能映射到 `mcp_servers.id`。如果 Phase 1/02a 之前的 coworker.tools 引用的 server name 在 `mcp_servers` 表里不存在，先**自动 upsert**一条 `mcp_servers` row（保 backward compat），并记 audit log
- 双写在事务内，保证一致
- pinned test：双写后 `coworker.tools` JSONB 与 `coworker_mcp_servers` row 内容等价

### PR 2 — Reader 全切到关系表

按 baseline grep 列出的位置一个一个切：

- `src/rolemesh/main.py:208 / 419 / 444` —— orchestrator 启动 coworker / 重启 / IPC payload 构造
- `src/rolemesh/agent/container_executor.py:256 / 264`
- `src/rolemesh/evaluation/freeze.py:83` / `cli.py:103`
- `src/webui/admin.py:152 / 225 / 453 / 499 / 524`
- `src/rolemesh/egress/orch_glue.py:289` / `mcp_cache.py`

每个切完后跑：
```bash
grep -rn "coworker.*\.tools\b\|cw\.tools\b" src/ | grep -v "coworkers\.tools[^=]" | grep -v __pycache__
```
应只剩 db/coworker.py 写入处和 schema.py。

**注意 pi/ 下的 `.tools`**——pi 用的是 LLM tool list（`context.tools`），与 `coworker.tools` JSONB 无关，**不要动**。

## Acceptance criteria

- [ ] 所有 reader 已切；上面 grep 输出为空（除写入处）
- [ ] 双写一致性 pinned test 绿
- [ ] Phase 1 e2e 重跑：chat + coworker 用 MCP tool 不退化
- [ ] 02a service-mode MCP smoke 在新 reader 路径下重跑通过
- [ ] `coworkers.tools` 列还在（drop 留 03+）
- [ ] 更新 plan 状态

## Out of scope

- ❌ **drop `coworkers.tools` 列** —— 必须留独立 03+ session，且本 session 跑一段时间后才能开
- ❌ 任何新业务 endpoint

## Open questions

1. **`coworker.tools` 引用未知 mcp_server name 时**自动 upsert vs. fail-fast？autosert backward compat；fail-fast 数据更干净。推荐 upsert + log warn。
2. **现有 coworker 的 tools backfill**：要不要写一个一次性 backfill script 把所有现有 `coworkers.tools` JSONB 转成 `coworker_mcp_servers` rows？还是只在下次 update 时双写触发？推荐前者——避免长尾未触发的 coworker 拖延 drop 列时机。
3. **`enabled_tools` 字段**：现有 `coworker.tools` JSONB 里有没有 per-tool 启停信息？如果有就直接迁到 `coworker_mcp_servers.enabled_tools`；没有则全设 NULL（=全启用）。

## Pitfalls

- pi/ 的 `tools` 不是同一个东西——别误改
- 写入双写必须在事务内——半写状态会很难 debug
- `mcp_servers` 表里 server 是 tenant 级的（UNIQUE tenant_id, name），跨 tenant 自动 upsert 可能产生冲突；upsert 时一定带 tenant_id
- backfill script 必须 idempotent（用 ON CONFLICT DO NOTHING）
- grep baseline 在 session 开头跑一次，结尾再跑一次比对——是验收的硬标准

## 执行前刷新清单

- [ ] 02a 完成？`coworker_mcp_servers` 关系层 endpoint 可用？
- [ ] 重新跑 reader grep，把 baseline 更新到 prompt 里
- [ ] backfill script 是否做？决定后改 PR 数

## Findings (after execution)

_(empty — 重点记录：reader 实际数量、有没有遗漏、backfill 是否做)_
