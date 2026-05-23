# Session 04 — Safety UI 迁 v1  `[REFRESHED 2026-05-21]`

| field | value |
|---|---|
| Phase | 4 |
| Prerequisites | 03b done（Phase 3 完工） |
| Estimated PRs | 2-3 |
| Estimated LOC | ~700（原估 900 偏高；admin writes 保留不在 v1，CSV 推迟）|
| Status | not started |

> **Refresh 起源**：v1.1 最后一个 session。把以前所有 session 学到的 pattern 收拢应用——typed ApiClient（01a / 02a / 03a）、`raise_error_response`（01a）、`<rm-app-shell>` shell pack（00c）、RLS 双层防御（INV-1, 00b）、INV-5 lint pattern（03b）。安全 engine + DB 已运行多 phase，本 session 是**纯 UI/API 表层搬迁**。
>
> **关键决策**：v1 surface **严格按设计 §3 Phase 4 GET-only**——admin 保留 POST/PATCH/DELETE 写入路径（safety 规则修改是 admin 特权操作）。这意味着 01c Findings 提到的"04 完成后清 lint:no-admin-chat allowlist"是过度乐观——allowlist 仍会保留 safety 3 个文件（frontend 写入仍走 admin）。**Refresh 老实记录这一点**，避免 04 session 误以为要扩 scope。

## Goal

1. 把现有 admin GET-类 safety endpoint（`/api/admin/safety/rules` GET 列表/详情/audit；`/api/admin/safety/checks`；`/api/admin/tenants/{tid}/safety/decisions` 列表/详情）搬到 `/api/v1/safety/*` 命名空间
2. 前端 `safety-rules-page` + `safety-decisions-page` 包进 `<rm-app-shell>`，**read 路径**切到 v1 typed client；**write 路径**保留 `safety-admin-client` 调 admin
3. 路径 `#/admin/safety/rules` / `#/admin/safety/decisions` 保留（已存在），只换 shell 与底层 client
4. v1.1 收尾：所有 13 个 session 全 done（含 retired 的 02c + 03+）

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §3 Phase 4（GET-only 严格遵守）/ §6.1 路由 / §11 INV-1
2. [`docs/13-safety-overview.md`](../13-safety-overview.md) + [`docs/14-container-hardening-architecture.md`](../14-container-hardening-architecture.md) + [`docs/15-safety-framework-architecture.md`](../15-safety-framework-architecture.md) —— safety 总览
3. [`docs/18-rls-architecture.md`](../18-rls-architecture.md) —— safety 表 RLS 现状（pitfall 提到要验）
4. **01a Findings § "ErrorResponse helper"** —— `raise_error_response` 用法
5. **02a Findings § "Frontend 状态"** —— typed ApiClient 现有方法签名
6. **03a Findings § "Admin endpoints 搬迁完整度"** —— admin/v1 双发布模式的成熟做法（共享 engine 单例、6 个月兼容期、字段差异表）
7. **03b Findings § "INV-5 lint scope"** —— Python ↔ TS 一致 lint pattern
8. **01c Findings § "admin → v1 切换的实际范围"** —— lint:no-admin-chat allowlist 是 04 关注重点
9. 现有 admin safety endpoints (`src/webui/admin.py` 第 1196-1620 行附近)：
   - `POST/GET/PATCH/DELETE /safety/rules` —— writes 不迁
   - `GET /safety/checks` —— 迁
   - `GET /tenants/{tid}/safety/decisions` 列表 / 详情 / CSV —— 迁前两个，CSV 推迟
   - `GET /tenants/{tid}/safety/rules/{rule_id}/audit` —— 迁
10. 现有 safety 前端 (`web/src/components/safety-rules-page.ts` / `safety-decisions-page.ts` / `web/src/services/safety-admin-client.ts`) —— **lint:no-admin-chat allowlist 三个文件**

## 概念定位：v1 read 路径完整化；admin 仍管 writes

Safety rule 修改（创建 / 编辑 / 启停 / 删除）是 admin 特权操作——只有 tenant admin / 系统管理员才该改。v1 surface 面向广义"用户访问"——能看 rules / 看 decisions / 看 audit 但不能改。

这与 approvals 不同（v1 完整 CRUD，每个 user 都可能 decide）；safety rules 的 read-only v1 是有意设计。

实际影响：

- **frontend 仍 import 两个 client**：v1 typed ApiClient（reads）+ safety-admin-client（writes）
- **lint:no-admin-chat allowlist** 保留 safety 三个文件
- **admin /safety/rules 写入端点**不动（保留 6 个月兼容期之外的语义性保留）
- 未来如果"safety writes 也迁 v1" 真有需求（如 super admin 跨多 tenant），那时单独 session

## Scope — PR breakdown

### PR 1 — `/api/v1/safety/*` GET endpoints

按设计 §3 Phase 4 完整列表（GET only）：

| v1 endpoint | 当前 admin endpoint | 备注 |
|---|---|---|
| `GET /api/v1/safety/rules` | `GET /api/admin/safety/rules` | 列表，支持 `?coworker_id` / `?stage` / `?check_id` / `?enabled` filter（与 admin 一致）|
| `GET /api/v1/safety/rules/{id}` | `GET /api/admin/safety/rules/{rule_id}` | 详情 |
| `GET /api/v1/safety/rules/{id}/audit` | `GET /api/admin/tenants/{tid}/safety/rules/{rule_id}/audit` | tenant_id 从 auth 推断不在 URL |
| `GET /api/v1/safety/checks` | `GET /api/admin/safety/checks` | 列可用 check_id |
| `GET /api/v1/safety/decisions` | `GET /api/admin/tenants/{tid}/safety/decisions` | tenant_id 同上；保留 `?limit` / `?offset` 分页 |
| `GET /api/v1/safety/decisions/{id}` | `GET /api/admin/tenants/{tid}/safety/decisions/{decision_id}` | 同 |

**实现策略**：

- 共享 helper：把 admin endpoint 内的核心查询逻辑提取到 `src/rolemesh/db/safety_service.py`（或直接复用现有 `src/rolemesh/db/safety.py`，看是否已有完整 CRUD helper），admin + v1 都调
- 不要双实现——v1 endpoint 内只做：auth + 调 helper + Pydantic response_model + raise_error_response
- 所有 4xx 走 `raise_error_response`（01a 落地的）
- `response_model` 走 `webui.schemas_v1.SafetyRuleResponse` / `SafetyDecisionResponse` / `SafetyAuditEntryResponse` 等新 Pydantic 类型

**RLS / INV-1 双层防御**：

- safety_rules / safety_decisions / safety_rules_audit 三表都要验
- 现有 admin endpoint 可能用 `admin_conn`（绕 RLS），v1 endpoint **必须**用 `tenant_conn(user.tenant_id)` + 显式 `WHERE tenant_id = $1`
- pinned test：tenant A 看不到 tenant B 的 rules / decisions / audit
- 如果 grep 发现现有 admin 路径有 RLS bypass（绕 tenant_id 过滤），不要在本 session 修——记 Findings 留独立 chore

**OpenAPI 同步**（与 01a-03a 一致）：

- yaml 先改 → npm run openapi:gen → commit types.ts → 实现 handler
- 每个新 endpoint 加 `response_model`
- `tests/test_openapi_contract.py` 加对应 `required` 集合相等测试

**Pinned tests** (`tests/webui/test_v1_safety.py`)：

- 6 endpoint 各 happy path
- RLS 跨租户隔离（每个 table 至少一个）
- 不存在的 rule_id / decision_id → 404 + 结构化 error
- decisions 分页：`?limit=10&offset=20` 正确
- audit endpoint 返回 timeline 顺序（created_at DESC）

### PR 2 — Frontend safety 页接入 shell + read 路径切 v1

**改动**：

- `<rm-safety-rules-page>` + `<rm-safety-decisions-page>` 包进 `<rm-app-shell>`（与其它 v1 页面统一布局）
- 路由保留 `#/admin/safety/rules` 与 `#/admin/safety/decisions`（与设计 §6.1 一致；hash 含 `admin` 是历史选择，不动）
- **Read 路径**：组件内所有 GET 调用改走 typed `ApiClient`（02a 落地）
- **Write 路径**：组件内 POST/PATCH/DELETE 保留 `safety-admin-client` 调 admin（不动）
- 现有交互不退化——rules 列表能筛选 / 编辑 / 启停（writes 仍 admin），decisions 列表能分页 / 查详情
- 验收：手动跑过现有 safety 页面流程

**lint allowlist 处理**：

- `web/scripts/lint-no-admin-chat.mjs` 内的 ALLOWLIST 保留三个文件（与"概念定位"段一致）
- 注释更新："Safety writes intentionally stay on admin surface (v1 is GET-only per design §3 Phase 4)"——把"Phase 4 migration"措辞改成显式说明保留理由
- **不删 allowlist 行**——01c Findings 误以为 04 会清空，refresh 修正

**Pinned tests (vitest)**：

- shell 布局：safety 页面在 sidebar 高亮正确
- read 走 v1 client：发请求时 URL 是 `/api/v1/safety/...`
- write 走 admin client：rule create/update 时 URL 是 `/api/admin/safety/...`
- 现有的 safety-rules-page / safety-decisions-page 测试不退化

### PR 3 (可选) — admin safety GET 端点 deprecation 标记

如果 PR 1/2 后 admin 的 GET 端点确实零调用（grep `web/src/` 确认），加 `Sunset` header 标 deprecated。如果还有调用（比如某些后台脚本或 CSV 导出走的还是 admin）就跳过。

- `Sunset: <date>` header（RFC 8594）—— 设计 §0 文档约定 admin 保留 6 个月兼容期。从 v1.1 发布日算 6 个月（具体日期 session 内问用户或用 today + 180d 占位）
- `Deprecation: true` header
- `Link: </api/v1/safety/rules>; rel="successor-version"` header

**判断**：如果 admin GET 端点仍有非前端调用方（如 ops 脚本 / monitoring），不加 Sunset 避免误导。session 内调查后决定。

## Acceptance criteria

- [ ] `/api/v1/safety/*` 6 个 GET 端点全工作
- [ ] RLS 隔离 + INV-1 双层防御 pinned test 绿
- [ ] OpenAPI yaml + codegen + contract test 同步
- [ ] 现有 safety frontend 不退化（rules / decisions 页面交互完整）
- [ ] **Phase 4 smoke**（设计 §10）：safety rule 触发 block → decision 落表 → UI 显示
- [ ] lint:no-admin-chat allowlist 保留 safety 三个文件 + 注释更新
- [ ] 现有 admin /safety/* 端点不退化（writes 仍工作；admin 测试全绿）
- [ ] 更新 plan 状态 —— **v1.1 全部 13 session 完成（含 2 retired）**

## Out of scope

- ❌ Safety rule writes 迁 v1（admin 保留，per design §3 Phase 4 GET-only）
- ❌ Safety 规则 DSL 改动 / engine 改动
- ❌ CSV export 端点迁 v1（admin 保留，使用面小）
- ❌ 修复现有 admin 路径上潜在的 RLS bypass（如发现，记 Findings 独立 chore）
- ❌ 清 lint:no-admin-chat allowlist（safety writes 留在 admin，allowlist 必须保留）
- ❌ Real-time WS event for safety decisions（用 polling 即可；safety decisions 不是高频时序事件）
- ❌ pre-existing TS errors in unrelated files（credentials-page / mcp-servers-page 03a Findings 提到的，独立 chore）

## Open questions

锁定：

1. ~~v1 是否含 writes~~ → **GET-only 严格按设计 §3 Phase 4**；admin 保留 writes
2. ~~CSV export~~ → 推迟（admin 保留）
3. ~~lint allowlist 处理~~ → **保留 safety 三行 + 注释更新**（01c Findings 误判修正）

仍需 session 内决策：

1. **Sunset header 截止日期**：admin GET 端点是否加 Sunset？如果 grep 发现还有非 frontend 调用（ops 脚本等），不加；否则用 today + 180d
2. **`/api/v1/safety/rules/{id}/audit` URL**：admin 现在的路径是 `/api/admin/tenants/{tid}/safety/rules/{rule_id}/audit`（tenant_id 在 URL）；v1 应该是 `/api/v1/safety/rules/{id}/audit`（tenant_id 从 auth 推断）—— **已锁后者**，与其它 v1 endpoint 一致
3. **现有 admin endpoint 是否有 admin_conn bypass RLS**：若有，本 session **只记不修**（独立 chore）；若没有就直接复用 helper

## Pitfalls

- **不要在搬迁时顺手"重构 safety 业务"** —— 04 是搬迁 + shell 整合，不是改 safety engine / 不是改 rule DSL / 不是优化 query
- **RLS 现状先确认** —— 现有 admin endpoint 可能用 `admin_conn`（admin 路径合理），v1 endpoint 必须用 `tenant_conn` + INV-1 双层防御；不验直接搬可能漏
- **decisions 表数据量可能大** —— 分页参数必须保留，默认 limit 不要过大（admin 现在用多少？保持一致）
- **safety_rules_audit.actor_user_id** 是 INV-4 audit FK 监管的——本 session 不写入这表（admin writes 才写），但 GET audit endpoint 返回 actor_user_id 是真 UUID，前端如果要显示 user 名要 join users 表
- **v1 endpoint 内不要双实现** —— admin handler 内的查询逻辑提取到 helper，admin + v1 共享一份；否则未来 schema 变了两处都要改
- **Frontend write 路径绝不切到 v1** —— write 没 v1 endpoint，调过去会 404；safety-admin-client 必须保留所有 write 方法
- **lint:no-admin-chat allowlist 行不能删** —— 删了 safety frontend 的写入路径会被 lint 红，本 session 测试就过不了
- **`#/admin/safety/*` 路由保留** —— hash 含 `admin` 是历史决定（设计 §6.1 也是这么列的），不改；只换内部组件壳与 client

## 执行前刷新清单

- [ ] 03b 完成？（plan.md 显示 done，Phase 3 全完工）
- [ ] 现有 admin safety endpoints 数量 + URL 模式 grep 确认（应为 9 个：4 rules CRUD + 1 checks + 3 decisions（list/detail/csv）+ 1 audit）
- [ ] `src/rolemesh/db/safety.py` 是否已有完整 read helpers 可复用，或需要提取到 safety_service.py
- [ ] safety 表 RLS 现状：现有 admin 路径是否绕 RLS（admin_conn 用法 grep）
- [ ] safety-admin-client 实际 method 数量（grep 看 admin 端点调用清单，决定哪些保留 write、哪些只 GET）

## Findings (after execution — v1.1 收尾 session)

### Concrete outcomes

**1. 6 v1 endpoints — LOC & tests**

| Endpoint | Handler LOC (rough) | Pinned integration tests |
|---|---|---|
| `GET /api/v1/safety/rules` | ~20 | list / ordering / coworker filter / stage+enabled filter / cross-tenant isolation (5) |
| `GET /api/v1/safety/rules/{id}` | ~13 | happy path / 404 unknown / 404 malformed / 404 cross-tenant (4) |
| `GET /api/v1/safety/rules/{id}/audit` | ~22 | timeline order / actor attribution / 404 unknown / 404 cross-tenant / delete drops history (5) |
| `GET /api/v1/safety/checks` | ~25 | wire contract + stable alpha ordering (1) |
| `GET /api/v1/safety/decisions` | ~35 | envelope+total / RLS / pagination / limit cap=422 (4) |
| `GET /api/v1/safety/decisions/{id}` | ~22 | happy path / 404 cross-tenant / 404 malformed / findings JSON round-trip (4) |

Total handler module (`src/webui/v1/safety.py`): 285 LOC including projections.
Wire schema additions (`schemas_v1.py`): 7 new Pydantic models + 5 Literal aliases.
Backend integration: 21 tests. Contract: 7 (5 required-set + 1 endpoint coverage + 1 enum anchor). Frontend vitest: 7 (read/write routing).

**2. Helper extraction decision**

No `safety_service.py` introduced. `src/rolemesh/db/safety.py` was *already*
the complete read helper layer — `list_safety_rules` / `get_safety_rule` /
`list_safety_rules_audit` / `list_safety_decisions` / `count_safety_decisions`
/ `get_safety_decision`. Both admin and v1 endpoints now call the same
six functions; the v1 module is purely projection + boundary glue (auth +
`raise_error_response` + Pydantic). The 04 prompt left the choice open
("safety_service.py 或 db/safety.py"); reading the code first showed the
helper layer already existed, so no extraction work was needed.

**3. RLS bypass investigation**

No bypass found on safety read paths. Every admin GET handler already
called the `db.safety` helpers, which uniformly use `tenant_conn(tenant_id)`
plus an explicit `WHERE tenant_id = $1::uuid` clause (INV-1 double defense
already in place). The only `admin_conn()` use in `db/safety.py` is
`cleanup_old_safety_approval_contexts` — a documented retention sweep that
is explicitly cross-tenant and carries an `# inv-1-ok` comment. No
follow-up chore needed.

**4. Sunset header decision**

PR 3 landed with hardcoded date `Tue, 17 Nov 2026 00:00:00 GMT`
(session date 2026-05-21 + 180 days). Eligible because after PR 2 the
admin safety GETs have **zero non-test runtime callers** across
`web/src/` and ops scripts (verified by grep). All 6 GETs now emit
`Sunset` + `Deprecation: true` + `Link: </api/v1/safety/...>; rel="successor-version"`.
Writes (POST/PATCH/DELETE) and the CSV export intentionally do NOT carry
the headers — they have no v1 successor.

**5. Lint allowlist note**

Three safety files stay on the allowlist permanently. Comment updated
from "Phase 4 migration" (which implied temporary staging) to an
explicit "writes + CSV stay on admin per design §3 Phase 4 — locked"
record. The 01c hope of clearing the allowlist after 04 was over-optimistic
and is now corrected at the comment level.

### v1.1 retrospective — 13 sessions across 2 days

**Sized correctly (estimate ≈ actual):**
- 00a INV foundations — 1 PR-ish, 7 punch list items, no scope creep.
- 00b Migrations + RLS — 2 commits, greenfield rename absorbed cleanly.
- 01a Coworkers + runs — 4 commits, 39 tests, came in around prediction.
- 01b WS protocol — 4 commits, 70 tests, biggest session by scope but the wire+state machine wiring is genuinely indivisible.
- 03a Approvals → v1 — 3 commits, 158 tests; the engine-singleton sharing pattern paid off.
- 03b Skills per-tenant — 4 commits including a dedicated catalog cutover PR; right call to absorb 03+ here.
- **04 (this session)** — 3 commits, ~700 LOC, well under the 900 LOC original estimate (CSV + writes stayed admin, helper already shared).

**Mis-sized (refresh was load-bearing):**
- 02a Models / Credentials / MCP — originally underestimated the CredentialVault primitive surface; refresh added BYTEA schema + INV-VAULT-{1,2,3} which became the long pole.
- 02b tools column — original plan called for dual-write+reader switch then a separate drop column session (03+); refresh greenfield-collapsed into one commit. Final form was much smaller than the dual-write design implied.
- 03a — the original prompt undersold the WS event bridge work; refresh "with Phase 1/2 context" was necessary to land the inline approval surface alongside the queue page.

**Refreshes that were genuinely necessary (vs nice-to-have):**

Necessary:
- 02b refresh — collapsed dual-write into one greenfield commit; without this the migration would have been three commits over two sessions for a column with zero readers in production.
- 02c retirement — the credential_proxy user-mode work had zero callers; killing the session entirely (rather than refactoring) was the right call.
- 03a Phase 1/2 context refresh — the WS event surface needed to be aware of the run-state machine from 01b.
- 03b greenfield cutover refresh — absorbed the 03+ "drop tools column" work after 02b proved greenfield was safe.
- **04 refresh** (this session's input) — corrected the 01c "clear allowlist after 04" assumption that turned out to be over-optimistic; locked GET-only.

Nice-to-have but didn't change scope:
- 03+ retirement document — could have been a comment in 03b's commit; the retirement note added little.

**Anti-over-engineering decisions that proved right (third+ times):**
- **Token rotation deferred** (00a) — never needed in v1.1; would have added schema + RPC surface for nothing.
- **`exchange_for` stub cut** (02c refresh) — zero callers; the abstraction was speculative.
- **02c retired entirely** — see above; the cleanest path was deletion.
- **03+ absorbed into 03b** — greenfield cutover proved safe; the staged drop was speculative.
- **02b three-stage retirement folded into one commit** — same pattern: when there is no concurrent reader to migrate, the staging adds no safety.
- **04 GET-only on v1** (this session) — keeping safety writes on admin means one less surface to extend, no v1 write tests to maintain, and a clear rule: "if humans configure it, admin owns it". Without the lock, scope would have crept into 8-9 endpoints with body validation. Saved ~400 LOC.

**Reusable lessons for future "v2 design + execution" projects:**

1. **Read the design doc before estimating, but read the *code* before the session.** The 04 estimate was 900 LOC; actual was ~700 because reading `db/safety.py` first showed the helper extraction work didn't exist. Three sessions (02b, 03b, 04) all came in smaller than estimate because greenfield realities were better than the design's worst-case assumptions.

2. **Greenfield assumption beats compat-window staging when callers are countable.** 02b / 03+ / 04 all proved that "list every caller, switch atomically, delete the old name" beats "dual-write + reader cutover + drop". The compat-window pattern is for code that ships to external users, not internal services on one branch.

3. **A locked decision matters more than a clever decision.** The 04 prompt's three "locked" items (GET-only, allowlist retained, audit URL shape) all could have been re-litigated; instead they were quoted into the commit message and held the scope. Earlier sessions where decisions stayed soft (e.g., 02a credential surface) drifted twice.

4. **Test the wire, not the helper.** Every new session that wrote `test_v1_*` exercised the JSON over httpx ASGI against a real Postgres testcontainer. The few times we mocked (e.g. NATS publisher in 03a) we paid for it later via a follow-up that pinned the real protocol shape.

5. **Refreshes are cheap; rewrites are not.** Refreshing a session prompt cost ~10 minutes and prevented multiple scope spirals. The pattern: between sessions, read the upstream session's Findings, grep what they touched, refresh the next prompt's "Open questions" with locked answers. Refreshes that mattered: 02c (retire), 03a (Phase 1/2 context), 04 (GET-only + allowlist permanence).

6. **One session = one Plan row = one commit train, multi-PR.** Mixing two phases in one Claude Code session hid coupling bugs (which is why design §"必须跨 session 的拆分" lists the seams we won't cross). 04 explicitly split into three commits (PR 1 backend / PR 2 frontend / PR 3 deprecation) so each is reviewable independently while staying on one branch.

**v1.1 final state:**
- 13 of 13 sessions complete (11 shipped + 2 retired as scope-cut).
- All on `feat/ui`, 19 commits, ~11k LOC across backend + frontend.
- Ready for `feat/ui → main` once a single human review pass lands.
