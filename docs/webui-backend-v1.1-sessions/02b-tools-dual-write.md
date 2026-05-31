# Session 02b — `coworker.tools` 一次性下线（greenfield）  `[DRAFT]`

| field | value |
|---|---|
| Phase | 2 |
| Prerequisites | 02a done（`coworker_mcp_servers` 关系层 endpoint 已可用） |
| Estimated PRs | 1-2 |
| Estimated LOC | ~300 |
| Status | not started — DRAFT |

> **DRAFT**：reader 站点列表会随 Phase 1 / 02a 引入新 reader 而变化。执行前必须重 grep。
> **Greenfield 简化（与原 DRAFT 不同）**：dev DB 只有测试数据 → 不走"stage 1 双写 → stage 2 reader 切 → stage 3 drop"三阶段。一次性 drop `coworkers.tools` 列 + 全 reader 切到 `coworker_mcp_servers` + 写入路径只写新表。原计划的独立 03+ "drop tools 列" session 因此被本 session 吸收。

## Goal

把 `coworker.tools` JSONB 列彻底从 schema 和代码里清掉，写入与读取全切到 02a 落下的 `mcp_servers` + `coworker_mcp_servers` 关系层。**单 commit 一刀切**——schema drop + 写入路径只写新表 + 10+ 处 reader 全切 + grep 验证清空 + 测试 fixture 更新。

**Greenfield 姿态（已锁定，与 00b/02a 一致）**：当前 dev DB 只有测试数据，drop column 是允许操作；不需要 stage 1 双写的安全网，不需要 backfill 旧数据，不需要"跑稳定后再 drop"的 timing 约束。

但下列**不打折**（架构质量与 INV，与是否生产无关）：

- 所有 reader 真切到 `coworker_mcp_servers`，grep 验证清空
- 写入路径事务保证（先 INSERT junction，再 commit；不允许部分写）
- pi/ 下的 `.tools`（LLM tool list，与 `coworker.tools` JSONB 无关）**绝不触碰**
- coworker 启动 / 重启路径仍正确投影 MCP 配置到容器
- Phase 1 e2e 重跑不退化（chat + coworker 用 MCP tool）

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §9.3（注意：§9.3 写的是三阶段，本 session 走 greenfield 简化版，不照搬）
2. 02a Findings —— `coworker_mcp_servers` 关系层 endpoint 实际签名 + `enabled_tools` 三态语义 (NULL/[]/[...])
3. Plan critique §6 —— grep 命令的收敛版本
4. **进 session 第一件事**：跑 reader baseline grep，把输出贴到 session 开头：
   ```bash
   grep -rn "coworker.*\.tools\b\|cw\.tools\b" src/ tests/ scripts/ container/ 2>/dev/null | grep -v __pycache__
   ```

## Scope — PR sketch

### PR 1 — drop `coworkers.tools` 列 + 全 reader 切 + 写入路径只写新表

**子任务**（顺序执行，**同一 commit**）：

1. **Schema 改动**：
   - `ALTER TABLE coworkers DROP COLUMN tools`（greenfield 直接 drop）
   - `src/rolemesh/db/schema.py` 同步删 `tools JSONB DEFAULT '[]'` 列定义
   - 若有相关索引 / CHECK 也一并删

2. **写入路径**（`src/rolemesh/db/coworker.py` 等）改成只写 `coworker_mcp_servers`：
   - `create_coworker` 接 `mcp_configs` 参数 → 同一事务内 INSERT `coworker_mcp_servers` 行
   - `update_coworker` 改 mcp 时 DELETE + INSERT junction 行
   - 旧 `tools JSONB` 写入逻辑全删
   - 事务保证：写入 coworkers 行 + junction 行在同一 `conn.transaction()` 内

3. **Reader 全切**（按 baseline grep 列出的位置）：
   - `src/rolemesh/main.py:208 / 419 / 444` —— orchestrator 启动 / 重启 / IPC payload 构造
   - `src/rolemesh/agent/container_executor.py:256 / 264`
   - `src/rolemesh/evaluation/freeze.py:83` / `cli.py:103`
   - `src/webui/admin.py:152 / 225 / 453 / 499 / 524`
   - `src/rolemesh/egress/orch_glue.py:289` / `mcp_cache.py`
   - 每个 reader 改成走 `coworker_mcp_servers` JOIN `mcp_servers`，按 `enabled_tools` 决定哪些 tool 暴露给容器

4. **Helper 抽取**（如有重复）：
   - 多个 reader 都需要"给我这个 coworker 的所有 enabled MCP server + 各自 enabled_tools"
   - 抽 `src/rolemesh/db/coworker_mcp.py::list_coworker_mcp_configs(coworker_id, tenant_id) -> list[McpServerConfig]`
   - 一处实现，统一 RLS + 双层防御 + `enabled_tools` 三态解读

5. **测试 fixture 更新**：
   - `tests/conftest.py` 或 coworker factory：创建 coworker 时不再传 `tools=...`，改传 `mcp_configs=...`
   - 凡有 `coworker.tools` 断言的测试，改成查 `coworker_mcp_servers` row

6. **grep 验证清空**：
   ```bash
   grep -rn "coworker.*\.tools\b\|cw\.tools\b" src/ tests/ scripts/ container/ 2>/dev/null \
     | grep -v __pycache__ \
     | grep -v "context\.tools"   # pi 的合法 LLM tool list
   ```
   输出**必须**为空（schema.py / db/coworker.py 写入处也清掉了，因为列已 drop）

7. **admin endpoint 兼容**：`/api/admin/agents/*` POST/PATCH 之前接 `tools` 字段 body；改成接 `mcp_configs`（或保留 `tools` 字段名但内部映射到 junction）。如果改名要同时改 admin frontend——视范围决定。**建议**：保留旧字段名 `tools` 作为 wire enum，内部映射，避免动 admin frontend

### PR 2 (可选) — 删除遗留代码 / 重构

如果 PR 1 commit 后还有"为了双写而存在的 adapter / legacy converter / backfill helper"等历史代码，本 PR 删干净。**预期一般不需要**——greenfield 一次性切完没有 transition 代码。

## Acceptance criteria

- [ ] `pytest` 全套通过（特别是 tests/db/test_coworker_*.py 以及任何用 `tools=` factory 的测试）
- [ ] 上面 grep 输出为空（除 pi/ 下的 `context.tools`）
- [ ] Phase 1 e2e 重跑：chat + coworker 用 MCP tool 不退化
- [ ] 02a Phase 2 smoke 重跑：configure credential → bind MCP server → coworker 用 MCP tool 成功
- [ ] `coworkers.tools` 列**不在** schema 里（`\d coworkers` 验证）
- [ ] 测试 fixture 全部用新参数（`mcp_configs` 或等价）
- [ ] OpenAPI yaml 同步：admin endpoint 的 `tools` 字段 schema 是否要改，由 session 决定后更新；codegen 一致性测试绿
- [ ] 更新 plan 状态

## Out of scope

- ❌ 任何新业务 endpoint（v1 / admin 都不加新的）
- ❌ 触碰 pi/ 下的 `tools`（LLM tool list，不同语义）
- ❌ MCP server 自身 CRUD（02a 已落地）
- ❌ user-mode MCP token 注入（02c）
- ❌ `channel_bindings.credentials` 明文迁 vault（独立 chore，已知技术债）

## Open questions

全部已解决（greenfield 姿态下不再适用）：

1. ~~stage 1 双写 vs 一刀切~~ → **一刀切**（greenfield，dev DB 可清）
2. ~~Backfill script~~ → **不需要**（dev DB 没有"老数据要保留"）
3. ~~何时 drop 列 / 何时开 03+~~ → **本 session 同 commit drop**，无独立 03+ session

仍需 session 内决策的：

1. **admin endpoint wire 字段名**：保留 `tools` 字面量（前端兼容）vs 改成 `mcp_configs`（命名一致）。推荐前者——避免动 admin frontend
2. **`enabled_tools` 默认值**：02a 已决定 NULL=全启用；本 session 写入路径不传时默认 NULL

## Pitfalls

- **pi/ 下的 `.tools` 不是同一个东西**——pi 用 `context.tools` 表示传给 LLM 的 tool list（function calling），与 `coworker.tools` JSONB（MCP server config）无关；grep 时主动排除 `context\.tools`
- **写入必须在事务内**：coworkers INSERT + junction INSERT 同事务，半写状态会让 orchestrator 启动时拿到不全的 MCP 配置
- **`mcp_servers` 表是 tenant 级（UNIQUE tenant_id, name）**：写入 junction 前若 `mcp_server_id` 不存在直接报错（INSERT FK 违约）——不要在本 session 自动 upsert 缺失的 mcp_servers 行（那是 02a 的 endpoint 职责，本 session 假设上游已经创建好）
- **grep baseline 在 session 开头跑一次，结尾再跑一次比对**——这是验收的硬标准。如果结尾 grep 不空，session 不算 done
- **`enabled_tools` 三态语义**（02a 锁定）：`NULL=全启用`、`[]=全禁`、`[...]=白名单`。reader 切换时三态必须保留——容易写成"NULL 当 [] 处理"，这样所有 coworker 突然没 MCP tool
- **测试 fixture 的连锁影响**：很多 conftest / factory 隐式传 `tools=[...]`，改 schema 后这些 fixture 一起爆。session 开始时先列出受影响 fixture 的清单
- **schema migration 顺序**：必须先在代码层确保所有写入路径不再写 `coworkers.tools`，**再** drop 列；否则启动后 INSERT 直接报"column does not exist"。Greenfield 下其实没风险（DB 可清重建），但好习惯还是按这个顺序

## 执行前刷新清单（DRAFT 状态）

- [ ] 02a 完成？`coworker_mcp_servers` 关系层 endpoint + `mcp_servers` CRUD 可用？
- [ ] 重新跑 reader grep，把 baseline 更新到 prompt 里（数字可能与原 prompt 写的"10+ 处"不一致）
- [ ] admin endpoint wire 字段名决策（保留 `tools` vs 改 `mcp_configs`）
- [ ] 03+ session prompt 现在变成只剩 `skills.coworker_id` drop——是否合并进 03b？（推荐合并：03b 做 skills per-tenant 迁移本来就要碰这块）

## Findings (after execution, 2026-05-21)

**Reader baseline (entry grep)** —— 17 hits, 9 of them real readers / writers (the rest were comments or pi `.tools` context). Real sites switched:

- `src/rolemesh/main.py:430` —— orchestrator startup MCP register loop. Now iterates `cw_state.mcp_configs` (loaded from the relation projection at state-load time).
- `src/rolemesh/agent/container_executor.py:256,264` —— spec builder. New `get_mcp_configs(coworker_id)` callable on the executor constructor; orchestrator + eval CLI both feed it.
- `src/rolemesh/evaluation/cli.py:103` —— `_user_mode_mcp_servers` re-signatured to take a `Sequence[McpServerConfig]`. Caller fetches via `list_coworker_mcp_configs` and reuses the cache for the executor.
- `src/rolemesh/evaluation/freeze.py:83` —— `_coworker_to_dict` now takes `mcp_configs` and projects them into the eval-run snapshot.
- `src/webui/admin.py:155,228,465,509,1127` —— `_publish_mcp_for_coworker(action, cw, mcp_configs)`, `_coworker_to_response(cw, mcp_configs)`, `list_agents` / `get_agent_detail` / `create_agent` / `update_agent` all read via the new helper; safety-rule reversibility guard also re-fetches.
- `src/rolemesh/db/coworker.py` —— `create_coworker` / `update_coworker` no longer accept `tools=`; `_record_to_coworker` no longer parses a `tools` column.

**Helper signatures** (new in `src/rolemesh/db/coworker_mcp.py`):

```python
async def list_coworker_mcp_configs(
    coworker_id: str, *, tenant_id: str,
) -> list[McpServerConfig]
```

JOINs `coworker_mcp_servers` to `mcp_servers`, ordered by server `name`. Three-state `enabled_tools` is **not** applied at this layer (returns every bound server; per-tool filtering belongs to the SDK's tool-allowlist).

```python
async def replace_coworker_mcp_configs(
    coworker_id: str, *, tenant_id: str,
    mcp_configs: Sequence[McpServerConfig],
) -> None
```

Transactional convenience for the legacy admin POST/PATCH wire and test fixtures: DELETE existing junctions, upsert `mcp_servers` rows by `(tenant_id, name)`, INSERT junctions with `enabled_tools=NULL`. The v1 relation API (`POST /api/v1/coworkers/{id}/mcp-servers`) still requires explicit `mcp_server_id`; no auto-upsert there.

**Callers of `list_coworker_mcp_configs`**:

- `src/rolemesh/main.py` —— orchestrator state load + hot-reload subscriber.
- `src/rolemesh/orchestration/coworker_hot_reload.py` —— `reload_coworker_into_state` (when caller passes `fetch_mcp_configs`) and new `reload_coworker_mcp_into_state`.
- `src/rolemesh/evaluation/freeze.py` —— eval snapshot.
- `src/rolemesh/evaluation/cli.py` —— pre-flight `--user` check + executor cache.
- `src/webui/admin.py` —— `_load_mcp_configs` wrapper used by every reader.

**Schema migration** —— `src/rolemesh/db/schema.py`:

1. Removed the inline `tools JSONB DEFAULT '[]'` column from the `coworkers` CREATE TABLE.
2. Removed `tools` from the legacy roles-migration backfill loop.
3. Added idempotent `ALTER TABLE coworkers DROP COLUMN IF EXISTS tools` after the existing skill-drop guard so pre-greenfield dev DBs converge on a fresh testcontainer's schema without a manual migration.

No back-compat shim: the dataclass field is gone, the column is gone, the create/update API no longer accepts the parameter.

**Open question decisions** (session-internal):

- Admin endpoint wire field = retained literal `tools`. The v1.1 design's admin frontend doesn't currently send it (02a built Models / Credentials / MCP pages instead), so the wire surface is only exercised by historical tests; keeping the name avoids a follow-up if the admin UI grows a tool-editing surface.
- `enabled_tools` default on writes = `NULL` (three-state "all enabled") in `replace_coworker_mcp_configs`. Matches 02a's locked semantics.

**Hot-reload subscriber** —— added `subscribe_coworker_mcp_changed` (durable `orch-web-coworker-mcp-changed`) alongside the existing `subscribe_coworker_restart`. The v1 relation endpoint already publishes `web.coworker.mcp_changed`; until this commit the orchestrator's `CoworkerState.mcp_configs` projection only refreshed on the next process restart.

**Test surgery**:

- `tests/db/test_pg.py`, `tests/safety/test_rest_validation.py`, `tests/safety/e2e/test_reversibility_roundtrip.py` —— swapped `create_coworker(tools=...)` for `create_coworker(...)` + `replace_coworker_mcp_configs(...)`; read-side asserts go through `list_coworker_mcp_configs`.
- `tests/core/test_types.py:89` —— removed the `cw.tools == []` default assertion.
- `tests/evaluation/test_user_mode_check.py` —— retargeted at the new `_user_mode_mcp_servers(mcp_configs)` signature; dropped the `_FakeCoworker` mock.
- `tests/egress/test_admin_mcp_publish.py` —— `_coworker_with_tools` returns `(Coworker, list[McpServerConfig])`; every `_publish_mcp_for_coworker` call now passes both.

**Grep verification** —— end-of-session run of the baseline grep is empty modulo:

- `src/rolemesh/main.py:424` (comment that documents the historic `coworkers.tools` column).
- `src/rolemesh/db/schema.py:193` (comment in the legacy roles-migration block about `roles.tools` rows being silently dropped).

Both are documentation references in the migration code itself.

**Impact on 02c / 03b**:

- 02c (user-mode credential injection) is independent — the relation projection layer already carries `auth_mode` per server, which is what the credential proxy keys on.
- 03b inherits a cleaner starting point: `Coworker` no longer carries any inline collection that 03b's per-tenant `skills` move would have to keep in lockstep. The `skills.coworker_id` drop stays the only schema change remaining for 03b.
