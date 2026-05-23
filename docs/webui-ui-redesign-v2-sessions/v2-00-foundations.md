# Session v2-00 — Foundations: tokens + primitives + router

| field | value |
|---|---|
| Phase | v2 cycle start |
| Prerequisites | v1.1 已合 main；当前 `feat/ui-v2` 分支 |
| Estimated PRs | 4-5 |
| Estimated LOC | ~600 |
| Status | not started |

## Goal

为 v2 UI 重设计建立技术 + 视觉地基。这一步打完后所有下游 session 的页面在统一的 design tokens + dialog primitive + wizard primitive + 嵌套 router 上构建。

**不做任何业务页面 reskin**——v2-00 只准备地基，v2-01 开始才碰具体页面。

**v1.1 现有组件全部保留运行**——chat-panel / safety pages / approvals page / coworkers page 等先在新 router 路径下继续挂着工作，后续 session 各自重组+换肤。本 session 后用户访问 web 与 v1.1 完工后**功能一致**（除路由 URL 略变）。

## Required reading

1. [`docs/webui-ui-redesign-v2-design.md`](../webui-ui-redesign-v2-design.md) §7（视觉语言）/ §2（IA 原则）/ §10.1 blocking items
2. [`docs/webui-ui-redesign-v2-prototype.html`](../webui-ui-redesign-v2-prototype.html) —— **重点看 `<style>` 段（前 358 行）**：CSS custom properties 在 `:root`、字体、卡片样式、terracotta/cream tokens
3. [`docs/webui-ui-redesign-v2-plan.md`](../webui-ui-redesign-v2-plan.md) Locked decisions 表
4. `web/src/router.ts` —— v1.1 落地的扁平 hash router；本 session 改成嵌套
5. `web/src/components/app-shell.ts` —— v1.1 落地的 sidebar shell；本 session **保留接口但内部要换 IA**
6. `web/package.json` —— 当前依赖（Lit 3 + Tailwind 4 + Vite 6）；**本 session 不引入新依赖**

## 概念定位：地基为业务页让路

本 session 的 primitive (`<rm-dialog>` / `<rm-wizard>`) 与 v1.1 的 `<rm-inline-approval>` 是同类——独立组件、清晰边界、parent-controlled state。**不要**把 dialog / wizard 做成"会话状态管理框架"或"路由解决方案"——它们只是壳，业务逻辑由调用方各自处理。

## Scope — PR breakdown

### PR 1 — Design tokens + 字体加载

**Goal**：把原型的 visual language 抽成可复用 CSS 变量，业务组件统一引用。

子任务：

1. **新建 `web/src/styles/tokens.css`** —— 从原型 `:root { ... }` 段（约第 30-100 行）抄过来：
   - 颜色：cream backgrounds (`--rm-bg`, `--rm-surface`)、terracotta accent (`--rm-accent`, `--rm-accent-hover`)、ink (`--rm-ink-primary`, `--rm-ink-muted`)、border
   - 字体：`--rm-font-display` (Fraunces serif)、`--rm-font-body` (Hanken Grotesk)
   - 间距 / 圆角 / 阴影 / transition
   - dark mode：用 `@media (prefers-color-scheme: dark)` 复盖（v1.1 dark mode 同模式，不引入 toggle）
2. **字体加载策略**——决策：
   - **Option A**：Google Fonts CDN（`<link>` in `index.html`）—— 最简，加载快但依赖外部
   - **Option B**：self-host woff2（下载放 `web/public/fonts/`）—— 慢启动一次性，无外部依赖
   - **推荐 Option A** —— dev 阶段简单优先；prod 切 self-host 是独立 chore
   - 必须有 system fallback：`font-family: var(--rm-font-display), Georgia, serif;`
3. **`tokens.css` 在哪里 import**：
   - 在 `web/src/main.ts`（应用入口）顶部 `import './styles/tokens.css'`
   - 全局 `:root` scope，所有 light DOM 自然继承
   - **Shadow DOM 透传策略**：CSS custom properties 默认穿透 shadow boundary——这是 spec §7 推荐的做法。每个 Lit 组件 `static styles = css\`:host { color: var(--rm-ink-primary); }\`` 即可拿到
4. **保留 Tailwind**——v1.1 现有组件用 Tailwind 工具类（在 light DOM）继续工作。新 v2 组件**优先用 CSS variables + scoped `css\`\``**，**不**强求 Tailwind（spec §7 提到 Tailwind 4 + Shadow DOM 摩擦）

**pinned tests**：

- vitest：构造一个最小 LitElement，断言 `:host` 内能解出 `--rm-accent` 的真值（颜色变量穿透 shadow）
- playwright：renders 任意一个现有 chat-panel 截图，颜色 token 已生效（terracotta 取代原 indigo）

### PR 2 — `<rm-dialog>` primitive

**Goal**：单步对话框基础组件。Credential / MCP server / Skill 创建编辑都浮在整页外壳之上的对话框（spec §4）。

子任务：

1. **新建 `web/src/components/dialog.ts`**：
   - `<rm-dialog title="..." open>` slot 化 body + slotted footer buttons
   - `open` boolean prop 控制可见性；`close` event 由 X 按钮 / ESC / backdrop click 触发
   - 内部 `<dialog>` HTML5 element 包一层（拿原生 a11y / focus trap / ESC 处理）—— 不要自己实现 modal stacking 逻辑
   - backdrop CSS：semi-transparent cream overlay
   - 视觉对照原型：`.dlg` 与 `.dlghd` class（搜原型 HTML 第 650 行附近的 `<dialog class="dlg">`）
2. **prop 设计**：
   - `title: string` (必填)
   - `open: boolean` (默认 false)
   - `closeOnBackdrop: boolean` (默认 true)
   - `closeOnEsc: boolean` (默认 true)
   - 事件：`@close` (用户关闭)
3. **不做**：
   - state machine (用 `open` prop 即可)
   - portal / teleport (HTML `<dialog>` 浏览器自己处理 z-index)
   - 多 dialog stacking (一个时间只 1 个 dialog 是约定)

**pinned tests** (`web/src/components/dialog.test.ts`)：

- open=true 时 dialog 可见
- 点 X 按钮 emit `close` event
- ESC 键 emit `close` event
- `closeOnBackdrop=false` 时点 backdrop 不触发 close
- slotted footer 渲染正确

### PR 3 — `<rm-wizard>` primitive

**Goal**：多步向导基础组件，coworker 创建用（v2-03）。

子任务：

1. **新建 `web/src/components/wizard.ts`**：
   - `<rm-wizard title="..." steps='["A","B","C"]' current-step="0">` + slot 化 body
   - 左侧 step rail（带步序号 + 名字 + active 高亮 = terracotta circle）
   - 右侧 body slot（由父组件渲染每步的 form）
   - 底部 navigation: Back / Next / Create
   - 父组件 listen `@step-change` event 切换 body 内容
   - 视觉对照原型：`.wiz` / `.wizhd` / `.wsteps` / `.wbody` class（原型第 590 行附近）
2. **prop 设计**：
   - `title: string`
   - `steps: string[]` (step 名字列表)
   - `currentStep: number` (0-indexed)
   - `canAdvance: boolean` (父组件控制；false 时 Next 禁用)
   - `submitLabel: string` (默认 "Create"；最后一步显示)
   - 事件：`@step-change` (用户点 Next/Back 触发)、`@submit` (最后一步点 Create)、`@close` (X 按钮)
3. **不做**：
   - draft state 管理（父组件自己维护跨步状态）
   - 表单验证（父组件控制 `canAdvance`）
   - 异步保存 indicator（父组件在 `@submit` handler 里处理）

**pinned tests** (`web/src/components/wizard.test.ts`)：

- step rail 渲染所有 step 名字 + 当前 active
- 点 Next emit `step-change` to currentStep+1
- 点 Back emit `step-change` to currentStep-1
- 最后一步 Next 按钮显示 `submitLabel` 文案
- `canAdvance=false` 时 Next 禁用
- 点 X emit `close`

### PR 4 — Router 重构：扁平 → 嵌套

**Goal**：从 v1.1 的扁平 9 项 sidebar URL（`#/coworkers`、`#/mcp-servers` 等）改成 v2 三组：
- `#/` —— chat 主壳（默认）
- `#/manage/<page>` —— Settings shell（coworkers / mcp-servers / skills / models / credentials / safety / approval-policies / general / members / appearance）
- `#/activity/<tab>` —— Activity shell（runs / safety-decisions / approvals-log——**runs 暂跳过**，per locked decision #3）

子任务：

1. **改 `web/src/router.ts`**：
   - 加 nested route 匹配（`#/manage/coworkers` 而不是 `#/coworkers`）
   - 旧扁平路由（`#/coworkers`、`#/mcp-servers` 等）**保留兼容**：用 router middleware 把旧路径 redirect 到新路径（`#/coworkers` → `#/manage/coworkers`），避免 bookmark 失效
   - hash 含 `admin` 的路径（`#/admin/safety/rules` / `#/admin/safety/decisions`）暂时**保留**——v2-04 真做 Activity 时再迁
2. **改 `web/src/components/app-shell.ts`**：
   - 当前 sidebar 9 项扁平 → 暂时**保留**但 reroute 到新路径
   - 真正的 chat 主壳 + Settings shell 在 v2-01 / v2-02 加；本 session 只让现有 shell 在新 URL 下工作
3. **加 lint 防止后续回退**：
   - `web/scripts/lint-flat-route.mjs`（继承 v1.1 lint:no-admin-chat 模式）grep 前端代码确认没有指向旧扁平 hash 的硬链接（除 router 的 redirect map）
4. **测试**：
   - vitest：navigate `#/coworkers` → router state.path == `/manage/coworkers`
   - vitest：navigate `#/manage/mcp-servers` → 命中 mcp-servers route
   - vitest：unknown path → 404 占位（沿用 v1.1 落的 coming-soon component）

### PR 5 (可选) — Lit Shadow DOM 策略文档化

**Background**：v1.1 现有组件混用 light DOM 渲染（Tailwind）+ shadow DOM 渲染（Lit 默认）。v2 加入 CSS variables 后两种模式都要兼容。

子任务（如果 PR 1-4 跑通后还有时间）：

- 新建 `docs/webui-ui-redesign-v2-conventions.md`：
  - 何时用 light DOM（用 Tailwind 的现存组件）
  - 何时用 shadow DOM（v2 新组件，CSS variables）
  - 跨模式 token 一致性约定
  - 字体加载 + system fallback 约定
- **不强制做**——如果 PR 1-4 已大致占满 LOC budget，写进 Findings 留下次

## Acceptance criteria

- [ ] `web/src/styles/tokens.css` 存在 + 所有原型 CSS 变量都在
- [ ] 字体加载工作（system fallback 也工作）
- [ ] `<rm-dialog>` + `<rm-wizard>` 单测全绿
- [ ] Router 嵌套化 + 旧扁平 hash redirect 工作
- [ ] **v1.1 chat 流程不退化**：浏览器开 web 能 chat（visual 颜色变了但功能完整）
- [ ] **v1.1 现有 sidebar 全部能 navigate**（每个 entry 跳得到，即使页面内容暂时未 reskin）
- [ ] `npm test` 全绿
- [ ] `npm run build` 通过，bundle size 不爆炸（< 100KB gzipped）
- [ ] OpenAPI codegen + freshness check 仍绿
- [ ] 更新 `docs/webui-ui-redesign-v2-plan.md` 状态表

## Out of scope

- ❌ 任何业务页面 reskin（v2-01+ 各自处理）
- ❌ 真正的 Settings shell / Activity shell 实现（v2-02 / v2-04）
- ❌ Coworker wizard 实际内容（v2-03；本 session 只准备 `<rm-wizard>` primitive）
- ❌ 顶栏 Approvals popover（v2-05）
- ❌ 新依赖引入（state mgmt / animation / form lib）
- ❌ 删除 v1.1 落地的 admin chat fallback（v2 仍兼容性保留）

## Open questions

仍需 session 内决策：

1. **字体加载**：Google Fonts CDN vs self-host woff2 —— 推荐前者（dev 简单）；如果项目有"不依赖外部网络"硬约束告知
2. **旧扁平 hash redirect**：永久保留 redirect 还是只 v2 cycle 期间？推荐永久（成本 0，用户 bookmark 不破）
3. **`<rm-dialog>` 用原生 `<dialog>` vs 自实现**：推荐原生（a11y + focus trap 浏览器免费给）；如果发现 Lit 与原生 `<dialog>` 有怪 bug，session 内决定 fallback

## Pitfalls

- **不要重写现有 chat-panel** —— 本 session 只是把它"包进新 router 路径"，逻辑动一点都不行
- **CSS 变量必须穿透 shadow boundary** —— Lit 组件 `static styles = css\`:host { color: var(--rm-ink-primary) }\``。如果在 shadow DOM 里硬写颜色字面量，跨主题切换会废
- **`<dialog>` 元素的 backdrop 是 `::backdrop` 伪元素** —— 不要在 element 上加 backdrop div
- **Router redirect 用 location.replace 不要用 location.assign** —— 避免 browser back button 卡在旧路径
- **不要做 portal 模拟** —— HTML5 `<dialog>` 浏览器原生处理 stacking context，自己 portal 反而和原生冲突
- **`<rm-wizard>` step rail 不是 router 替代品** —— wizard 内部步骤是 component state，不进 URL；关 wizard 状态全丢是有意（与 spec §3 "点 Create 之前什么都不写" 一致）

## 执行前刷新清单

- [ ] feat/ui-v2 分支干净（git status）
- [ ] v1.1 chat 流程在 main 上还能跑（手动开 web 验证）
- [ ] `web/package.json` 没有新增依赖（本 session 不引）

## Findings (after execution)

_(empty — 重点记录：字体加载策略实际选择 / Lit shadow DOM 与 Tailwind 摩擦的具体表现 / `<dialog>` 原生 vs 自实现的最终选择 / 旧路径 redirect 的实现细节 / 对 v2-01 的影响（特别是 token 命名是否要扩））_
