# Session 00b — Migrations + RLS

| field | value |
|---|---|
| Phase | 0 |
| Prerequisites | 00a done（INV 基建已落地，audit helper / BOOTSTRAP_USERS 已可用） |
| Estimated PRs | 2-3 |
| Estimated LOC | ~600 (SQL + Python migration runner + 测试) |
| Status | not started |

## Goal

把设计 §2.1 / §2.2 所有新表与列 ADD 一次性落到 schema 与 RLS policy 里。**不写业务 API**，但要写到"第一个调用方"——否则 RLS bypass 测不出来（INV-1 belt-and-braces 必须同 session 验证）。

**Greenfield 姿态（已锁定）**：当前 dev DB 只有测试数据，不需要考虑生产 migration / 数据保留 / 回滚兼容。schema.py 是"目标 schema 的真值源"，drop-and-recreate 是允许的操作。这意味着：

- 不需要 `ADD COLUMN IF NOT EXISTS` 之类的 migration 兼容写法（用 plain `ADD COLUMN` 即可，schema.py 仍走 `CREATE TABLE IF NOT EXISTS` 保证 re-run 不抛）
- 不需要数据 backfill（NULL 即可，业务层 / Phase 1+ 写新数据时自然带值）
- 不需要 pg_dump 备份
- 不需要 skills 重名冲突的"先 dry-run + 停下让人决定"流程——直接加 UNIQUE 约束；testcontainer 起来就是空表
- 生产环境的真 migration 留给未来 pre-prod cutover 时单独规划，**不在本 session 范围**

但下列要求**不打折**（架构质量与 INV，与是否生产无关）：

- 完整 RLS policy + 双层防御
- INV-1 lint
- belt-and-braces RLS 隔离测试
- schema.py 的 idempotency（同样 schema.py 跑两次不抛）
- 约束、索引、默认值与设计文档完全一致

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

### PR 2 — 现有表加列 + 新约束

按设计 §2.2 给 `coworkers / skills / messages` 加列、加约束。**不需要 backfill**——dev DB 只有测试数据，新列留 NULL，业务层（Phase 1+）写新数据时自带值；testcontainer 起来就是空表。

schema.py 的写法应该是**幂等 CREATE / ALTER**——能在干净 DB 上一次跑出目标 schema，也能在已应用过的 DB 上 re-run 不抛。具体 SQL：

```sql
ALTER TABLE coworkers
  ADD COLUMN IF NOT EXISTS model_id           UUID REFERENCES models(id),
  ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id);  -- NULLABLE

ALTER TABLE skills
  ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id);  -- NULLABLE

ALTER TABLE skills
  ADD CONSTRAINT skills_tenant_name_unique UNIQUE (tenant_id, name);
  -- PG 11 之前没有 ADD CONSTRAINT IF NOT EXISTS；用 DO $$ BEGIN ... EXCEPTION ... END $$ 包，
  -- 或者查 pg_constraint 系统表先决定建不建。schema.py re-run 不能抛 "constraint already exists"。

ALTER TABLE messages
  ADD COLUMN IF NOT EXISTS run_id UUID REFERENCES runs(id);
```

**列默认值与可空性约束**：
- `coworkers.model_id` —— NULL（业务层默认值或 Phase 1 后必填，本 session 不强制）
- `coworkers.created_by_user_id` —— NULLABLE（L6 强约束：audit FK 必须能容 NULL）
- `skills.created_by_user_id` —— 同上
- `messages.run_id` —— NULL，01a 写入路径打通后新消息有值

**单测**：

新建 `tests/test_schema_alters.py`（testcontainer 起空 DB）：
- 测 schema.py 是 idempotent（连跑两次不抛）
- 测新列默认 NULL
- 测 `skills_tenant_name_unique` 真生效（插两条同 tenant 同名 → 第二条 IntegrityError）
- 测 `messages.run_id` FK 真生效（插不存在的 run_id → IntegrityError）

**Acceptance**：
- 干净 testcontainer 上 schema.py 跑通，新列 + 约束都在
- schema.py 连跑两次不抛
- 单测全绿
- 现有 coworker / skills / messages 单测不退化（dev 的测试 DB 行为不变）

### PR 3 (推迟) — schema_migrations 版本表

**本 session 不做**。理由：greenfield 姿态下，drop-and-recreate 是允许操作，不需要版本追踪；schema.py 自身的 idempotency 已能覆盖 dev 场景。版本追踪是**生产 cutover 时**才需要的能力，留给那时单独 session 规划（届时大概率会一起决定 alembic vs. 手卷方案）。

如果执行中发现 `schema.py` 已经有某种半成品版本机制（少见），**记录到 Findings 段**让用户决定要不要清理，本 session 不动它。

## Acceptance criteria（session 级）

- [ ] `pytest tests/test_inv1_tenant_predicate_lint.py tests/test_tenant_isolation_belt_and_braces.py tests/test_schema_alters.py` 全绿
- [ ] 在干净 testcontainer 上 `python -m rolemesh.db.schema` 跑完后，所有新表 + 新列存在（`psql` / `\d` 验证）
- [ ] schema.py 连跑两次不抛（idempotency）
- [ ] `models` 表有 seed 数据
- [ ] 全套现有测试不退化（含 dev 数据库重建后的现有功能测试）
- [ ] 手动 smoke：开两个 tenant connection，SELECT 互不可见（RLS 生效）
- [ ] 更新 `docs/webui-backend-v1.1-plan.md` 状态

## Out of scope

- ❌ Coworker model_id 选择 UI / API（留 Phase 2）
- ❌ 任何 `/api/v1/*` endpoint（除了 00a 已建的 `/api/v1/backends`，本 session 不加新的）
- ❌ `coworker.tools` 双写逻辑（留 02b）
- ❌ runs 表写入路径（留 01a/01b）
- ❌ skills per-tenant 数据迁移（留 03b；本 session 只加 unique constraint）

## Open questions

全部已解决（greenfield 姿态下不再适用）：

1. ~~`coworkers.model_id` backfill 默认值~~ → **不 backfill**（greenfield，留 NULL）
2. ~~现有 skills 重名冲突处理~~ → **不存在**（dev 数据可清；testcontainer 起空表）
3. ~~schema_migrations 版本表~~ → **本 session 不做**（推迟到生产 cutover）

## Pitfalls

- **`CREATE POLICY` 不是 idempotent**——PG 不支持 `CREATE POLICY IF NOT EXISTS`。要 `DROP POLICY IF EXISTS ... ; CREATE POLICY ...` 模式，或者用 `pg_policies` 系统表查再决定建不建
- `ALTER TABLE ADD CONSTRAINT` 默认不 idempotent；schema.py re-run 时会抛 "constraint already exists"。用 `DO $$ BEGIN ... EXCEPTION WHEN duplicate_object THEN NULL; END $$` 包，或查 `pg_constraint` 系统表先决定建不建
- `models.is_platform` 默认 TRUE 是设计意图（v2 时 per-tenant 自定义模型才设 FALSE）；不要因为看不出意义就删
- `coworker_mcp_servers.enabled_tools TEXT[] DEFAULT NULL` —— NULL 代表"全启用"，空数组 `'{}'` 代表"全禁"——语义不同。RLS / API 层都要尊重这个区别，**不要在 migration 默认成 `'{}'`**
- 双层防御 lint 不要走"AST 解析 Python 字符串"那么重的方案——简单 `grep -n` + 正则就够，宁可漏过少数 false positive 也别引入 babel 级别复杂度
- Greenfield 姿态不是"可以糙"——架构、约束、RLS、INV 测试一个都不能省。只是省掉了"数据保留 / 渐进 rollout"那层 ceremony

## Findings (after execution)

_(empty — 重点记录：schema.py idempotency 实现细节（DO $$ ... EXCEPTION 包还是查 pg_constraint）？INV-1 lint 的 false positive 数量？双层防御测试覆盖到了哪几张表？对 01a 的影响（如新表 CRUD 风格、RLS session 设置惯例）？)_
