# Session v2-C — Activity shell + Approvals popover + visual polish  `[REFRESHED 2026-05-23]`

| field | value |
|---|---|
| Phase | v2 cycle（最后一个 session）|
| Prerequisites | v2-A done + v2-B done + 用户 smoke 通过两者 |
| Estimated PRs | 3-4 |
| Estimated LOC | ~1300（取保守 2x：原 600 × 2 + 含 v2 retro + 累积 polish backlog） |
| Status | not started |

> **Refresh 起源**：v2-A 已建 `<rm-activity-shell>` 最小占位 + v2-B 累积 polish backlog + 发现 `v1_client.ts ServerEvent` union 缺 approval events 类型。本 refresh 把这些都明确：
> 1. activity-shell 已存在，v2-C 只**填内容**（Activity index + Approval log tab）
> 2. v1_client.ts ServerEvent 缺 ApprovalRequiredEvent / ApprovalResolvedEvent 类型——v2-C 补
> 3. polish backlog 累计 5 项（v2-A + v2-B 各 self-flag）—— v2-C 是收尾，能修就修
> 4. v2-A 真实 LOC 系数 2.85x、v2-B 1.95x —— 估算 × 2 = 1300

## Goal

v2 最后一个 session，三件收尾：

1. **Activity shell 真内容**：在 v2-A 落的最小 `<rm-activity-shell>` 内填 Activity index（无 Runs，per locked decision #3）+ Approval log tab（已处理视图）
2. **`<rm-approvals-popover>`**：顶栏 Approvals icon 弹出实时 inbox（pending approvals）+ badge 实时数字（替换 v2-A 占位的 hardcode 0）+ 内部用 v1.1 `<rm-inline-approval>` 组件
3. **Visual polish + 累积 backlog + token lint** + **v2 整体 retro Findings**

## Required reading

1. [`docs/webui-ui-redesign-v2-design.md`](../webui-ui-redesign-v2-design.md) §2 observe 模式 / §6.3 I approval queue / §7 视觉
2. **v2-A Findings**（特别 polish backlog: 双层卡片 / chat-panel 内嵌 sidebar / approvals badge 占位）
3. **v2-B Findings**（特别 polish backlog: mcp-server-dialog 无测试 / partial-commit banner 位置）+ **Lit boolean property binding 真坑**
4. **v1.1 03a Findings**——WS event 双发布 subject pattern (`web.approval.required.{conv}` + `web.approval.resolved.conv.{conv}` + `.req.{id}`)
5. `web/src/ws/v1_client.ts` —— ServerEvent union（**缺 approval events，本 session 补**）
6. `src/webui/v1/ws_stream.py` —— ws handler forwarder（验证已 forward approval events 到 client；如未 forward 是 backend gap，记 Findings 但本 session 不修后端）
7. `web/src/components/inline-approval.ts` (v1.1 03a 落) —— popover 每行直接复用
8. `web/src/components/activity-shell.ts` (v2-A 落) —— 已有最小骨架，**扩展不重建**
9. `web/src/components/safety-decisions-page.ts` + `approvals-page.ts` (v1.1) —— Activity tab 内复用

## 概念定位

- Activity shell 是 **overlay**（设计 §2 observe ≠ configure），不是新页面替换 chat
- Popover 是 **顶栏 Approvals icon 右下方浮层**，不是新路由
- WS approval events **已在 backend 实现**（03a 落地）但 frontend 类型未暴露——v2-C 第一件事是补 TS 类型 + subscribe path
- Inline approval 复用 v1.1 `<rm-inline-approval>`，**0 触碰其内部**

## v2-A / v2-B primitive 实际可用（直接照抄）

### v1_client.ts ServerEvent 扩展（v2-C 加）

```ts
// 当前缺，本 session 加：
export interface ApprovalRequiredEvent extends ServerEventBase {
  type: 'event.approval.required';
  approval_id: string;
  run_id: string;
  summary: { tool_name: string; args: Record<string, unknown> };
}
export interface ApprovalResolvedEvent extends ServerEventBase {
  type: 'event.approval.resolved';
  approval_id: string;
  decision: 'approve' | 'deny' | 'expired' | 'cancelled';
  actor_user_id: string;
  note?: string;
}
// 加进 ServerEvent union
export type ServerEvent = 
  | RunStartedEvent | RunTokenEvent | RunCompletedEvent | RunErrorEvent | RunRequiresReauthEvent
  | ApprovalRequiredEvent | ApprovalResolvedEvent;  // ← new
```

**先验证**：跑一次手动 smoke 确认 backend ws_stream.py 真 forward 这两个 event（应该 03a 落了；如未 forward 是 backend gap，**只记 Findings 不修**——本 session 是前端 v2 polish）

### v2-A primitive 复用

- `<rm-dialog>` for popover？**否**——popover 不是 modal，是 anchored 浮层。v2-C 直接 `position: absolute` + 自己管 click-outside 关闭
- `<rm-wizard>` 用不上
- `icons.ts` 8 个 + 可能加 1-2 个（如 history-dot for activity timeline）
- tokens.css `--rm-warn` / `--rm-good` 给 approval status 用

### v2-B Lit boolean binding 教训

**任何 boolean prop 用 `.foo=${value}` 不要用 `?foo=${value}`**——后者只移除属性不写回 property，default=true 时无效。

popover open state、各种 active/selected/disabled 都注意。

## Scope — PR breakdown

### PR 1 — v1_client.ts approval events 扩展 + Activity shell 内容

**Goal**：v1_client subscribe approval events；Activity shell 替换 coming-soon 占位为真内容。

子任务：

1. **v1_client.ts ServerEvent union 扩展**（见上 TS 例）
   - 加 `ApprovalRequiredEvent` / `ApprovalResolvedEvent` interface
   - update ServerEvent union
   - 不改 subscribe API（onEvent / emit 已通用），只加类型
2. **手动验证 backend forward**：起 dev → 触发 approval（chat 进 gated tool） → 浏览器 devtools WS frames 看是否真有 `event.approval.required` 帧。**如未 forward 是 backend gap**：记 Findings，本 session 在前端 mock event 走通 popover 逻辑，但实时 path 留 backend 修
3. **`<rm-activity-shell>` 内容扩展**：
   - 新 tab 路由：`#/activity` (default index) / `#/activity/safety-decisions` / `#/activity/approvals` (新增)
   - tab bar 显示 2 个 tab（Safety decisions / Approval log）+ 顶栏右上 X 返 chat
   - Activity index (`#/activity`) → 2 卡片 link 到 sub tabs；不做 Runs（locked #3）
   - Approval log tab → wrap v1.1 `<rm-approvals-page>` filter `status=resolved`（已处理视图）
   - Safety decisions tab → wrap v1.1 `<rm-safety-decisions-page>`（v2-A 已 slot；reskin cosmetic 即可）
4. **关闭 Activity 返 chat**：右上 X → `location.href = '#/'`（沿用 v2-A reload pattern；与 wizard create completion 同款）
5. **pinned tests** (`web/src/components/activity-shell.test.ts` 扩展)：
   - 3 个 route 渲染对应内容
   - tab 切换 hash 更新
   - X 触发 `location.href = '#/'`

### PR 2 — `<rm-approvals-popover>` + 实时 badge

**Goal**：顶栏 Approvals icon 弹 popover；badge 实时数字；列表实时增删；inline decide 按钮。

子任务：

1. **新建 `web/src/components/approvals-popover.ts`**：
   - prop: `open: boolean`（父控）+ `userId?: string`
   - mount 时：
     - `GET /api/v1/approvals?status=pending&approver=me` 拿初始 list
     - subscribe v1_client `event.approval.required` (新行) + `event.approval.resolved` (移除该行)
   - 渲染：popover 浮层（右上 anchor under topbar icon）含 list + empty state + footer "View all in Activity" link
   - 每行用 v1.1 `<rm-inline-approval approval=${row}>`；inline decide button click → 走 `<rm-inline-approval>` 内部已有的 `POST /api/v1/approvals/{id}/decide` 路径
   - max 5 行 + 超过显示 "+N more in Activity log"
2. **`<rm-chat-shell>` 接入**：
   - 顶栏 Approvals icon click → toggle popover open
   - popover open 时增 click-outside listener 关闭
   - **badge 数字** = popover.items.length 实时；用 `--rm-warn-subtle` 背景 + `--rm-accent-ink` 文字（与 v1.1 03a UI 一致）
   - 替换 v2-A hardcode `approvalsBadge = 0`
3. **WS 订阅生命周期**：
   - popover unmount 时 unsubscribe；防 leak
   - chat-shell 顶栏的 **badge 永久订阅**（不依赖 popover 打开）—— 否则关闭 popover 后 badge 不更新
   - 抽 `useApprovalsBadge()` helper 或在 chat-shell 内部直接维护 count state + WS handler
4. **pinned tests** (`web/src/components/approvals-popover.test.ts`)：
   - mount fetch 初始 list
   - 收 `event.approval.required` → 加行
   - 收 `event.approval.resolved` → 移除该行
   - empty state 显示
   - 超过 5 行截断 + "View all" link
   - click-outside 关闭
5. **Boolean property binding** —— popover open / row decided 状态都用 `.foo=${}` 不用 `?foo=${}`

### PR 3 — Polish backlog + token lint + v2 retro

**Goal**：累积 5 项 polish + 防回退 lint + v2 整体 Findings retro。

子任务（按 ROI 排）：

1. **token lint script** (`web/scripts/lint-tokens-only.mjs`)：
   - grep 找硬编码颜色 hex `/#[0-9a-fA-F]{3,8}/` 在 `web/src/`（除 tokens.css 自己 + 自动生成文件）
   - grep 找 `font-family: '...'` 字面量
   - 命中输出 file:line + 总数；exit 1
   - 加进 `web/package.json` scripts `lint:tokens-only`
   - 跑一遍现有代码看 violations 数；如 < 10 顺手修；> 10 列 Findings 留独立 chore
2. **Settings page 双层卡片** (v2-A flagged)：
   - `<rm-settings-shell>` 内 `.ss-card` wrapper 移除（让 v1.1 page 直接渲染在 surface 上）—— 或保留 wrapper 改成透明背景 + 0 padding
   - 11 page 视觉 spot check：不该再有"卡中卡"
3. **mcp-server-dialog 单测** (v2-B small gap)：
   - 5 个 case：开/关、form 验证、save 调对 endpoint、close after save、close on backdrop
4. **partial-commit banner 位置** (v2-B trade-off)：
   - 从 review step 内移到 wizard 固定 banner 区（wizard primitive 已有 footer slot；加 header banner slot 或 fixed top）
   - **小心**：动 `<rm-wizard>` primitive API 是大改；不做。改 `<rm-coworker-wizard>` 父组件渲染逻辑：把 banner 放在 wizard 内顶部 `<div slot="...">` 而不是 step 内
5. **chat-panel 内嵌 sidebar overlap** (v2-A flagged)：
   - 当前 workaround: `localStorage.setItem('rm-sidebar-collapsed', 'true')`
   - polish: CSS 加 `.chat-panel-sidebar-hamburger { display: none }` 在 chat-shell 内 host 时隐藏；用 `:host-context` 或 attribute 选择器
6. **v2 整体 retro Findings**（必做）：
   - 3 session sized correctly 否
   - 实际 refresh 次数（目标 < v1.1 6 次；当前 v2-B 1 次 + v2-C 1 次 = 2 次）
   - 13 条 locked decisions 事后看正确否
   - 反 over-engineering 在 v2 cycle 应用次数（cut chat-panel 重构 / cut activity-shell 重建 / cut wizard primitive 抽象等）
   - 与 v1.1 retro 6 条 reusable lessons 对比验证
   - v2 LOC 实际系数 ~2x（v2-A 2.85x + v2-B 1.95x + v2-C 待测）—— v3 估算 baseline
   - 对未来 v3 / 新 cycle 的建议

## Acceptance criteria

- [ ] v1_client.ts ServerEvent union 扩展 + 类型导出 + vitest 钉 union 包含 approval events
- [ ] `<rm-activity-shell>` 3 个 route 全工作（index / safety-decisions / approvals）；tab 切换 + X 返 chat
- [ ] `<rm-approvals-popover>` 实时增删 + badge 实时数字（手动 smoke：起 dev → alice 触发 approval → bob 顶栏 badge +1 + popover 含该行；bob decide → badge -1 + 行消失）
- [ ] `<rm-chat-shell>` 顶栏 badge 替换 v2-A 占位的 hardcode 0
- [ ] token lint 加入 + 现有 violations 全清（或 Findings 列残留）
- [ ] Settings 双层卡片 polish 完成
- [ ] mcp-server-dialog 单测加上
- [ ] partial-commit banner 移到 wizard 顶部 banner 区
- [ ] chat-panel 内嵌 sidebar overlap 用 CSS 隐藏（不再依赖 localStorage workaround）
- [ ] v2 整体 retro 在 Findings 末尾
- [ ] vitest + build + lint:no-admin-chat + lint:flat-route + **lint:tokens-only** + openapi:check 全绿
- [ ] 手动 smoke：alice/bob 双 user 完整 approval 流程（起 dev → 用 BOOTSTRAP_USERS） → popover 实时 + Activity 已处理视图正确
- [ ] 更新 plan.md 状态 ——**v2 cycle 完工，3 session 全 done**

## Out of scope

- ❌ **修复 location.href reload trade-off**（v2-A flagged；v3 chore）
- ❌ **修后端 ws_stream.py forward gap**（如发现 backend 没真 forward approval events；本 session 只记 Findings）
- ❌ **chat-panel 内部重构**（包括 reactive URL 监听 / sub-sidebar lift state）—— v3
- ❌ **Runs activity tab**（locked #3，永远不做；index 页 2 卡片就够）
- ❌ **完整 approval policy 编辑器** / **safety rules check-driven editor**（locked #7/#8；保留 v1.1 现有 list + 基础 CRUD）
- ❌ **`<rm-wizard>` primitive API 改动**（partial-commit banner 在父组件解决，不动 primitive）
- ❌ **新依赖引入**

## Open questions

锁定（refresh 已定）：

1. **popover anchor 形式** = 顶栏 icon 下方 absolute 浮层（不用 `<rm-dialog>`；不是 modal）
2. **badge 数字订阅范围** = chat-shell 永久订阅（即使 popover 关也维持）
3. **WS event 类型扩展** = 在 v1_client.ts 加 interface + union，不在 schemas_v1 那边动后端（前端只解析现有 wire payload）
4. **partial-commit banner 移位** = `<rm-coworker-wizard>` 父组件改渲染逻辑，**不动** `<rm-wizard>` primitive
5. **chat-panel sidebar overlap** = CSS hide，不动 chat-panel 内部
6. **token lint scope** = 仅 `web/src/`，排除 generated + tokens.css

仍需 session 内决策：

1. **WS approval events 真 forward 吗**：手动 smoke 验证；如未 forward 是后端 gap，本 session 用 mock event 让 popover 逻辑端到端能跑，但实时验证留 backend chore
2. **token lint 现有 violations 数**：grep 后看；< 10 顺手修，> 10 列 Findings 单独 chore
3. **`<rm-approvals-popover>` 与 v1.1 `<rm-approvals-page>` 关系**：popover 是简化 list；page 是完整 list + 详情 + audit。popover 不替换 page，只是 "real-time inbox" UI
4. **iconChevronRight 是否补**：activity index 的 link 卡片右侧 chevron 当前没有；icons.ts 加进去给 chevron-right factory 还是用 chevronDown 旋转 -90°？推荐前者（一次加，cross-session 复用）

## Pitfalls

- **boolean property binding** — `.open=${}` / `.busy=${}` / `.disabled=${}` 用 property 不用 attribute（v2-B 教训）
- **popover click-outside listener 注意 unmount**：unsubscribe 否则 leak；用 `disconnectedCallback`
- **WS 订阅生命周期** —— popover unmount 取消订阅；chat-shell badge 订阅生命周期 = shell 生命周期
- **`<rm-inline-approval>` 0 触碰内部** —— v1.1 03a 落地组件；popover 只 import + 传 prop
- **token lint script 别太严格** —— 排除 tokens.css 自己 + types.ts (codegen) + node_modules + dist；CSS-in-JS 模板字符串内的 css.literal 也属于硬编码但难 grep，列 known-issue 不强求清空
- **`location.href` reload 沿用** —— X 返 chat / popover footer "View all" 都用 `location.href`，与 v2-A / v2-B 一致；v3 fix
- **WS approval events 后端 forward 验证** —— **session 第一件事**就跑 smoke 验证。如果后端没 forward 整 PR 2 的实时部分会变成"前端代码完整但永不触发"——这是真痛
- **`<rm-coming-soon>` 在 v2-A activity-shell 仍占位** —— PR 1 替换为真内容时确认 v1.1 coming-soon 组件本身不删（v2-B 也用）

## 执行前刷新清单

- [ ] v2-A + v2-B 完成 + 用户手动 smoke 通过两者？
- [ ] BOOTSTRAP_USERS 多 user 配置可用（alice/bob 两 token）—— smoke 必需
- [ ] backend ws_stream.py 是否 forward approval events 提前 grep 验证（前端实现 PR 2 前需要知道答案）
- [ ] v1.1 `<rm-approvals-page>` 是否支持 `?status=resolved` filter（activity log tab 需要）—— grep 看

## Findings (after execution) - v2 cycle 收尾 retro

_(empty — 重点记录：v2 整体 retro / popover WS 接通是否真发生 / polish backlog 完成度 / LOC 实际 vs 1300 / 对 v3 启动条件的建议)_
