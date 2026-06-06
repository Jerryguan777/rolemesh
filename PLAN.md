# 任务(修订版):迁移 admin.py 独占端点到 /api/v1 + 整删旧面 + 补全 OpenAPI 契约

> **修订说明**:本版在原方案基础上,按对照代码核实的结果修正了 6 处事实/范围问题。
> 修正点以 **【修订N】** 标注,集中清单见 §10。核心方向(design-first 契约、等价迁移、
> 声明式角色门、404-不-403、平台超集派生)不变。

## 1. 背景(新 session 必读,无前置上下文)

RoleMesh 是多租户 AI agent 平台(self-hosted + 可托管云 AaaS)。后端 FastAPI,前后端同仓
(`src/` 后端、`web/` 前端)。**目前存在两套并行的 REST 面:**

- **新面 `/api/v1/*`**:`src/webui/v1/*.py` 各 router,聚合在 `src/webui/api_v1.py`
  (`APIRouter(prefix="/api/v1")`),`main.py` 挂载。**这是规范主面**:统一错误信封、角色门齐全、
  **被 OpenAPI 契约覆盖**(`contracts/openapi.yaml`,前端从中 codegen `web/src/api/generated/types.ts`)。
- **旧面 `/api/admin/*`**:单文件 `src/webui/admin.py`(`APIRouter(prefix="/api/admin")`,**1678 行 / 37 条
  路由**)。已按角色 gate(无安全漏洞),但:**错误格式是 FastAPI 默认 `{"detail":...}`、未被 OpenAPI
  契约收录(契约里 `/api/admin` 出现 0 次)、且独占了一批 v1 还没有的功能**。

**为什么要做(已评审):**
一个第三方 UI 若只靠 OpenAPI 契约 codegen 接入,**拿不到旧面的功能**(契约没收录),而用户/租户/
safety 写入这些关键管理能力**只在旧面**。要让"契约 = 完整公开面",必须把旧面独占端点迁到 v1、
统一到 v1 的约定、补进契约,然后删掉旧面。

**v1 当前缺口(admin.py 独占,v1 没有):**
1. **用户管理** `/users`、`/users/{id}`(list/create/get/update/delete,admin.py:341-408)。
2. **租户设置** `/tenant`(GET/PATCH,admin.py:313-333)。
3. **safety 写入与审计**:`/safety/rules`(POST,876)、`/safety/rules/{id}`(PATCH 953/DELETE 999)、
   `/safety/checks`(1048)、`/tenants/{tid}/safety/decisions*`(1101/1149)、
   `/tenants/{tid}/safety/rules/{id}/audit`(1173)、
   **`/tenants/{tid}/safety/decisions.csv`(流式导出,1293)**(v1/safety 目前**只读**,且没有 CSV)。
4. **定时任务管理**:`/tasks`(661)、`/tasks/{id}`(DELETE 677)、`/agents/{id}/tasks`(667)
   (v1/schedules 目前**只读**)。

**纯重复(已在 v1,可直接随文件删):**
- admin.py 的 `/agents*`(416-533)、`/agents/{id}/bindings*`(541-604)、
  `/agents/{id}/conversations*`(612-653) —— 对应 v1 `/coworkers*`、`/coworkers/{id}/bindings`、conversations。
- **【修订2】** admin.py 的 `/agents/{id}/skills*` 与 **skill 文件操作**(1403-1678,约 275 行) ——
  **原方案漏列**。已核实 v1 `skills_router` 完全覆盖(含 `PUT/DELETE /skills/{id}/files/{path:path}`,
  skills.py:628/673),故删除安全。**但前端 `/api/admin/agents` 仍有消费者,见下。**

**前端现状(核实修正):**
全仓库前端只有 `web/src/services/safety-admin-client.ts` 调 `/api/admin/*`,共 **11 个不同端点**:
- safety 规则 CRUD(checks/list/create/update/delete)、`/api/admin/tenant`、
  decisions(list/get)、rule audit、**`decisions.csv` 导出**;
- **【修订1】** 外加 `listCoworkers()` 调 **`/api/admin/agents`**(safety-admin-client.ts:336,
  decisions UI 的 coworker 下拉筛选用)。**原方案称 `/agents` 前端无消费者,是错误的** —— 删 admin.py
  前必须把这处 repoint 到 `/api/v1/coworkers`,否则 decisions 页面断。

`/users`、`/tasks`、`/agents` 的写端点**前端无消费者**(`/agents` 仅被上面那个只读 list 消费);迁移
它们是 API 完整性,前端风险低。

**错误解析适配:** client 的 `jsonOrThrow`(safety-admin-client.ts:140)读 `.detail`;信封改为
`{code,message,details?}` 后须改读 `.message`。

**贯穿原则(必须遵守):**
- v1 约定:统一错误信封 `raise_error_response(...)`(`src/webui/v1/errors.py`,`{code,message,details?}`);
  跨租户一律 **404 不 403**(不泄露存在性)。
- **角色门**:每个 v1 端点挂 `require_action(...)` 声明式依赖(handler body 零 authZ);受
  feat/roles 引入的 **default-deny 元测试**(`tests/webui/test_v1_default_deny.py`)约束 —— 新端点
  要么挂 `require_action`,要么进显式 allowlist(带 justification)。
  **该元测试有两个子测试**:① 每条 v1 路由必须被 `require_action` 门控或在 `AUTH_ONLY_V1_ROUTES`
  allowlist 中;② **AST 静态扫描 `src/webui/*.py` 里所有 `require_action("…")` 字面量,要求每个 action
  字符串都存在于 `_USER_ROLE_ACTIONS`**。删 legacy action 时这条会兜底(见 §3 / 【修订4】)。
- **能力 vs 归属分离**:能力门用 `require_action`;"操作自己的资源"用 `require_manage_or_owner`
  归属逃生(见 `src/webui/dependencies.py`)。本任务**不涉及归属逃生**(都是租户级管理资源)。
- **OpenAPI 是 design-first**:`contracts/openapi.yaml` 是**真理源**(手写维护),`types.ts` 由
  `openapi-typescript` 从 yaml 生成(`npm run openapi:gen`)。补全契约 = **编辑 yaml + 重新生成 TS**,
  不是 dump FastAPI。受 `tests/test_openapi_codegen_freshness.py`(yaml↔ts 漂移,字节级 diff)和
  `tests/test_openapi_contract.py`(yaml↔Pydantic↔实时响应)约束。

## 2. 目标与范围

### ✅ IN SCOPE
1. 在 v1 新建/扩展端点,覆盖 admin.py 的 4 类独占功能(users / tenant / safety-写含 CSV / tasks),
   全部挂 v1 角色门、用 v1 错误信封、404 约定。
2. 把 `web/src/services/safety-admin-client.ts` 的 **全部 11 处** `/api/admin/*` 调用
   **repoint 到新 v1 端点**(含 `/api/admin/agents` → `/api/v1/coworkers`、`decisions.csv`),
   并适配 `.detail` → `.message` 错误解析。
3. **删除整个 `src/webui/admin.py`**(及其在 `main.py` 的挂载),包括纯重复端点(agents/bindings/
   conversations/**skills**)。
4. **补全 `contracts/openapi.yaml`**:新增上述 v1 端点的 paths/schemas/security,重新生成 `types.ts`。
5. 角色表新增对应 fine-grained action(见 §3),并在 admin.py 删除后**清理不再被引用的 legacy 粗粒度
   action 及其在 `dependencies.py` 的快捷定义**(见 §3 / 【修订4】)。
6. **【修订5】** 迁移/改写依赖 admin.py 的 **15 个测试文件**(详见 §7),其中 safety e2e 的可逆性
   往返覆盖须**迁移到 v1 保留**,不可一删了之。

### ❌ OUT OF SCOPE
- 任何**新业务逻辑/字段** —— 这是**等价迁移**:v1 端点的行为与 admin.py 原端点一致(同样的 DB 调用、
  同样的响应形状),只换前缀、错误信封、角色门、404 约定。**包括等价搬运 safety 规则创建/更新的
  可逆性守卫逻辑(admin.py:804-839)。**
- 凭证 pool / 用量计量 / 配额(另立任务)。
- 前端**功能/样式改动** —— 只做 `/api/admin` → `/api/v1` 的 URL repoint + 因错误信封变化所需的最小适配。

## 3. 角色权限控制(本任务的重点,新端点逐一指定)

当前 `_TENANT_ROLE_ACTIONS`(`src/rolemesh/auth/permissions.py:32-79`)已有 fine-grained:
`agent.create/manage/use`、`skill.create/manage`、`mcp.configure`、`approval_policy.manage`、
`credential.byok.manage`、`safety.read`;`platform_admin` 由 `_all_known_actions()` **派生为超集**
(新增 action 自动授予,勿手加)。

**本任务新增 action(加进对应租户角色集,平台超集自动覆盖):**

| 新 action | owner | admin | member | 用于 |
|---|:--:|:--:|:--:|---|
| `user.manage` | ✅ | ✅ | — | `/users*` 全部写读 |
| `tenant.manage` | ✅ | — | — | `/tenant` GET/PATCH(**owner only**,对齐旧 `OwnerUser`)|
| `safety.rule.manage` | ✅ | ✅ | — | safety 规则 POST/PATCH/DELETE |
| `task.manage` | ✅ | ✅ | — | 定时任务删除/管理 |

**端点 → 角色门映射(逐一,务必与旧面 gate 等价):**

| 新 v1 端点 | 方法 | `require_action` | 旧面对应 gate |
|---|---|---|---|
| `/api/v1/users`、`/users/{id}` | GET/POST/PATCH/DELETE | `user.manage` | `UserManager`=`manage_users`(admin+)|
| `/api/v1/tenant` | GET/PATCH | `tenant.manage` | `OwnerUser`=`manage_tenant`(owner)|
| `/api/v1/safety/rules`、`/rules/{id}` | POST/PATCH/DELETE | `safety.rule.manage` | `AdminUser`=`manage_agents` |
| `/api/v1/safety/checks` | GET | `safety.read`(已存在)| `AdminUser` |
| `/api/v1/safety/decisions*`、`/rules/{id}/audit`、`/decisions.csv` | GET | `safety.read`(已存在)| `AdminUser` |
| 定时任务删除/管理(扩 `v1/schedules`)| DELETE/PATCH | `task.manage` | `AdminUser` |
| 定时任务读(`v1/schedules` 现有)| GET | **见下【修订7】** | — |

说明:
- safety **读**(decisions/audit/checks/csv)复用已有 `safety.read`;**写**用新 `safety.rule.manage` ——
  读写分离,对齐"admin 管租户 safety"。注:旧面 safety 读写都门控在 `manage_agents`(AdminUser);拆成
  `safety.read`(读)+`safety.rule.manage`(写)是细化,在 admin+ 层级等价。
- tenant 设置是 **owner-only**(`tenant.manage` 只给 owner),不要给 admin。
- 不需要"归属逃生" —— 这些都是租户级管理资源(users/tenant/safety/tasks),纯角色门即可,
  **不涉及 `require_manage_or_owner`**(那只用于 agent/skill 的个人 private 资源)。
- **【修订7】** v1/schedules.py 现有两条 GET 路由用的是 `get_current_user`(auth-only),
  **并非某个"read action"**,它们靠列入 `test_v1_default_deny.py` 的 `AUTH_ONLY_V1_ROUTES` allowlist
  通过元测试。新增的写路由(DELETE/PATCH)挂 `task.manage`;现有 GET 路由保持 auth-only allowlist 现状
  即可(不在本任务给读路由发新 action)。
- **【修订4】删除 admin.py 后的 legacy action 清理(三处必须同步,否则元测试红):**
  1. `src/rolemesh/auth/permissions.py`:从 `_TENANT_ROLE_ACTIONS` 删除不再被任何 `require_action`
     引用的 `manage_tenant` / `manage_users` / `manage_agents`;以及自始至终无 enforce 站点的
     `view_all_conversations` / `use_agent`(仅作元数据存在,grep 确认无引用后删)。
  2. `src/webui/dependencies.py:128-130`:**删除 `require_manage_tenant` / `require_manage_agents` /
     `require_manage_users = require_action("manage_*")` 这三行**。⚠️ 它们是字面量 `require_action("…")`
     调用,若只删角色表不删这三行,default-deny 的 AST 子测试会扫到孤立字符串而**变红**。
  3. `tests/auth/test_permissions_role_model.py:46-50`:该测试断言上述 legacy action 在角色集中存在,
     清理后须同步更新。
  - grep 确认顺序:删 admin.py 后,`grep -rn 'require_action("manage_' src/`、
    `grep -rn 'manage_tenant\|manage_users\|manage_agents\|view_all_conversations\|use_agent' src/`
    应只剩 permissions.py 自身定义(随之删除)。

## 4. 详细需求(等价迁移)

对每组,**先读 admin.py 原 handler**,在 v1 复刻其 DB 调用与响应形状,只换:前缀 / 错误信封 /
`require_action` / 404 约定。

1. **users**:新建 `src/webui/v1/users.py`(`APIRouter(prefix="/users")`),迁
   list/create/get/update/delete(admin.py:341-408)。响应模型复用 `webui/schemas.py` 的 `UserResponse`
   等(或在 `schemas_v1.py` 对齐)。注意保留 admin.py:353 的"只有 owner 能创建 owner-role 用户"这条
   handler 内规则(等价搬运,作为 422/403 信封)。
2. **tenant**:新建 `src/webui/v1/tenant.py`(`prefix="/tenant"`)。**核实:`/tenant/credentials` 已被
   `credentials.py`(`prefix="/tenant/credentials"`)占用,二者是子路径关系,不冲突。** 迁 GET/PATCH
   (admin.py:313-333)。
3. **safety 写/审计/CSV**:**扩展现有 `src/webui/v1/safety.py`**,加:
   - rules 写:POST `/rules`、PATCH/DELETE `/rules/{id}`(迁 admin.py:876/953/999),**含可逆性守卫
     (admin.py:804-839)等价搬运**;
   - `/checks`(迁 1048)—— 注:可能已部分存在于 v1/safety.py(`GET /checks`,safety.py:317),核对后
     避免重复,缺则补;
   - decisions:`GET /decisions`、`GET /decisions/{id}` —— **v1/safety.py 已有只读 decisions(359/412)**,
     核对其与 admin.py:1101/1149 是否形状一致;若一致则前端直接 repoint 到现有 v1,无需新增;
   - **`/decisions.csv` 流式导出(迁 admin.py:1293-1338)** —— 【修订3】原方案未单列。v1 新增
     `GET /decisions.csv`,沿用 `db.stream_safety_decisions(...)` 流式游标 + `StreamingResponse`,
     `safety.read` 门控;
   - audit:`GET /rules/{id}/audit` —— **v1/safety.py 已有(292)**,核对后复用。
   - **路径改造**:旧面 decisions/audit/csv 路径带显式 `{tid}`,v1 一律**从认证用户的 tenant 推导**
     (不再 URL 传 tenant_id,跨租户经 RLS/404)。
4. **tasks**:**扩展现有 `src/webui/v1/schedules.py`**,加删除/管理(迁 admin.py 的 `/tasks` 661、
   `/tasks/{id}` DELETE 677、`/agents/{id}/tasks` 667)。**不要新建 `/tasks` router** —— scheduled_tasks
   是同一资源,统一在 `/schedules`。新增写路由挂 `task.manage`(见 §3【修订7】)。
5. 在 `src/webui/api_v1.py` `include_router` 新 router(users、tenant)。
6. **删除** `src/webui/admin.py` 全文件 + `main.py:308-309` 的 `app.include_router(admin_router)` +
   `from webui.admin import router as admin_router`,以及纯重复端点(agents/bindings/conversations/
   **skills**,随文件删除一并消失)。

## 5. 前端 repoint
- `web/src/services/safety-admin-client.ts`:把 **全部 11 处** `/api/admin/...` 改为对应 `/api/v1/...`:
  - safety rules/checks/decisions/audit、`/api/admin/tenant` → `/api/v1/tenant`;
  - decisions/audit/**csv** 去掉 URL 里的 `{tenantId}`(改由后端从会话推导);
  - **【修订1】`listCoworkers()` 的 `/api/admin/agents` → `/api/v1/coworkers`**(注意核对 v1 coworkers
    list 的响应形状,client 只取 `{id,name}`,做必要的字段映射);
  - **【修订3】`decisionsCsvUrl()`/`downloadDecisionsCsv()` 的 `decisions.csv`** 同步 repoint 并去
    `{tenantId}`;注意它是"拼 URL 带 bearer 下载"的形态,确认 v1 新 URL 形状后改。
- **错误解析适配**:`jsonOrThrow`(:140)从读 `.detail` 改为读 `.message`(信封 `{code,message,details?}`),
  保留 statusText 兜底。
- 全仓库 `grep -rn "/api/admin" web/src` 必须归零。

## 6. OpenAPI 契约补全
- **手工编辑 `contracts/openapi.yaml`**:新增 users / tenant / safety-写(含 `decisions.csv`)/ tasks-管理
  的 paths、request/response schemas、security(沿用现有 Bearer securityScheme)。命名/风格对齐现有 v1 条目。
  - CSV 端点的响应在 yaml 里声明为 `text/csv`(`StreamingResponse`),与现有 JSON 端点风格区分。
- 重新生成 `web/src/api/generated/types.ts`(`npm run openapi:gen`,即
  `web/node_modules/.bin/openapi-typescript contracts/openapi.yaml -o web/src/api/generated/types.ts`)。
- `test_openapi_codegen_freshness.py`(yaml↔ts 字节级)与 `test_openapi_contract.py`(yaml↔Pydantic↔
  实时响应)必须绿。

## 7. 改动面 / 文件地图(已补全)
- **新增**:`src/webui/v1/users.py`、`src/webui/v1/tenant.py`。
- **扩展**:`src/webui/v1/safety.py`(含 CSV)、`src/webui/v1/schedules.py`、`src/webui/api_v1.py`、
  `schemas_v1.py`。
- **改**:
  - `src/rolemesh/auth/permissions.py`(加 4 个 action;删 legacy 未用 action)、
  - **【修订4】`src/webui/dependencies.py`(删 128-130 的 `require_manage_*` 三行)** —— 原方案漏列,
  - `src/webui/main.py`(摘除 admin_router import + include_router)。
- **删**:`src/webui/admin.py`。
- **改前端/契约**:`web/src/services/safety-admin-client.ts`、`contracts/openapi.yaml`、
  `web/src/api/generated/types.ts`。
- **【修订5】测试**(影响远大于"删旧测试"):
  - **新增** v1 的 users/tenant/safety-写/tasks 的 403 与功能用例;default-deny 元测试自动覆盖路由门控。
  - **迁移保留覆盖**(不可纯删):`tests/safety/e2e/test_rest_to_audit.py`、
    `tests/safety/e2e/test_reversibility_roundtrip.py`(可逆性往返,对应 §4.3 守卫逻辑)。
  - **改写 override 模式**(15 文件使用 `require_manage_*` dependency override,删 action 后失效):
    `tests/safety/e2e/test_rest_to_audit.py`、`tests/safety/e2e/test_reversibility_roundtrip.py`、
    `tests/test_skills_integration.py`、`tests/safety/test_rest_discovery.py`、`tests/safety/test_api.py`、
    `tests/safety/test_csv_export.py`、`tests/safety/test_rest_validation.py`、
    `tests/safety/test_rules_audit.py`、`tests/webui/test_skills_api.py`、
    `tests/webui/test_assignment_removal_regression.py` 等 —— 把对 admin.py 的 override/请求改为对 v1。
  - **更新**:`tests/auth/test_permissions_role_model.py:46-50`(去掉对已删 legacy action 的断言,
    新增对 4 个新 action 的角色归属断言)。

## 8. 验收标准
- [ ] 旧面功能在 v1 全部可用:users CRUD(`user.manage`,admin+)、tenant GET/PATCH(`tenant.manage`,
      owner)、safety 规则写/checks/decisions/audit/**csv 导出**(写=`safety.rule.manage`、读=`safety.read`,
      admin+)、task 删除/管理(`task.manage`,admin+)。各自 403 用例通过。
- [ ] 全部走 v1 错误信封 + 跨租户 404;default-deny 元测试两个子测试均通过(无裸奔 v1 路由 + 所有
      `require_action` 字符串在角色表)。
- [ ] `src/webui/admin.py` 已删除,`main.py` 不再挂载;`dependencies.py` 的 `require_manage_*` 已删;
      全仓库 `grep "/api/admin"`(`src/` 与 `web/`)归零。
- [ ] **前端 decisions UI 的 coworker 下拉(原 `/api/admin/agents`)与 CSV 导出仍可用**(repoint 后回归)。
- [ ] `contracts/openapi.yaml` 收录全部新端点(含 `text/csv` 的 csv 导出);`types.ts` 重新生成;
      `test_openapi_codegen_freshness.py` 与 `test_openapi_contract.py` 绿。
- [ ] 角色表清理:无 `require_action` 引用的 legacy action 已移除;`platform_admin` 仍为派生超集;
      `test_permissions_role_model.py` 已更新。
- [ ] 受影响的 15 个测试文件已迁移/改写,safety 可逆性覆盖在 v1 保留;`uv run pytest` 全绿
      (需 Docker/Postgres testcontainers)。
- [ ] `uv run ruff check src tests` 干净(ruff 是 CI 硬门;mypy 非门,保持零新增错误)。

## 9. 风险 / 提示
- **等价迁移、勿改语义**:逐端点对照 admin.py 原实现,DB 调用与响应形状保持一致,降低回归面。
  尤其搬运可逆性守卫(804-839)与 owner-only 用户创建规则(353)。
- **【修订1】`/api/admin/agents` 有前端只读消费者** —— 删 admin.py 前先 repoint `listCoworkers()` 到
  `/api/v1/coworkers`,否则 decisions 页面 coworker 筛选断。
- **【修订3】CSV 导出是流式 + 前端拼 URL 带 token 下载**,迁移时 URL 形状改变,前端 `decisionsCsvUrl`
  须同步;别遗漏。
- **decisions/audit/csv 去 URL tenant**:旧面用 `/tenants/{tid}/...`;v1 应从认证用户 tenant 推导,跨租户
  靠 RLS + 404。前端 client 同步去掉该路径参数。
- **【修订4】删 legacy action 三处同步**:permissions 表 + `dependencies.py:128-130` + 角色模型测试;
  缺一则 default-deny AST 子测试或 role-model 测试变红。
- **【修订2】admin skills(1403-1678)是纯重复但原方案漏列**:删除前再 grep 确认前端无 `/api/admin/...
  /skills` 消费者(已核实无),v1 skills 覆盖含文件操作。
- **工作量重估**:admin.py 单文件 1678 行 + 15 个测试文件迁移 + yaml + types.ts,**实际更接近 2000+ 行**
  (原"800–1500"偏乐观);"2–3 个中等 PR"的拆分仍合理。
- **建议提交顺序**(每步独立可测):
  ① 加 4 个新 action + v1 新端点(users/tenant)+ 测试;
  ② 扩 safety/schedules 写端点(含 CSV、可逆性守卫)+ 测试(迁可逆性 e2e);
  ③ 补 openapi.yaml + 重生成 types.ts;
  ④ 前端 repoint(含 `/agents`→coworkers、csv、`.detail`→`.message`),`grep /api/admin web/src` 归零;
  ⑤ 删 admin.py + 清 legacy action。**⑤ 必须把 `dependencies.py:128-130` 的删除与 action 删除放在
     同一提交**,否则中间态 default-deny 测试红;同步改写 15 个测试文件。

## 10. 本次修订集中清单
- **【修订1】** 改正"`/agents` 前端无消费者":`safety-admin-client.ts:336 listCoworkers()` 调
  `/api/admin/agents`,须 repoint 到 `/api/v1/coworkers`。(§1、§2.2、§5、§8、§9)
- **【修订2】** 补列纯重复的 admin **skills CRUD + 文件操作**(1403-1678),v1 已覆盖,删除安全。(§1)
- **【修订3】** 补列 safety **`decisions.csv` 流式导出**端点(后端迁移 + 前端 repoint + yaml `text/csv`)。
  (§1、§4.3、§5、§6、§8)
- **【修订4】** 补列删 legacy action 时**必须同步删 `dependencies.py:128-130` 的 `require_manage_*`**,
  否则 default-deny AST 子测试红;并更新 `test_permissions_role_model.py`。(§3、§7、§9)
- **【修订5】** 明确**15 个测试文件**需迁移/改写,safety 可逆性 e2e 覆盖须迁移保留而非纯删。(§2、§7、§8)
- **【修订6/7】** 校正 schedules 现有 GET 为 auth-only allowlist(非"read action");校正 safety
  读写在旧面同为 `manage_agents`,v1 拆 `safety.read`/`safety.rule.manage` 为细化等价;工作量重估为 2000+ 行。
  (§3、§9)
