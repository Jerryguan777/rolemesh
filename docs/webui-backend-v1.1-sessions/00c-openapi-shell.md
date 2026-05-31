# Session 00c — OpenAPI 脚手架 + `<rm-app-shell>` 前端抽离

| field | value |
|---|---|
| Phase | 0 |
| Prerequisites | 00a done（`/api/v1/backends` 已存在）；00b 不强依赖但建议先做（避免 schema 变化导致 codegen 反复）|
| Estimated PRs | 2-3 |
| Estimated LOC | ~800 (openapi.yaml + codegen pipeline + frontend shell + chat 接入) |
| Status | not started |

## Goal

把 `/api/v1` 的 OpenAPI 契约写起来 + TS codegen 跑通 + 前端 `<rm-app-shell>` 抽出来。这之后所有 Phase 1+ 的前端工作都基于这个 shell 与 codegen，**不再容许手写 fetch URL 字面量**。

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §3（API 端点）/ §6（UI 设计）/ §13（错误码）
2. [`docs/5-webui-architecture.md`](../5-webui-architecture.md) —— 现有 webui 架构
3. `web/` 目录 —— 现有前端结构（找现有的 chat-panel 组件，理解 Lit + Tailwind 模式）
4. `src/webui/admin.py` —— 现有 `/api/admin/*` endpoints 形态（要把 schema 抄到 openapi.yaml 里作为对照）
5. `src/webui/main.py` —— FastAPI app 入口，看怎么挂 `response_model` 使其自动出 OpenAPI

## Scope — PR breakdown

### PR 1 — `contracts/openapi.yaml` 初稿 + codegen pipeline

**契约文件位置**：`contracts/openapi.yaml`（设计 §1 写"web/src/api/generated/" 是 codegen 输出位置，**契约源文件**放 `contracts/openapi.yaml`）。

**包含范围**：

- 设计 §3 列的所有 Phase 1 endpoints + `/api/v1/backends`（已存在）
- 不需要每个都有实现——契约先行
- 每个 endpoint 必须：
  - `summary` + `description`（DELETE 必须说清楚是 409 还是级联，按设计 §3 表）
  - `requestBody` / `responses` 引用 `components/schemas`
  - 错误响应至少包含 `400` / `401` / `403` / `404` / `409`（按需）+ 统一引用 `ErrorResponse` schema
- `components/schemas/ErrorResponse`（按设计 §13）：
  ```yaml
  ErrorResponse:
    type: object
    required: [code, message]
    properties:
      code: {type: string}
      message: {type: string}
      details: {type: object, additionalProperties: true}
  ```
- 所有 enum 用 `enum:` 显式列举（避免 codegen 出 string）

**Codegen pipeline**：

- 装 `openapi-typescript`（已是常用方案）：`npm install -D openapi-typescript`
- `package.json` 加 script：
  ```json
  "openapi:gen": "openapi-typescript ./openapi.yaml -o ./src/api/generated/types.ts"
  ```
- 加一个 minimal client 封装 `web/src/api/client.ts`（不要造大轮子）：
  ```ts
  import type { paths } from "./generated/types";
  // 一个最薄的 fetch wrapper，typed-paths input/output
  ```
- CI lint：`tests/test_openapi_codegen_freshness.py` 或一个 npm script，跑 codegen 再比 diff，有 diff 则 CI 失败（防止 yaml 和 ts 不同步）
- FastAPI 端：在 `webui/main.py` 加 `response_model=` 校验，确保实际返回 matches contract（这一步对 `/api/v1/backends` 已可做）

**Acceptance**：
- `npm run openapi:gen` 跑通无报错
- 生成的 `types.ts` 提交进 git（明确"generated, do not edit"header）
- `pytest tests/test_openapi_codegen_freshness.py` 绿（如果选 Python lint 路线）
- 至少一个 endpoint（`/api/v1/backends`）走 typed client 调通

### PR 2 — `<rm-app-shell>` 抽离 + chat 接入

**Background**：设计 §6.2 整体布局——sidebar + topbar，现有 chat 必须包进来（不允许 chat 独立保留旧布局）。

- 新建 `web/src/components/app-shell.ts`：
  - Lit element `<rm-app-shell>`
  - 默认 slot 给 main content；命名 slot `sidebar-extra` / `topbar-extra` 给页面扩展
  - sidebar 项目按设计 §6.1 列：Chat / Coworkers / MCP / Models / Skills / Credentials / Bindings / Safety
  - 未实现的页面 router 进去显示 "Coming soon — Phase X" 占位
- 改 hash router（保留现有方案，不引 React Router）：
  - 一个集中的 `web/src/router.ts` 把 hash → component class 映射
  - shell 通过 `<rm-router-outlet>` 渲染当前 component
- 把现有 chat-panel 包进 shell（不是简单 wrap，要确保现有所有 chat 行为不退化）
- Dark mode（设计 §15 第 4 条）：CSS 加 `@media (prefers-color-scheme: dark) { :root { ... } }`，沿用现有 `--color-d-*` token，**不加 toggle**（Phase 1 不做）
- sidebar 高亮当前路由

**Acceptance**：
- chat 行为 100% 不退化（消息流、token streaming、reconnect 都还工作）
- 其它 sidebar item 点击进 "Coming soon" 页（明确标 Phase）
- 切换系统暗色模式，UI 跟着变
- 现有所有前端测试不退化
- 手动 UI smoke：把现有 chat 全流程走一遍

### PR 3 (optional) — Bootstrap smoke 加 openapi check

如果 PR 1 的 freshness check 选了 npm script 路线，把它加进 00a 的 `smoke_bootstrap.sh`。

## Acceptance criteria（session 级）

- [ ] `contracts/openapi.yaml` 覆盖 Phase 1 + `/api/v1/backends` 的全部 endpoints（哪怕实现还没有）
- [ ] `npm run openapi:gen` 跑通，types.ts 生成
- [ ] codegen freshness check 绿（yaml 改不更新 ts 时 CI 红）
- [ ] `<rm-app-shell>` + chat 接入完成；现有 chat 行为不退化
- [ ] 系统 dark mode 切换 UI 跟着变
- [ ] `pytest` 全套通过
- [ ] 手动 UI smoke：chat 全流程跑通；sidebar 各 entry 跳得到（占位也行）
- [ ] 更新 `docs/webui-backend-v1.1-plan.md` 状态

## Out of scope

- ❌ 实现 Phase 1 任何业务 endpoint（除 `/api/v1/backends`）—— 留 01a
- ❌ WS 新协议 —— 留 01b
- ❌ Coworkers / MCP / Models / Credentials 页面真实现 —— 占位即可，留 Phase 1+
- ❌ Dark mode toggle —— 设计 §15 决定 Phase 1 不做
- ❌ React Router / 路由库引入 —— 保留 hash router

## Open questions

1. **`web/` 现有结构**：用了 Vite？还是 webpack？还是其它 bundler？openapi:gen 怎么 hook 进现有 build？（先看 `web/package.json`）
2. **`response_model=` 校验**：FastAPI 模式现有 admin.py 是怎么做的？要不要 PR 1 顺手把 admin 的也改成 explicit `response_model`（让 OpenAPI 自动出更准）？或者保持现状只管 v1？
3. **sidebar 项目顺序**：设计 §6.1 列了路由但没说排序优先级。chat 第一应该没问题，其它按 phase 排序？还是按使用频率？

## Pitfalls

- **OpenAPI freshness check 必须 run codegen + diff**，不能只是"yaml 有没有改过"——前者抓的是真不一致，后者只抓"忘改 yaml"
- 现有 chat-panel 抽 shell 时容易**漏掉某个 dom event listener**（reconnect / typing indicator）——抽完后必须 chat 全流程手动测
- `<rm-app-shell>` 不要做太重——只管 sidebar / topbar / outlet，业务逻辑全留页面 component
- dark mode 用 `prefers-color-scheme` 不需要 JS——但 sidebar 高亮等 element 必须用 CSS variable 不要 hardcode 颜色
- TS codegen 生成的 `types.ts` 必须提交进 git（不要 `.gitignore`），否则 frontend dev 拉代码后必须先跑 npm script
- 占位页"Coming soon — Phase 2" 写明 Phase 是为了用户跑 dev 时看出整体进度——别糊弄成单一字符串

## Findings (after execution)

Completed 2026-05-20 in 3 commits on `feat/ui`. Notes for 01a / 01c:

### 现有 bundler / openapi-gen hook 方式
- `web/` 已经是 Vite 6 + Tailwind 4（class-based dark via `prefers-color-scheme`）。没有引入新的 bundler / 编译链。
- Codegen 用 `openapi-typescript@^7` 跑 `npm run openapi:gen`（输出到 `web/src/api/generated/types.ts`，committed）。
- Freshness gate 走两路：
  - `npm run openapi:check`：再跑一次 codegen 到 `/tmp/` 然后 `diff -u`；适合本地、Husky 等同步 hook。
  - `tests/test_openapi_codegen_freshness.py`：跑同一个 `node_modules/.bin/openapi-typescript`，自动 skip 当 `web/node_modules` 没安装；CI 跑 pytest 就会自然触发。
- 没有把 codegen 串进 `vite build`——意图是把 yaml 当成显式的 contract artifact，不让 dev 启动时"顺手把 yaml 改了 ts 没更新"溜过去。CI 红线由 pytest 守。

### openapi.yaml 覆盖范围
完全对齐设计 §3 Phase 1 表 + `/api/v1/backends`：

```
GET   /api/v1/auth/config
POST  /api/v1/auth/ws-ticket
GET   /api/v1/me
GET   /api/v1/backends                       (实现 + response_model=)
GET   /api/v1/coworkers
POST  /api/v1/coworkers
GET   /api/v1/coworkers/{id}
PATCH /api/v1/coworkers/{id}
DELETE /api/v1/coworkers/{id}                (级联 — 见 §3 表)
GET   /api/v1/coworkers/{id}/conversations
POST  /api/v1/coworkers/{id}/conversations
GET   /api/v1/conversations/{id}
DELETE /api/v1/conversations/{id}            (级联)
GET   /api/v1/conversations/{id}/messages
GET   /api/v1/runs/{id}
POST  /api/v1/runs/{id}/cancel               (409 = 已终态)
```

`/api/v1/conversations/{id}/stream` (WS) 故意没列：OpenAPI 3 不能干净表达 WS event protocol，转而通过 yaml 顶部 `info.description` 指向设计 §4。

### chat-panel 抽进 shell 时发现的隐藏依赖
- `rm-chat-panel` 在 constructor 直接读 `location.search`（`agent_id` / `token` / `chat_id`）和 `sessionStorage.getItem('rm_id_token')`，并构造 `AgentClient`。这是在路由变化时**每次重新挂载**就会发生的——目前不算 bug，因为 query string 不会跨页面变化；但等 Phase 1 把 chat URL 改成 `#/coworkers/:id/conversations/:cid`，要把这些字段移到 props 或 hash params。
- `rm-chat-panel` 自身包含 `rm-sidebar`（conversation 列表 sub-sidebar）。设计 §6.2 sidebar 是**应用级 nav** (Chat/Coworkers/...)；本 session 没合并这两个 sidebar，所以 chat 页面会出现"app-nav (w-52) + conversation-list (w-64)"两个左侧栏。可接受为 v1（spec 明确说"不要重写 chat-panel 逻辑"）；UX 优化留给 01c。
- `rm-chat-panel` 在 `connectedCallback` 直接 `style.height = '100%'`。要求父容器有 resolved height——shell 的 `<main class="flex-1 min-h-0 flex flex-col overflow-hidden">` 满足这个，build 通过。
- 全局事件 `rm-token-refreshed` / `rm-auth-failed` 都监听在 `window`，与 mount/unmount 解耦，shell 切换路由不会丢监听。

### sidebar 占位页实现
独立 component `<rm-coming-soon>`（`web/src/components/coming-soon.ts`），通过 `label` + `phase` 两个 prop 区分。路由表 `web/src/router.ts` 把每个未实现 route 的 `render` factory 直接绑到这个 component 上——加新 sidebar item 只需要在 `ROUTES` 加一行，sidebar / 占位 / 路由匹配三处自动跟上。

`<rm-router-outlet>` 单独写成一个 component（不直接塞进 `<rm-app-shell>` 的 render），是因为 outlet 自己监听 `hashchange` + 维护 `route` state；如果挂到 shell 里，shell 还要分清"我自己需要重渲染的 currentHash"与"outlet 需要重渲染的 route"，两路重渲染竞争反而比拆分复杂。

### Codegen freshness check 实现路径
- `web/src/api/generated/types.ts` 被 commit 进 git（明确头部带 `auto-generated` 标记）。
- `tests/test_openapi_codegen_freshness.py` 用同一个本地 `node_modules/.bin/openapi-typescript` 重新生成到 `tmp_path` 然后字节级比较；不一致直接 `pytest.fail` 并打印 `diff -u` 前 80 行。
- 第二个测试 `test_codegen_output_carries_do_not_edit_marker` 始终运行（不依赖 node），守"有人手改 types.ts"这条线。
- 第三个测试文件 `tests/test_openapi_contract.py` 是 codegen 不能管的横向漂移：yaml↔Pydantic（schemas_v1）↔实际 HTTP 响应↔code 常量（ALL_BACKENDS）。`required` 集合**强相等**（不是 subset），所以"yaml 加字段但 handler 没返"也会红。
- smoke_bootstrap.sh 已 hook 上述两个测试。

### 对 01a / 01c 的影响

**Typed client 用法约定（给 01a 用）**：
- 所有新 endpoint 必须**先**改 yaml、跑 `npm run openapi:gen`、commit 生成的 `types.ts`；然后在 `web/src/api/client.ts` 里加对应方法。一个端点一个方法；不要折腾运行时 path-builder。
- Pydantic 一侧：每个新 endpoint 加 `response_model=` 指向 `webui.schemas_v1` 里对应的 model。在 `tests/test_openapi_contract.py` 里加一个 `Backend`-风格的 yaml/Pydantic `required` 集合相等测试。这是廉价的回归保险，加一个 endpoint 大概 5 行测试。
- 错误响应**必须**用 `ErrorResponse`（schemas_v1）即 `{code, message, details?}`；handler 抛 `HTTPException` 时把这个塞进 `detail` 字段（FastAPI 默认把 detail 当 body 顶层"detail"，需要自己 wrap——这块到 01a 实际 wire coworker DELETE 409 时再决定确切 helper 函数）。

**Shell 子路由 outlet 怎么挂（给 01c 用）**：
- 默认全用 `<rm-router-outlet>`。如果 Phase 1+ 某个页面要 sub-tab（例如设计 §6.3 C `#/coworkers/:id/{overview,skills,...}`），**不要**新建一个 outlet——在 page component 内部消化 hash 后缀。`web/src/router.ts` 的 longest-prefix 匹配机制已经验证（hash `#/coworkers/abc` 正确解析为 `coworkers` route）。
- 占位页迁移真实现：把 `ROUTES[i].render` 改成 `() => html\`<rm-coworkers-page></rm-coworkers-page>\`` 即可；旧的 coming-soon 组件保留——下游 Phase 还要复用。
- App-shell 的 topbar 目前对 chat 页**隐藏**（chat-panel 自有 header），对其它页显示。01c 若要在 chat 上加 topbar，需要先把 chat-panel 里的 brand/header 收掉，避免双 header。建议在 01c 那个 session 一起处理。
- App-shell 的 nav 没做 collapse（chat-panel 自带的 sub-sidebar 还能 collapse）。如果 Phase 2+ 要让 app-nav 也能 collapse，加一个 `@state collapsed` 即可，没有结构层阻碍。

### Live UI smoke 结果（2026-05-20 后补）
后续在同一台机器上把 Postgres / NATS / orchestrator 真起起来跑了一遍 Playwright 驱动的 smoke，全部通过：

- App-shell 渲染 + sidebar 项顺序 + Phase tag 都对（Chat / Coworkers P1 / MCP servers P2 / Models P2 / Skills P3 / Credentials P2 / Bindings P2 / Safety）
- chat 走 `ADMIN_BOOTSTRAP_TOKEN` fast-path 直接 WS 接入：发消息 → 收到 verbatim 回复；orchestrator 端 `Agent output chars=N` 落账
- sidebar 切到 `#/skills` 渲染 `<rm-coming-soon label="Skills" phase="Phase 3">`；active 项 `bg-brand/10` + `aria-current="page"`；topbar 对 chat 隐藏、对其它页显示——和 §6.2 设计一致
- conversation 切换：sub-sidebar 点旧 conversation，url `chat_id` 跟着改、历史消息正确加载、WS 不掉
- WS reconnect：client-side `ws.close(1000)` 触发 `AgentClient.onclose` 重连分支，4 秒内拿到新 WS 对象、readyState=OPEN，post-reconnect 发消息回得来
- dark mode：4 处 `@media (prefers-color-scheme: dark)` 全部走 `--color-d-*` token，无 hardcode

**01c 要先认的一个预存行为**（feat/ui 没改 chat-panel 逻辑、不是本 session 引入）：`AgentClient` 把 WS close code 4000-4999 当作"intentional close, do not reconnect"（`web/src/services/agent-client.ts:123-125`），逻辑没错；但 `<rm-chat-panel>` 的 "Connected" 文字 badge 不会因为 close 而切到 "Disconnected"——UI 显 stale 直到下一次 mount。01c 做 chat polish 时建议把 connection state 真订阅到 `AgentClient._connected` 上。

Dev smoke 复现路径（给 01c 用）：`docker compose -f docker-compose.dev.yml up -d` → `.venv/bin/rolemesh &` → `.venv/bin/rolemesh-webui &` → 浏览器开 `http://localhost:8080/?agent_id=<coworker-uuid>` 同时在 console `sessionStorage.setItem('rm_id_token', '<ADMIN_BOOTSTRAP_TOKEN>')` 绕过 OIDC——足够跑通 chat smoke。多用户 / 真 OIDC 测试还是要留给 03a。
