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

### PR 1 — `web/openapi.yaml` 初稿 + codegen pipeline

**契约文件位置**：`web/openapi.yaml`（设计 §1 写"web/src/api/generated/" 是 codegen 输出位置，**契约源文件**放 `web/openapi.yaml`）。

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
  - sidebar 项目按设计 §6.1 列：Chat / Coworkers / MCP / Models / Skills / Credentials / Bindings / Approvals / Safety
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

- [ ] `web/openapi.yaml` 覆盖 Phase 1 + `/api/v1/backends` 的全部 endpoints（哪怕实现还没有）
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

_(empty — 重点记录：openapi.yaml 覆盖的 endpoints 是不是完全跟 §3 一致？codegen pipeline 用的什么 freshness check 方式？shell 抽离时发现的 chat-panel 隐藏 dependency？)_
