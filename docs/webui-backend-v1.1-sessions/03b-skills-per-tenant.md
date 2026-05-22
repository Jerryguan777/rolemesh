# Session 03b — Skills per-tenant 迁移 + UI  `[REFRESHED 2026-05-21]`

| field | value |
|---|---|
| Phase | 3 |
| Prerequisites | 03a done |
| Estimated PRs | 4 |
| Estimated LOC | ~1500 (PR 1 greenfield 切换 + PR 2 命名对齐 + PR 3 v1 endpoints + PR 4 前端) |
| Status | done (2026-05-21) — 4 commits 91c277d / 0b84aba / 542c439 / eef12db on `feat/ui` |

> **Refresh 起源**：03a 落地后 + greenfield 姿态全面应用，本 prompt 大改两处：
> 1. **PR 1 改成 greenfield 一次性切换**（与 02b 同模式）：drop `skills.coworker_id` 列 + reader 全切 + writer 改 + admin auth 改 在**同一 commit**完成。原 DRAFT 的"双写期 → backfill → 03+ drop"路径不再适用。
> 2. **吸收 03+ session 工作**：03+ 已 retire (`d0c7ee2`)，原由它负责的 `skills.coworker_id` drop 在本 session PR 1 完成。
>
> 另外 INV-5 范围缩到 **Python ↔ TS 两处一致** —— DB CHECK 'SKILL.md' 字面量是 over-engineering（app layer 已守，多一层 DB 约束反而绑住未来 manifest rename 空间，无 net 价值）。

## Goal

1. **Greenfield 切换 skills 表语义**：从"per-coworker（每个 skill 属于一个 coworker_id）"改成"per-tenant catalog（skills 按 tenant 共享，通过 `coworker_skills` junction 关联 coworker）"——单 commit 完成 drop column + reader 切 + writer 改
2. `Skill.created_by` → `created_by_user_id` 三层（Python / OpenAPI / TS）对齐（00b 已改 DB 列，三层漂移在此收敛）
3. `/api/v1/skills/*` flat tenant-scoped CRUD + 文件 endpoints + `/api/v1/coworkers/{id}/skills` 关系层
4. Frontend `#/skills` 列表 + `#/skills/:id` 文件树编辑器 + coworker 详情 skills 子面板
5. INV-5 Python ↔ TS `SKILL_MANIFEST_NAME` 一致 lint

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §3 Phase 3 Skills / §6.3 C + G / §11 INV-5
2. [`docs/19-skills-architecture.md`](../19-skills-architecture.md) —— 现有 skills 架构（skills + skill_files 表分工）
3. **02b Findings** —— greenfield 一次性切换的成熟模式（drop column + reader 切 + writer 改 + grep 验证清空），03b PR 1 直接照搬该模式
4. **00a PR1 落地** —— `SKILL_MANIFEST_NAME` Python 常量 + `src/rolemesh/core/skills_consts_pin.py`
5. **00b PR2 落地** —— `skills_tenant_name_unique UNIQUE (tenant_id, name)` 约束 + `skills.created_by` rename → `created_by_user_id`（**仅 DB 列名改了，Python/TS/OpenAPI 三层 PR 2 收敛**）
6. **03a Findings § 3** —— `<rm-inline-approval>` 独立 component pattern（**仅作架构参考**，skills 不需要 inline 桥接 UI——见下"概念定位"）
7. 现有 admin skills endpoints (`src/webui/admin.py:1680+`) —— `/api/admin/agents/{agent_id}/skills/*` 模式，本 session **保留兼容期**，新加 `/api/v1/skills/*` 平面命名

## 概念定位：Skills 是静态配置，不需要 inline UI 桥接

`<rm-inline-approval>`（03a 落地）解决的是"动态、需要决策的事件"——alice 等 bob 批准，agent 卡着。

Skills 是**静态配置**——admin 配好 skill 后 agent 容器启动时投影到 tmpfs，运行期不变。不需要"chat 内联 skill 操作"UI。本 session 只做：

- `#/skills` flat 列表（看 / 编辑全 tenant skills）
- `#/skills/:id` 文件树（编辑某 skill）
- Coworker 详情页 `#/coworkers/:id/skills` 子面板（看 / enable / disable 本 coworker 的 skill 关联）

不要把 inline UI pattern 误用到 skills。

## Scope — PR breakdown

### PR 1 — Greenfield 切换：drop `skills.coworker_id` + reader/writer 全切到 `coworker_skills` 关系层

**Background**：原 DRAFT 走"双写 + backfill + 03+ drop"路径，是 production-grade migration。Greenfield 下 dev DB 可清，且 02b 已经验证过这种一次性切换 pattern 工作良好。**单 commit 完成**，与 02b 同形。

**Baseline grep**（session 第一件事）：

```bash
grep -rn "skills.*coworker_id\|skill\.coworker_id\|Skill.coworker_id" src/ tests/ scripts/ container/ 2>/dev/null | grep -v __pycache__
```

把输出贴在 session 开头作为工作清单。

**子任务**（按顺序，**同一 commit**）：

1. **Schema 改动** (`src/rolemesh/db/schema.py`)：
   - `skills.coworker_id` 列从 `NOT NULL` → 直接 `DROP COLUMN`（greenfield 直接 drop）
   - `idx_skills_coworker ON skills(coworker_id, enabled)` 同步 drop
   - 加 idempotent `ALTER TABLE skills DROP COLUMN IF EXISTS coworker_id`（兼容老 dev DB，与 02b legacy block 同模式）
   - 现有 `tenant_id` + `skills_tenant_name_unique UNIQUE (tenant_id, name)` 已就位（00b PR2），自然成为 per-tenant catalog 主键

2. **Writer 改动** (`src/rolemesh/db/skill.py`)：
   - `create_skill(tenant_id, name, ...)` 移除 `coworker_id` 参数
   - 新加 `enable_skill_for_coworker(skill_id, coworker_id, tenant_id, enabled=True)` —— INSERT/UPDATE `coworker_skills` 关系行
   - 兼容路径：`create_skill_for_coworker(coworker_id, tenant_id, name, ...)` 作为 admin 旧 endpoint 的 transactional convenience helper —— 同事务内先 create_skill + enable_skill_for_coworker（与 02b 的 `replace_coworker_mcp_configs` 同模式）
   - 事务保证：skills 行 + coworker_skills 行同 `conn.transaction()` 内
   - `Skill` dataclass (`src/rolemesh/core/types.py` 或实际位置) 移除 `coworker_id` 字段

3. **Reader 全切**：
   - `src/rolemesh/db/skill.py:180,187` —— `list_skills_for_coworker(coworker_id)` 改成 JOIN：
     ```sql
     SELECT s.* FROM skills s
       JOIN coworker_skills cs ON cs.skill_id = s.id
      WHERE cs.coworker_id = $1::uuid AND cs.enabled = true AND s.enabled = true
     ```
   - **`coworker_skills.enabled` 三态语义**与 02b `coworker_mcp_servers` 不同：`coworker_skills.enabled` 是 boolean（true=启用 / false=显式禁用），与 `skills.enabled`（全局开关）AND 一起决定最终是否生效
   - `src/webui/admin.py:1765,1869,1890,1928` —— 旧 admin auth check `skill.coworker_id == agent_id` 改成 "skill 是否在 coworker_skills 表内 enabled for this agent"——抽 helper `is_skill_enabled_for_coworker(skill_id, coworker_id, tenant_id)`
   - 新加 `list_skills_for_tenant(tenant_id, *, with_files: bool = False)` 给 v1 flat endpoint 用（PR 3）

4. **测试 fixture 更新**：
   - 凡有 `create_skill(coworker_id=...)` 的 factory 改成 `create_skill_for_coworker(...)` (convenience) 或 `create_skill(...)` + `enable_skill_for_coworker(...)`
   - 凡有 `skill.coworker_id` 断言改成 `list coworker_skills where skill_id=...`

5. **grep 验证清空**：
   ```bash
   grep -rn "skills.*coworker_id\|skill\.coworker_id\|Skill.coworker_id" src/ tests/ 2>/dev/null | grep -v __pycache__
   ```
   输出**必须**为空（schema.py 内 `# v1.1 03b greenfield: drop the legacy skills.coworker_id` 注释例外）

**测试**：

- `tests/db/test_skill_pertenant.py`：
  - 同 tenant 不同 coworker 共享同一 skill row（之前不可能，per-tenant 后可能）
  - `list_skills_for_coworker` 返 enabled skills，禁用的不返
  - DELETE coworker → coworker_skills 行级联删 + skills 行**保留**（per-tenant catalog）
- 现有 admin skills 端点测试不退化（admin 路径走 helper convenience 函数）

### PR 2 — `created_by` → `created_by_user_id` 三层对齐

**Background**：00b 把 DB 列改了，三层 (Python / REST schema / OpenAPI yaml / TS types) 仍用旧名 `created_by`——00b Findings 已 flag。本 session 收敛。

**改动范围**：

- `src/rolemesh/core/types.py`：`Skill.created_by` → `Skill.created_by_user_id`
- `src/rolemesh/db/skill.py` `_record_to_skill` mapper：`created_by=` → `created_by_user_id=`（PR 1 改 Skill dataclass 时一起改也行，但单独 PR 更易 review）
- `src/webui/schemas.py` (admin) + `src/webui/schemas_v1.py` (v1) skills 相关 response model：字段同步改
- `src/webui/admin.py`：response_model 引用 + 任何 alias 处理
- `web/openapi.yaml`：skills schema 字段名同步（codegen 自动出 TS）
- 前端任何手写 `created_by` 在 skills 相关代码 —— `grep -rn "created_by[^_]" web/src/` 看具体范围

**注意排除项**：

- `eval_runs.created_by` 是另一张表的另一个字段——**绝不一起改**（00b Findings 已 flag，admin UI + CLI 在用）
- `eval_runs.created_by` 出现的位置：`src/rolemesh/db/eval_runs.py`、可能的 admin endpoints；本 PR grep 时主动排除

**测试**：

- 既有 skills 单测 + admin skills endpoint 测试不退化
- 加 lint 测试：`assert "Skill.created_by\"" not in source` 之类（防止后续回退漂移）—— 实现可以用 grep 在 src/ 范围
- v1 endpoint Pydantic schema test 验 `created_by_user_id` 字段存在

### PR 3 — `/api/v1/skills/*` + 文件 endpoints + INV-5 lint

**Endpoints**：

- `GET /api/v1/skills` —— flat 列表（tenant 内所有，含 enabled 状态）
- `POST /api/v1/skills` —— 创建 skill（含 initial files；`SKILL.md` 必填，否则 422）
- `GET /api/v1/skills/{id}` —— 含 files
- `PATCH /api/v1/skills/{id}` —— 改 metadata（不含 files；文件用 file endpoints）
- `DELETE /api/v1/skills/{id}` —— 如果被任何 coworker_skills enabled 返 409 `RESOURCE_IN_USE`
- `GET /api/v1/skills/{id}/files` —— 列文件名
- `GET /api/v1/skills/{id}/files/{path:path}` —— 单文件内容
- `PUT /api/v1/skills/{id}/files/{path:path}` —— 写单文件（包括新增）
- `DELETE /api/v1/skills/{id}/files/{path:path}` —— **`{path}=SKILL.md` 必须返 409 `SKILL_MANIFEST_PROTECTED`**
- `GET /api/v1/coworkers/{id}/skills` —— 该 coworker enabled 的 skill 列表
- `POST /api/v1/coworkers/{id}/skills/{skill_id}` —— enable
- `DELETE /api/v1/coworkers/{id}/skills/{skill_id}` —— disable（删 coworker_skills 行）

**INV-5 三处一致 → 缩为 Python ↔ TS 两处**：

- Python: `src/rolemesh/core/skills.py:85 SKILL_MANIFEST_NAME = "SKILL.md"` 已就位（00a）
- TS: 新加 `web/src/api/skill_constants.ts`：
  ```ts
  export const SKILL_MANIFEST_NAME = "SKILL.md";
  ```
- Lint test (`tests/test_skill_manifest_constant_consistency_ts.py` 或扩 00a 已有的):
  - Python 读 `SKILL_MANIFEST_NAME` 常量值
  - TS 读 `web/src/api/skill_constants.ts` 文本，正则提取 `SKILL_MANIFEST_NAME = "..."` 字面量
  - 断言两侧字符串相等
- **不加 DB CHECK 'SKILL.md' 字面量**——理由：`db/skill.py:377-378` 已有 app-layer 守护，DB CHECK 多一层硬绑定（未来想 rename manifest 三处都改才放行），over-engineering 没 net 价值。Open Question 2 锁定为"DB CHECK 不做"

**Real-time hot-reload**：

- 写入路径同时发 `web.coworker.skills_changed` event（设计 §7）
- orchestrator 端订阅（**本 session 加 subscriber**——类似 02b 加的 `subscribe_coworker_mcp_changed`）：
  - subject pattern: `web.coworker.skills_changed.{coworker_id}` 单发布
  - 处理：调 `reload_coworker_skills_into_state(coworker_id)` 重新拉 `list_skills_for_coworker`
- 与 02b 的 `subscribe_coworker_mcp_changed` 同 pattern，复用 chore A 落的 NATS subscriber pattern

**Pinned tests**：

- `tests/webui/test_v1_skills.py`：
  - CRUD 各 endpoint + RLS 隔离
  - DELETE SKILL.md → 409 `SKILL_MANIFEST_PROTECTED`
  - DELETE skill 被 coworker enable → 409 `RESOURCE_IN_USE`
  - 文件路径校验：`PUT /files/../escape` → 422（用 `SKILL_FILE_PATH_RE` 拦）
  - hot-reload event publish 真发出
- `tests/orchestration/test_coworker_skills_reload.py`：subscriber 收到 NATS 事件后 state 更新
- INV-5 lint test 验 Python ↔ TS 一致

### PR 4 — Frontend skills 列表 + 文件树编辑器 + coworker 详情子面板

**`#/skills` 列表页** (`<rm-skills-page>`):

- 列表：tenant 全部 skills，每行 name / description / 创建者 / 关联 coworker 数
- "+ New skill" button → 跳 `#/skills/new`（新建向导）
- 行 click → 跳 `#/skills/:id` 详情编辑

**`#/skills/:id` 文件树编辑器** (`<rm-skill-detail-page>`):

- 左：文件树（含 SKILL.md，标灰禁用 delete）
- 右：编辑器（monaco / codemirror，按现有前端 build 选——grep 看是否已 import）
- 顶部：metadata 编辑（name / description / enabled toggle）
- 保存：PUT files / PATCH metadata
- DELETE skill button（顶部右）→ 调 DELETE endpoint；若 409 显示"in use by N coworkers"

**`#/coworkers/:id/skills` 子面板** (`<rm-coworker-skills-tab>`):

- 在 coworker 详情页 tabs 内（设计 §6.3 C "Overview / Skills / MCP / Bindings / Schedules / Conversations"）
- 列：tenant 全部 skills + 每行 enable checkbox
- check / uncheck → POST/DELETE `/api/v1/coworkers/{id}/skills/{skill_id}`

**Pinned tests (vitest)**:

- 文件树渲染含 SKILL.md，delete 按钮禁用
- 路径 invalid (`../foo`) → form 拒
- enable / disable POST/DELETE 调对

## Acceptance criteria

- [ ] PR 1: grep `skills.*coworker_id\|skill\.coworker_id` 输出为空（除 schema.py 注释）
- [ ] PR 1: 同 tenant 内多 coworker 共享同一 skill row 验证通过
- [ ] PR 1: DELETE coworker → coworker_skills 行级联删，skills 行保留
- [ ] PR 2: `Skill.created_by_user_id` 全三层一致；`eval_runs.created_by` 不动
- [ ] PR 2: lint test 防回退
- [ ] PR 3: `/api/v1/skills/*` 全 CRUD + RLS 隔离
- [ ] PR 3: SKILL.md 受保护（409 `SKILL_MANIFEST_PROTECTED`）
- [ ] PR 3: DELETE skill 被 enabled → 409 `RESOURCE_IN_USE`
- [ ] PR 3: INV-5 lint Python ↔ TS 一致
- [ ] PR 3: hot-reload `web.coworker.skills_changed` event 端到端工作
- [ ] PR 4: 前端三页面（list / detail / coworker subtab）能用
- [ ] **Phase 3 完整 smoke**（设计 §10）：tenant admin 创建 skill → enable 给 coworker → coworker 容器启动 → skill 投影到 tmpfs（grep `cat container/skills/SKILL.md`）→ chat 用 skill 内容
- [ ] OpenAPI yaml + codegen + contract test 同步
- [ ] 现有 admin /skills endpoints 不退化（保留兼容期）
- [ ] 更新 plan 状态

## Out of scope

- ❌ Inline skill UI bridge in chat panel（skills 是静态配置，不需要 inline）
- ❌ DB CHECK `SKILL.md` 字面量（over-engineering，app layer 已守）
- ❌ Drop admin `/api/admin/agents/{agent_id}/skills/*` endpoints（6 个月兼容期）
- ❌ Skill version history / rollback（v2）
- ❌ Skill marketplace / cross-tenant 共享（v2）
- ❌ Drop `coworkers.tools` 列（02b 已做）
- ❌ Touch `eval_runs.created_by`（独立 field，不同 entity）

## Open questions

锁定：

1. ~~PR 1 stage 1 双写 vs 一刀切~~ → **一刀切**（greenfield，与 02b 同模式）
2. ~~DB CHECK constraint for SKILL_MANIFEST_NAME~~ → **不加**（over-engineering；app layer + INV-5 lint 已守 + DB 硬绑死未来 rename 空间）
3. ~~backfill script 同名冲突~~ → **moot**（greenfield 无数据要保留）
4. ~~drop coworker_id 何时做~~ → **同 commit drop**（吸收原 03+ session 工作）

仍需 session 内决策：

1. **monaco vs codemirror vs textarea**：前端编辑器选型——grep `web/src/` 看现有依赖，没有则用 textarea 起步（避免引入 ~1MB 编辑器依赖；后续 polish chore 升级）
2. **`<rm-coworker-skills-tab>` 是否做 search/filter**：tenant 大时 skill 列表可能长——先不做，超过 50 个 skill 再 polish
3. **convenience helper `create_skill_for_coworker` 是否放在 db/skill.py 公共 API**：vs 只 admin 内部用。推荐放在 public（admin 与 v1 内部都可能复用），与 02b `replace_coworker_mcp_configs` 同位置策略

## Pitfalls

- **pi/ 下的 `skill` 引用**：pi 有自己的 skill 加载机制（`src/pi/coding_agent/core/skills.py`），与 RoleMesh 的 skills 表是独立两套；grep 时主动排除 `src/pi/`（与 02b 排除 pi `context.tools` 同理）
- **写入必须事务包裹**：skills + coworker_skills 同事务，半写让 orchestrator 拿到不全的 skill 配置
- **`enabled` 双层 AND**：`skills.enabled` (全局开关) AND `coworker_skills.enabled` (per-coworker 开关)，reader 切换时两个 flag 都要 AND；漏一个会让"全局禁用但被 coworker enable"的 skill 误启用
- **测试 fixture 连锁影响**：很多 conftest 隐式调 `create_skill(coworker_id=...)`，schema 改后 fixture 一起爆。session 开始时先列受影响 fixture 清单
- **schema migration 顺序**：必须先在代码层确保所有写入路径不再写 `skills.coworker_id`，**再** drop 列。Greenfield 下其实没风险（DB 可清），但好习惯按此顺序
- **`SKILL_FILE_PATH_RE` 校验**（00a 落的常量）：所有文件路径（POST / PUT）都要走，防 `../` 等
- **`coworker_skills` 关系层 hot-reload**：写入路径同时发 `web.coworker.skills_changed` event；orchestrator 侧 subscriber 用 02b `subscribe_coworker_mcp_changed` 同模式新加
- **`eval_runs.created_by` 不要碰**：00b Findings 已 flag，独立 entity 独立 admin UI / CLI，本 session 绝不一起改
- **monaco 引入会爆 bundle size**：若 grep 发现项目内无现有编辑器依赖，textarea 起步即可——polish 留独立 session

## 执行前刷新清单

- [ ] 03a 完成？（plan.md 显示 done）
- [ ] grep `skills.*coworker_id` baseline（数字可能与原 prompt 写的不一致）
- [ ] 前端编辑器决策（monaco / codemirror / textarea）
- [ ] `eval_runs.created_by` 当前 reader 数量（grep 确认 PR 2 排除范围）
- [ ] `coworker_skills` junction 表当前是否真在用（00b 落地但可能没 writer——03b 是第一个真用）

## Findings (after execution)  `[2026-05-21]`

**4 commits on `feat/ui`** — 91c277d (PR 1) / 0b84aba (PR 2) / 542c439 (PR 3) / eef12db (PR 4).

### Baseline grep + reader cutover (PR 1)

Initial sweep returned **15 hits** outside `src/pi/` (per the spec's exclusion):

```
src/rolemesh/db/skill.py:100,180,187   ← INSERT + 2 list reads
src/rolemesh/db/schema.py:340,343,401  ← idx + comment + trigger msg
src/webui/admin.py:1765,1869,1890,1928 ← 4 auth checks
tests/test_schema_alters.py:190,196,219,225 ← 4 direct INSERT
tests/db/test_skills.py:294,357,385    ← 3 direct INSERT
```

Final grep (post-PR 1) was clean — every live access removed, only schema.py `DROP COLUMN` statements and a doc-string back-reference remain.

The legacy `skills_check_coworker_tenant` SECURITY DEFINER trigger got replaced by a parallel `coworker_skills_check_tenant` trigger on the junction (same `IS DISTINCT FROM` cross-tenant guard, just rebound onto the row that now carries the relationship). Without that the test-pool's bypass-RLS access could have forged cross-tenant bindings — caught by the new `test_coworker_skills_rejects_cross_tenant_binding` test.

### Admin auth helper (PR 1)

Original spec named only `is_skill_enabled_for_coworker` (strict both-AND). Using that for admin auth would have **locked admins out of re-enabling a disabled skill** — the existing `test_toggle_enabled_round_trip` admin test failed when I wired it through. Added a second helper `is_skill_bound_to_coworker` (ignores enabled flags) and routed all 4 admin endpoints through it. The strict helper still ships — used by the v1 layer where projection semantics matter.

This is the kind of thing the spec's "the helper auto-falls out of the read pattern" line under-specifies. Worth a line in the design doc for v2.

### `created_by` rename (PR 2)

Three-layer touch: Python dataclass + `_record_to_skill` mapper + Pydantic `SkillResponse`. OpenAPI yaml skill schemas didn't exist yet (PR 3 adds them), so the rename's TS-layer obligation lived in PR 3. eval_runs.created_by deliberately untouched — new `test_eval_runs_created_by_is_not_affected` is a negative-control to flag any future bulk-rename refactor.

### Editor decision (Open Q 1) — textarea

`web/package.json` shows only `lit / marked / tailwindcss / typescript / vite`. No monaco, no codemirror. Per prompt resolution, textarea ships now; polish chore can swap later without changing the v1 wire surface. Bundle size after PR 4: 207.34 kB raw / 49.55 kB gzipped — a monaco swap would have ~tripled that.

### `create_skill_for_coworker` location (Open Q 3) — public `db/skill.py`

Kept the convenience helper alongside the lower-level primitives, matching 02b's `replace_coworker_mcp_configs` placement. Admin REST and tests both reach for it; v1 endpoints don't (they're catalog-first, bind separately). One-call shape matters for fixtures more than for production code.

### Hot-reload subscriber details (PR 3)

- Subject: `web.coworker.skills_changed` — single-coworker scoped per spec, payload identical to `mcp_changed` (`{coworker_id, tenant_id}`).
- Stream: same `web-ipc` JetStream stream (max_age=3600s) already created for restart/mcp_changed.
- Durable name: `orch-web-coworker-skills-changed`.
- Reload helper: `reload_coworker_skills_into_state(coworker_id, tenant_id, state, fetch_skills)` returns `False` when the coworker isn't in cached state — matching the `mcp_changed` posture, not the `restart` posture. The catalog edit path on the webui side fan-outs one publish per **bound** coworker (`_broadcast_skills_changed`), so a previously-unknown coworker would never receive an event anyway.
- `CoworkerState` gained `skills: list[Skill]` populated from `list_skills_for_coworker(enabled_only=True)`. The spawn-path container projector still re-reads through `list_skills_for_coworker` directly; the cache is for future request-path code that wants the snapshot without a DB hit.

### INV-5 lint scope (PR 3)

Python ↔ TS only, as locked. New `web/src/api/skill_constants.ts` re-exports `SKILL_MANIFEST_NAME` and (bonus, for PR 4's form validator) the `SKILL_FILE_PATH_RE` regex + a `isValidSkillFilePath` helper. The lint test reads the TS source as text and regex-extracts the string literal — no TS execution required.

### Test count

- PR 1 fixture rewrite: 30 DB-layer tests (existing 17 + 13 net new on per-tenant catalog, double-AND enable, cross-tenant junction guard, helper bound-vs-enabled distinction).
- PR 2 anti-regression: 4 tests, including the negative-control on `eval_runs.created_by`.
- PR 3 v1 surface: 14 endpoint-level tests + 2 reload-helper unit tests + 4 contract-test required-set pins + 2 INV-5 consistency checks.
- PR 4 frontend: 6 vitest scenarios (list view rows, create POST, manifest delete disabled, traversal rejection, enable/disable toggle round-trip).

### Impact on Phase 4 / v1.1 closeout

03b was the last per-entity Phase 3 piece (approvals shipped in 03a). After this, **the only outstanding session is 04 (Safety UI migration)** — a UI-only migration since the safety engine + DB already live behind the legacy admin surface. Phase 4 (orchestration / WS lifecycle hardening) inherits the hot-reload pattern fully validated by 02b + 03b: subject naming, JetStream durable, publish fan-out, reload helper. Future sessions plugging new entities into the hot-reload mesh have a template.

One yellow flag: the v1 catalog DELETE returns 409 with `details.coworker_ids` but the wire UI doesn't yet bulk-unbind from the catalog page. Today the admin has to walk into each coworker detail's skills tab to unbind. Acceptable for v1.1; queue a UX polish chore.
