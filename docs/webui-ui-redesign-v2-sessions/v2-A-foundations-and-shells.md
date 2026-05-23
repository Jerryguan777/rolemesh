# Session v2-A — Foundations + Chat shell + Settings shell

| field | value |
|---|---|
| Phase | v2 cycle |
| Prerequisites | feat/ui-v2 分支起好（v1.1 已合 main） |
| Estimated PRs | 4-5 |
| Estimated LOC | ~1300 |
| Status | not started |

## Goal

v2 最大一个 session——把整套视觉地基 + IA 重组一次性完成：

1. **Design tokens** from prototype `:root`（cream + terracotta + Fraunces + Hanken）
2. **`<rm-dialog>` + `<rm-wizard>` primitive**（无逻辑壳，parent-controlled state）
3. **Router 嵌套化**：`#/`（chat shell）/ `#/manage/*`（Settings shell）/ `#/activity/*`（v2-C 占位）
4. **`<rm-chat-shell>`**：左 sidebar 含 coworker 切换器 + 历史会话；中间 chat-panel；顶栏 3 图标 (Activity / Approvals / Settings) + tenant pill
5. **`<rm-settings-shell>`**：分组 sidebar (Coworkers / Building blocks / Governance / Workspace / Account)；11 个内嵌页全部 slot 进去并做 cosmetic reskin

**v1.1 现有业务组件全部保留**——chat-panel / safety pages / approvals page / coworkers page / mcp / models / credentials / skills 都 slot 进新 shell。本 session 后 chat + 配置全栈视觉切到 v2，**功能完全等价于 v1.1**（除 URL 略变）。

## Required reading

1. [`docs/webui-ui-redesign-v2-design.md`](../webui-ui-redesign-v2-design.md) §2（IA 原则）/ §3（chat vs 管理外壳分工）/ §7（视觉语言）/ §10.1 blocking
2. [`docs/webui-ui-redesign-v2-prototype.html`](../webui-ui-redesign-v2-prototype.html) —— **三处重点看**：
   - `<style>` 段（前 358 行）：CSS variables / 字体 / 卡片样式
   - chat shell 布局（默认视图，sidebar + main + topbar）
   - Settings shell 布局（点齿轮后弹出的右侧主面板 + 左侧分组导航）
3. [`docs/webui-ui-redesign-v2-plan.md`](../webui-ui-redesign-v2-plan.md) **Locked decisions 表**（13 条全部不要再讨论）
4. `web/src/router.ts` —— v1.1 落地的扁平 hash router
5. `web/src/components/app-shell.ts` —— v1.1 现有 shell（本 session 收窄成 fallback）
6. `web/src/components/chat-panel.ts` —— **零触碰**，只 slot
7. `web/package.json` —— 当前依赖；本 session 不引入新

## 概念定位

- Primitive (`<rm-dialog>` / `<rm-wizard>`) 与 v1.1 `<rm-inline-approval>` 同模式：清晰边界 + parent-controlled state + 无业务逻辑
- 视觉对照"大致语言一致"——颜色 token / 字体 / 卡片样式 match prototype；spacing/margin 差几 px 不算回归
- v1.1 业务组件**全部保留运行**——chat-panel 等 0 触碰，slot 进新 shell 即可
- 11 个 settings 页只做**cosmetic reskin**（卡片包一层 / 颜色变量替换硬编码）—— 业务逻辑、API 调用、表单字段 0 改

## Scope — PR breakdown

### PR 1 — Design tokens + 字体 + 原生 `<dialog>` + `<rm-wizard>` primitive

**Goal**：纯地基，不触碰业务。

子任务：

1. **`web/src/styles/tokens.css`**：从原型 `:root` 段抄（颜色 / 字体 / 间距 / 圆角 / 阴影 / transition / dark mode `@media`）
2. **字体加载** = Google Fonts CDN（Fraunces + Hanken Grotesk），`<link>` 在 `web/index.html`；必有 system fallback (`Georgia, serif` / `Helvetica, sans-serif`)
3. **`tokens.css` 在 `web/src/main.ts` 顶部 import** —— 全局 `:root` 注入，自然穿透 shadow DOM
4. **`<rm-dialog>`** (`web/src/components/dialog.ts`)：包原生 HTML5 `<dialog>` 元素；prop `title / open / closeOnBackdrop / closeOnEsc`；事件 `@close`
5. **`<rm-wizard>`** (`web/src/components/wizard.ts`)：step rail + body slot + Back/Next/Submit；prop `title / steps / currentStep / canAdvance / submitLabel`；事件 `@step-change / @submit / @close`
6. **pinned tests** (vitest)：
   - LitElement 内 `var(--rm-accent)` 真值（CSS 变量穿透 shadow）
   - dialog：open/ESC/X/backdrop 四种 close 路径
   - wizard：step rail 切换 / canAdvance 控制 Next / 最后一步 submit label
7. **Tailwind 保留** —— v1.1 现有组件继续用；v2 新组件优先 CSS variables + scoped `css\`\``

### PR 2 — Router 嵌套 + 旧扁平路径 redirect

**Goal**：路由从 v1.1 扁平改成 v2 三组，旧 URL bookmark 不破。

子任务：

1. `web/src/router.ts` 加 nested 路由匹配：
   - `#/` → 新 chat shell（PR 3）
   - `#/manage/*` → 新 settings shell（PR 4）
   - `#/activity/*` → v2-C 占位（用 `<rm-coming-soon>` 占住）
2. **旧扁平路径 redirect**（用 `location.replace`）：
   - `#/coworkers` → `#/manage/coworkers`
   - `#/mcp-servers` → `#/manage/mcp-servers`
   - `#/models` → `#/manage/models`
   - `#/credentials` → `#/manage/credentials`
   - `#/skills` → `#/manage/skills`
   - `#/approvals` → `#/manage/approval-policies`
   - `#/admin/safety/rules` → `#/manage/safety` （rules 与 decisions 在 v2 不在同一壳）
   - `#/admin/safety/decisions` → `#/activity/safety-decisions`
3. **测试**：navigate 旧路径 → location.replace 触发 → 新路径生效

### PR 3 — `<rm-chat-shell>` + 顶栏 3 图标 + coworker 切换器

**Goal**：`#/` 路径下渲染新 chat shell；功能等价于 v1.1 chat。

子任务：

1. **`web/src/components/chat-shell.ts`**：
   - 左 sidebar (240px)：logo / 当前 coworker 卡片 (含切换 button) / "+ New chat" / 搜索框 / 历史会话列表（Today / Yesterday / Earlier 分组）/ 底部 user pill (menu: Settings / Log out)
   - 中间 main：直接渲染 v1.1 `<rm-chat-panel>`，所有 props 透传
   - 顶栏右：Activity icon (脉冲 svg) / Approvals icon (checkmark + badge) / Settings icon (gear) / tenant pill (`acme-corp · prod`)
   - 视觉对照原型 `.shell` / `.sb` / `.main` / `.tbar` class
2. **Coworker 切换器** popover：点 sidebar 顶部当前 coworker 卡片 → 弹列表 (`GET /api/v1/coworkers`) + 每行 click 切换 + 底部 "Manage coworkers…" link (`#/manage/coworkers`)
3. **顶栏 3 图标动作**：
   - Activity → `router push #/activity` (v2-C 之前用 `<rm-coming-soon>`)
   - Approvals → 点开 placeholder popover (v2-C 真做实时；本 session 显示 "Coming v2-C" + badge hardcode 0)
   - Settings → `router push #/manage/coworkers`
4. **tenant pill**：`GET /api/v1/me` 拿 tenant 名 + hardcode `prod`（v3 加 backend env field）
5. **历史会话分组**：按 `messages.created_at` 用 user local timezone 分 Today / Yesterday / Earlier
6. **抽 svg icons 到 `web/src/components/icons.ts`** 给后续 session 复用
7. **pinned tests**：
   - 顶栏 3 icon click 各跳对路由
   - coworker 切换器 popover 渲染列表
   - 切 conversation chat-panel 收到新 conv id
   - 底部 user pill 弹 menu

### PR 4 — `<rm-settings-shell>` + 11 页 slot + sidebar 分组

**Goal**：`#/manage/*` 路径下渲染新 settings shell；11 个内嵌页全部 v1.1 现有组件 slot 进去 + cosmetic reskin。

子任务：

1. **`web/src/components/settings-shell.ts`**：
   - 左 sidebar：分组导航
     ```
     Coworkers                ← 置顶
     BUILDING BLOCKS
       · MCP servers
       · Skills
       · Models
       · Credentials
     GOVERNANCE
       · Safety rules
       · Approval policies
     WORKSPACE
       · General  ← 新建 placeholder
       · Members  ← 新建 placeholder
     ACCOUNT
       · Appearance  ← 新建 placeholder (但要做：system theme 自动检测)
     ```
   - 右侧 main：渲染当前选中的 page
   - 顶栏可选：返 chat 按钮（X 在右上）
2. **11 页 slot**：每页都是 v1.1 现有组件 + 一层 padding/卡片样式 wrapper：
   - `#/manage/coworkers` → `<rm-coworkers-page>` (v1.1 03b)
   - `#/manage/mcp-servers` → `<rm-mcp-servers-page>` (v1.1 02a)
   - `#/manage/skills` → `<rm-skills-page>` (v1.1 03b)
   - `#/manage/models` → `<rm-models-page>` (v1.1 02a)
   - `#/manage/credentials` → `<rm-credentials-page>` (v1.1 02a)
   - `#/manage/safety` → `<rm-safety-rules-page>` (v1.1 04，已在 v2 admin allowlist 内)
   - `#/manage/approval-policies` → 复用 v1.1 现有的 approvals page placeholder OR `<rm-coming-soon>`（v1.1 没真做这页）
   - `#/manage/general` → 新建 `<rm-coming-soon label="General" phase="v3">`
   - `#/manage/members` → 同上 placeholder
   - `#/manage/appearance` → 真做（system theme card + dark mode 跟随系统 readonly display）
3. **Reskin** = 每页统一卡片样式（cream 背景 + 微 border + hover 出现编辑按钮 / arrow）—— 业务逻辑、表单字段 0 改
4. **删 v1.1 `<rm-app-shell>` 在 `#/` 的渲染**（被新 chat shell 替代）；`<rm-app-shell>` 类**整个删除**（settings 用新 shell 替代，没有遗留 caller）
5. **pinned tests**：
   - settings sidebar entry click 各跳对 page
   - 当前选中 entry 高亮
   - 11 页都能渲染不抛
6. **lint**：新加 `web/scripts/lint-flat-route.mjs` 检查前端无对扁平 hash 的硬链接

### PR 5 (可选) — 收尾 + 文档约定

如果 PR 1-4 跑完还有时间：

- 新建 `docs/webui-ui-redesign-v2-conventions.md`：light DOM vs shadow DOM 选择 / Tailwind vs CSS-in-Lit / 字体加载 / token 命名
- v1.1 `<rm-app-shell>` 文件正式 `git rm`（PR 4 已 unwire；本 PR 删文件）
- pre-existing TS errors in `credentials-page.ts` / `mcp-servers-page.ts`（v1.1 03a 提的）—— 顺手修

## Acceptance criteria

- [ ] `web/src/styles/tokens.css` 存在 + 所有原型变量；字体 Google Fonts 加载工作（system fallback 也工作）
- [ ] `<rm-dialog>` + `<rm-wizard>` 单测全绿
- [ ] Router 嵌套化 + 8 个旧扁平路径 redirect 工作
- [ ] `<rm-chat-shell>` 渲染：coworker 切换器 / chat-panel slot / 顶栏 3 图标 / tenant pill
- [ ] `<rm-settings-shell>` 渲染：分组 sidebar + 11 页全部能进去
- [ ] **v1.1 chat 行为不退化**：浏览器开 web → 发消息 / token stream / Stop / Cancel / reconnect 全工作（颜色变了但功能完整）
- [ ] **v1.1 各 settings page 内交互不退化**：safety rules 还能看；approvals page 还能进；MCP server 还能 CRUD；coworker 还能创建（用 v1.1 现有创建 UI——wizard 是 v2-B）
- [ ] dark mode 跟系统切换 colors 变
- [ ] `npm test` + `npm run build` 全绿
- [ ] OpenAPI codegen freshness check 仍绿
- [ ] 手动 smoke：完整 chat 流程 + 顶栏 3 图标点 + Settings 11 页 navigate + dark mode 切
- [ ] 更新 `docs/webui-ui-redesign-v2-plan.md` 状态表

## Out of scope（明确不做）

- ❌ **Coworker wizard 实际内容**（v2-B；本 session "+ New coworker" link 跳现有 v1.1 创建 UI）
- ❌ **Coworker 详情编辑页 wizard**（v2-B；本 session 用 v1.1 现有）
- ❌ **Models page provider grouping + credential 交叉**（v2-B 落 helper；本 session 用 v1.1 现有）
- ❌ **Activity 真内容**（v2-C；本 session `#/activity` 用 `<rm-coming-soon>` 占位）
- ❌ **Approvals popover 真实时**（v2-C；本 session 占位 popover + badge 0）
- ❌ **Safety rules 完整编辑器**（locked decision #7，永远只做 read-only list；本 session reskin 即可）
- ❌ **Credential per-provider extras**（v2-B；本 session 用 v1.1 现有单 api_key 表单）
- ❌ **删除 v1.1 任何业务组件**（chat-panel / safety pages / approvals page 等 0 触碰）
- ❌ **新依赖引入**（state mgmt / animation / form lib / 编辑器库）
- ❌ **重写现有 Tailwind 用法**（v2-C polish 才统一）

## Open questions

锁定（plan.md locked decisions 已涵盖）：

1. ~~字体加载~~ → Google Fonts CDN
2. ~~`<rm-dialog>` 实现~~ → 原生 HTML5 `<dialog>`
3. ~~tokens.css 注入~~ → main.ts 全局 import + CSS variables 穿透
4. ~~Tailwind 兼容~~ → 保留 v1.1 用法，新 v2 组件优先 CSS variables
5. ~~视觉对照严格度~~ → 大致语言一致（颜色 / 字体 / 卡片样式），不做 pixel-perfect
6. ~~11 个 page 何时全 reskin~~ → 本 session 全部完成（cosmetic only，业务 0 改）

仍需 session 内决策：

1. **顶栏 Approvals badge 占位值**：hardcode 0 vs 调 GET 一次（无 WS）—— 推荐前者（v2-C 一起做实时，避免双实现）
2. **历史会话分组时区**：user local 还是 server tz —— 推荐 user local (`new Date().toLocaleDateString()`)
3. **`<rm-settings-shell>` 顶栏返 chat button** 位置：右上 X 还是左上 < 箭头 —— 看原型实际
4. **"Manage coworkers…" 在 chat shell 切换器**：popover 内的 link，还是单独的 button —— 看原型

## Pitfalls

- **chat-panel 内部 0 触碰** —— v1.1 真业务 smoke 过；任何"顺便重构"冲动 reject
- **CSS 变量必须穿透 shadow boundary** —— `:host { color: var(--rm-ink-primary) }`，不要硬编码颜色字面量
- **`<dialog>` backdrop 是 `::backdrop` 伪元素** —— 不要 element 上加 backdrop div
- **Router redirect 用 `location.replace`** 不要 `location.assign`（避免 browser back button 卡在旧路径）
- **`<rm-wizard>` draft state 由父组件管** —— primitive 0 内部 state；这是与 v1.1 inline-approval 同模式
- **不要做 portal / teleport 模拟** —— 原生 `<dialog>` 浏览器免费处理 stacking
- **11 页 reskin 只动样式 wrapper** —— 业务组件内部任何 logic / API call / 表单 schema 触碰都是 reject
- **chat shell 历史会话列表数据源** —— `GET /api/v1/coworkers/{id}/conversations`；与 v1.1 chat-panel 内的 sub-sidebar 共享数据；可能需要 lift state 到 shell（如果 chat-panel 内已 fetch，复用结果而不双 fetch）
- **顶栏 3 icon svg** 抽 `icons.ts` —— v2-B / v2-C 都会复用
- **`<rm-coming-soon>` 占位** 沿用 v1.1 00c 落的（不要新做）
- **dark mode test** 手动跑 system 切换；自动测试覆盖不到这条
- **新 v2 组件别用 Tailwind** —— 走 tokens.css；保持 v2 一致

## 执行前刷新清单

- [ ] feat/ui-v2 分支干净（git status）
- [ ] v1.1 chat 流程在 main 上还能跑（手动开 web 验证）
- [ ] `web/package.json` 没有新增依赖（本 session 不引）
- [ ] prototype HTML 视觉对照已在浏览器打开（参照用）

## Findings (after execution)

_(empty — 重点记录：字体加载实际表现 / Lit shadow DOM 与 Tailwind 4 摩擦细节 / `<dialog>` 原生元素是否有浏览器兼容问题 / 11 页 reskin 实际改了什么 / 旧 `<rm-app-shell>` 删除影响 / 对 v2-B 的影响（特别是 Models page provider grouping 是否要在本 session 抽 helper））_
