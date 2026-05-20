# Session 00b — Migrations + RLS

| field | value |
|---|---|
| Phase | 0 |
| Prerequisites | 00a done（INV 基建已落地，audit helper / BOOTSTRAP_USERS 已可用） |
| Estimated PRs | 2-3 |
| Estimated LOC | ~600 (SQL + Python migration runner + 测试) |
| Status | not started |

## Goal

把设计 §2.1 / §2.2 所有新表与 ALTER 一次性落到 schema 与 RLS policy 里。**不写业务 API**，但要写到"第一个调用方"——否则 RLS bypass 测不出来（INV-1 belt-and-braces 必须同 session 验证）。

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §2（数据模型）/ §8（多租户表格）/ §11 INV-1
2. [`docs/18-rls-architecture.md`](../18-rls-architecture.md) —— 现有 RLS 模式
3. `src/rolemesh/db/schema.py` —— 现有 schema 创建逻辑，理解 migration 是 idempotent CREATE / ALTER 模式（不是 alembic）
4. `src/rolemesh/db/_pool.py` —— `tenant_conn` / `admin_conn` 的使用模式（与 RLS 配合）
5. `src/rolemesh/db/coworker.py` —— 现有 coworker CRUD 模式，新表 CRUD 风格保持一致
6. 00a Findings 段（若有）—— 看看 audit helper 实际签名是什么，是否影响 migration

## Scope — PR breakdown

### PR 1 — 新表 + RLS policy + 双层防御 lint

**Tables**（按依赖顺序 CREATE）：

1. `models`（无 RLS，平台表）
2. `tenant_model_credentials`（RLS on `tenant_id`）
3. `mcp_servers`（RLS on `tenant_id`）
4. `coworker_mcp_servers`（无独立 RLS，依赖 `coworkers.tenant_id`）
5. `coworker_skills`（同上）
6. `runs`（RLS on `tenant_id`）

**RLS policy 模板**（每张 tenant 表都要）：

```sql
ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;

CREATE POLICY <table>_tenant_isolation ON <table>
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

**Models 表 seed**（必须在 migration 内）：

- Anthropic：`claude-opus-4-7` / `claude-sonnet-4-6` / `claude-haiku-4-5-20251001`（看 `src/rolemesh/core/types.py` 现有 backend 配置）
- OpenAI / Google / Bedrock：从 `src/rolemesh/agent/` 找现有支持的列表抄
- ON CONFLICT (provider, model_id) DO NOTHING — idempotent

**双层防御 lint（INV-1）**：

新建 `tests/test_inv1_tenant_predicate_lint.py`：
- grep `src/rolemesh/db/` 所有 SQL 字符串
- 对 tenant-scoped 表（`coworkers / mcp_servers / runs / tenant_model_credentials / approval_requests / ...` —— 维护一个 known 集合常量）的 `SELECT` / `UPDATE` / `DELETE` 强制要求 `WHERE tenant_id =` 字样
- 缺则 lint 失败 + 明确指出文件:行号
- 例外白名单（如 join 在父表已带 tenant_id 的）走显式 `# inv-1-ok: <reason>` 注释豁免

**RLS bypass smoke test**（INV-1 另一半）：

新建 `tests/test_tenant_isolation_belt_and_braces.py`：
- 用真 postgres testcontainer
- 创建两个 tenant，各插一条 `mcp_servers` / `runs` / `tenant_model_credentials`
- 用 tenant A 的 session，SELECT 不带显式 `WHERE tenant_id` → RLS 应只返 tenant A 的行（防御 1）
- 用 admin connection（绕过 RLS）+ 显式 `WHERE tenant_id = $A` → 只返 tenant A 的行（防御 2）
- 故意写一条不带 `WHERE tenant_id` 的 query 走 admin connection → 返两个 tenant 的行（演示为啥需要 lint）

**Acceptance**：
- 6 张表全建 + RLS enabled
- `models` 表 seed 跑通
- lint 测试在当前 code 下绿色（必要时调整 known 表集合）
- belt-and-braces 测试三个分支都过

### PR 2 — 现有表 ALTER

按设计 §2.2：

```sql
ALTER TABLE coworkers
  ADD COLUMN IF NOT EXISTS model_id           UUID REFERENCES models(id),
  ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id);  -- NULLABLE

ALTER TABLE skills
  ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id);  -- NULLABLE

-- 新约束（先检查现有数据是否冲突，再加）
ALTER TABLE skills
  ADD CONSTRAINT IF NOT EXISTS skills_tenant_name_unique UNIQUE (tenant_id, name);

ALTER TABLE messages
  ADD COLUMN IF NOT EXISTS run_id UUID REFERENCES runs(id);
```

**数据 backfill 决策**：

- `coworkers.model_id`：所有现有 coworker 没值。option a) 不 backfill（NULL，API 层兜底默认）；option b) 按 `agent_backend` 推断（claude → 一个默认 claude model_id）。**推荐 b**——避免 Phase 1 写 API 时再处理 NULL。具体默认 model 在 migration 里选 `claude-opus-4-7`（或 session 内问 reviewer）。
- `coworkers.created_by_user_id`：留 NULL（L6 强约束）。
- `skills.created_by_user_id`：同上。
- `skills_tenant_name_unique`：执行前 `SELECT tenant_id, name, count(*) FROM skills GROUP BY 1,2 HAVING count(*) > 1` 检查重名；有则**先报错让 reviewer 决定**（不要自动 rename / merge）。
- `messages.run_id`：留 NULL，01a 写入路径打通后新消息有值。

**单测**：

新建 `tests/test_schema_alters.py`：
- 测 ALTER 是 idempotent（连跑两次不抛）
- 测新列默认 NULL
- 测 `skills_tenant_name_unique` 真生效（插两条同名 → 第二条 IntegrityError）
- 测 `messages.run_id` FK 真生效（插不存在的 run_id → IntegrityError）

**Acceptance**：
- 全部 ALTER 跑通（含已有 coworker / skills / messages 的现有数据）
- 单测全绿
- 现有 coworker / skills / messages 业务功能不退化

### PR 3 (optional) — Migration 运行器加 dry-run + version 标记

如果当前 `db/schema.py` 没有干净的"已应用 migration"记录机制，这个 session 趁机补：

- 加 `schema_migrations` 表（如果还没）记录 `(version, applied_at, sha256_of_sql)`
- migration runner 启动时跑 `dry-run` 比对待应用 vs 已应用，给警告
- 不强制 hash 一致（避免格式化 diff 卡住），但记录到表里方便溯源

**判断**：如果 schema.py 已经是清晰的 idempotent CREATE 模式（看了之后再决定），这个 PR 不做。否则做一个最小 version 跟踪。

## Acceptance criteria（session 级）

- [ ] `pytest tests/test_inv1_tenant_predicate_lint.py tests/test_tenant_isolation_belt_and_braces.py tests/test_schema_alters.py` 全绿
- [ ] 在干净数据库上 `python -m rolemesh.db.schema` 跑完后，所有新表 + 新列存在（`psql` 验证）
- [ ] 在已有数据的数据库上跑一次 migration 不抛、不丢数据（重要：跑前 pg_dump 备份）
- [ ] `models` 表有 seed 数据
- [ ] 全套现有测试不退化
- [ ] 手动 smoke：开两个 tenant connection，SELECT 互不可见（RLS 生效）
- [ ] 更新 `docs/webui-backend-v1.1-plan.md` 状态

## Out of scope

- ❌ Coworker model_id 选择 UI / API（留 Phase 2）
- ❌ 任何 `/api/v1/*` endpoint（除了 00a 已建的 `/api/v1/backends`，本 session 不加新的）
- ❌ `coworker.tools` 双写逻辑（留 02b）
- ❌ runs 表写入路径（留 01a/01b）
- ❌ skills per-tenant 数据迁移（留 03b；本 session 只加 unique constraint）

## Open questions

1. **`coworkers.model_id` backfill 默认值**：选哪个 model？推荐 `claude-opus-4-7`，但如果项目内主流 backend 是别的，请明示。
2. **现有 skills 重名（tenant_id + name）冲突处理**：如果检查到重名，**这个 session 直接停**等用户决定（不要静默 rename / 删一个），还是说项目里目前不会有重名？跑 `SELECT tenant_id, name, count(*) ...` 看一下。
3. **schema_migrations 版本表**：现有 `db/schema.py` 是否已有这种机制？没有的话 PR 3 是否做？

## Pitfalls

- **`CREATE POLICY` 不是 idempotent**——PG 不支持 `CREATE POLICY IF NOT EXISTS`。要 `DROP POLICY IF EXISTS ... ; CREATE POLICY ...` 模式，或者用 `pg_policies` 系统表查再决定建不建
- `ALTER TABLE ADD CONSTRAINT IF NOT EXISTS` 在 PG 11+ 才支持。如果项目要兼容更老 PG，用 `DO $$ BEGIN ... EXCEPTION ... END $$` 包
- `models.is_platform` 默认 TRUE 是设计意图（v2 时 per-tenant 自定义模型才设 FALSE）；不要因为看不出意义就删
- `coworker_mcp_servers.enabled_tools TEXT[] DEFAULT NULL` —— NULL 代表"全启用"，空数组 `'{}'` 代表"全禁"——语义不同。RLS / API 层都要尊重这个区别，**不要在 migration 默认成 `'{}'`**
- 跑 backfill `coworkers.model_id` 之前一定先 `BEGIN`，验证后 `COMMIT`；不要直接 ALTER + UPDATE 一气呵成
- 双层防御 lint 不要走"AST 解析 Python 字符串"那么重的方案——简单 `grep -n` + 正则就够，宁可漏过少数 false positive 也别引入 babel 级别复杂度

## Findings (after execution)

_(empty — 重点记录：backfill 策略最终选了哪个 model_id？有没有 skills 重名？migration 在已有数据库上跑的耗时？)_
