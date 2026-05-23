# Session v2-01 — Chat shell with coworker switcher

| field | value |
|---|---|
| Phase | v2 cycle |
| Prerequisites | v2-00 done（tokens + primitives + router 就位） |
| Estimated PRs | 3 |
| Estimated LOC | ~700 |
| Status | not started |

## Goal

把 v1.1 落的 `<rm-app-shell>` 改成专用 **chat 主壳**——左侧 coworker 切换器（替代 v1.1 sidebar）+ 中间 chat panel + 顶栏 3 图标（Activity / Approvals / Settings）+ 右上 tenant pill。

v1.1 是"chat 是 sidebar 9 项之一"，v2 是"chat 是默认壳，配置/observe 都在另一处"。这次重构改 IA + 视觉，但**chat-panel 内部完全不动**（已经在 01c 真 smoke 验证过）。

## Required reading

1. [`docs/webui-ui-redesign-v2-design.md`](../webui-ui-redesign-v2-design.md) §2.1（顶栏）/ §2.2（一致性规则）/ §3（chat 外壳 vs 管理外壳分工）
2. [`docs/webui-ui-redesign-v2-prototype.html`](../webui-ui-redesign-v2-prototype.html) —— chat 主壳布局（默认视图，主 `<body>` 的前半段）
3. v2-00 Findings（design tokens / dialog primitive / router 实际形态）
4. v1.1 `web/src/components/chat-panel.ts` —— **不要改它**，只 wrap
5. v1.1 `web/src/services/agent-client.ts` —— Stop 按钮的旧 WS 路径（**保留**，与 01c 决策一致）
6. v1.1 `web/src/ws/v1_client.ts` —— Cancel 等 v1 WS

## Scope — PR breakdown

### PR 1 — Chat 主壳基础

**Goal**：新 `<rm-chat-shell>` 替代旧 `<rm-app-shell>` 在 `#/` 路由下渲染。

子任务：

1. 新建 `web/src/components/chat-shell.ts`：
   - 左侧 sidebar（240px 固定）：logo / 当前 coworker 卡片（带切换器 button）/ "+ New chat" / 搜索框 / 历史会话列表（按 Today / Yesterday / Earlier 分组）/ 底部 user pill（弹 menu: Settings + Log out）
   - 中间 main：渲染 v1.1 `<rm-chat-panel>`（直接 import + 透传 props）
   - 顶栏右：3 个 icon button (Activity / Approvals / Settings) + tenant pill
   - 视觉对照原型 `.shell` / `.sb`（sidebar）/ `.main` / `.tbar`（top bar）class
2. **Coworker 切换器**——左 sidebar 顶部当前 coworker 卡片点击 → 弹一个小 popover 列出 tenant 内全部 coworker，每行有 "Open" + 底部 "Manage coworkers…"（→ `#/manage/coworkers`）
3. **顶栏 tenant pill** —— 用 `GET /api/v1/me` 拿当前 user.tenant 名 + 环境（暂时 hardcode `prod`，未来从 backend 拿）
4. **保留 v1.1 dark mode** —— tokens.css 已含 dark；不加 toggle

**pinned tests** (`web/src/components/chat-shell.test.ts`)：

- 切 conversation 时 chat-panel 收到新 conv id
- 点切换器 button 弹出 coworker 列表
- 点顶栏 Settings icon → router 跳 `#/manage/coworkers`（v2-02 实现页面前 fallback coming-soon 即可）
- 点底部 user pill 弹 menu

### PR 2 — 顶栏 3 图标动作 + 主壳路由挂载

**Goal**：让顶栏 3 图标都跳到正确 destination；v2-04 / v2-05 真正实现内容时只需要 swap 占位。

子任务：

1. **Activity icon (脉冲 svg)** → router push `#/activity` → v2-04 占位（coming-soon "Phase v2-04"）
2. **Approvals icon (checkmark svg + badge)** → 点开 popover（v2-05 实现），本 session 用 placeholder popover 显示 "Coming v2-05"
   - Badge 数字暂用 hardcode 0；v2-05 改成实时
3. **Settings icon (gear svg)** → router push `#/manage/coworkers`（v2-02 实现 Settings shell 时已存在 placeholder）
4. **Router 挂载** `web/src/router.ts` 把 `#/` 路径绑到 `<rm-chat-shell>`；旧 `<rm-app-shell>` 仍然为 `#/manage/*` 兜底（v2-02 才换成 Settings shell）

**pinned tests**：

- 顶栏 3 个 icon click 各自跳对路由
- Badge 占位渲染（v2-05 之前用 0 即可，但 DOM 结构对）

### PR 3 — v1.1 现有 sidebar 兼容收尾

**Goal**：v1.1 sidebar 9 项扁平菜单从 chat 主壳消失（被 coworker 切换器 + 顶栏 3 图标 + Settings shell 替代）；但访问 `#/manage/*` 仍能进每个旧页面（v2-02 才真换肤）。

子任务：

1. 删除 v1.1 `<rm-app-shell>` 在 `#/` 路径的渲染——`#/` 走新 chat shell
2. **`<rm-app-shell>` 留在 `#/manage/*` 路径**作为占位 shell（v2-02 替换）；它现有的 sidebar 收窄到只显示 "Coworkers / MCP / Models / Skills / Credentials / Safety rules / Approval policies / General / Members / Appearance"——这一步只重排顺序，不重设计
3. **`<rm-coming-soon>` 沿用**（00c 落的）—— 顶栏 Activity / Approvals popover 都用这个占位
4. **跑一次完整 chat e2e**确认不退化：登录 → 发消息 → token stream → 切 conversation → 点顶栏 3 图标 → 占位页正常

## Acceptance criteria

- [ ] `<rm-chat-shell>` 渲染：coworker 切换器 / chat-panel / 顶栏 3 图标 / tenant pill
- [ ] `#/` 默认进 chat 主壳，不是旧 app-shell
- [ ] 顶栏 3 图标 click 跳对路由 / 弹对 popover
- [ ] Coworker 切换器 popover 列出当前 tenant 全部 coworker
- [ ] **v1.1 chat 行为不退化**：发消息 / Stop / Cancel / token stream / reconnect 全工作
- [ ] dark mode 跟系统切换
- [ ] `npm test` + `npm run build` 全绿
- [ ] 手动 smoke：完整 chat 流程跑通；点顶栏 3 图标都有反应
- [ ] 更新 plan.md 状态

## Out of scope

- ❌ Settings shell 真内容（v2-02）
- ❌ Coworker wizard（v2-03；本 session "Manage coworkers…" link 跳现有 v1.1 coworkers page）
- ❌ Activity 真内容（v2-04）
- ❌ Approvals popover 真内容（v2-05；本 session 占位）
- ❌ 删 v1.1 任何业务组件（chat-panel / safety pages 等都保留）
- ❌ Tailwind → CSS-in-Lit 重写（v2-06 polish 阶段才统一）

## Open questions

仍需 session 内决策：

1. **`<rm-chat-shell>` vs `<rm-app-shell>` 命名**：保留 `<rm-app-shell>` 作为新 chat shell + 旧 shell 改名 `<rm-legacy-shell>`？还是新建 `<rm-chat-shell>` + 旧的留原名？推荐后者（语义更准；v2-02 落 `<rm-settings-shell>` 时也对齐）
2. **顶栏 Approvals badge 数字**：v2-05 才实时；本 session hardcode 0 还是从 `GET /api/v1/approvals?status=pending` 取一次（无 WS 实时）？推荐前者（v2-05 一起做实时，避免双实现）
3. **Coworker 切换器要不要 search filter**：tenant 大时 coworker 多——推荐 `<= 10 个 coworker` 不加 search；超过加。session 内观察决定
4. **tenant pill 环境字段**：原型显示 `acme-corp · prod`；`prod` 字段后端 `/api/v1/me` 不返。临时 hardcode `prod` 还是从 env var 注入？推荐前者（v3 加 backend field）

## Pitfalls

- **chat-panel 内部 0 触碰** —— 任何想"顺便重构 chat-panel"的冲动都 reject；它是 v1.1 真业务 smoke 过的组件
- **coworker 切换器不是 router 替代** —— 切换 coworker = 跳到那个 coworker 的最新 conversation；通过 router push 实现（`#/?coworker=<id>` 或 `#/coworkers/<id>/chat`）
- **顶栏 svg icon 不要内联** —— 抽到 `web/src/components/icons.ts` 让 v2-02+ 复用
- **左 sidebar 历史会话分组 (Today / Yesterday / Earlier)** —— 用 `messages.created_at` 比较，注意时区（用 user's local timezone，不是 UTC）
- **dark mode 测试**：手动跑 system → dark 切换，确认 tokens.css `@media` 生效
- **`<rm-chat-shell>` 别用 Tailwind 工具类** —— 走 v2-00 落的 CSS variables；保持 v2 新组件的 shadow DOM 一致性

## 执行前刷新清单

- [ ] v2-00 完成？tokens.css / dialog / wizard / 嵌套 router 全就位？
- [ ] v2-00 Findings 段读完，特别看 CSS shadow DOM 透传策略 + 字体加载真选项
- [ ] 当前 web/ 端 vitest 全绿

## Findings (after execution)

_(empty — 重点记录：`<rm-chat-shell>` vs `<rm-app-shell>` 最终命名 / coworker 切换器 UI 细节 / 历史会话分组时区处理 / 顶栏 icon 复用模式 / 对 v2-02 (Settings shell) 的影响)_
