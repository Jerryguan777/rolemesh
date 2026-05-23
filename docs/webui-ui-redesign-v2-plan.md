# WebUI UI Redesign v2 — 实施计划

> 设计来源：[`docs/webui-ui-redesign-v2-design.md`](./webui-ui-redesign-v2-design.md)
> 视觉对照原型：[`docs/webui-ui-redesign-v2-prototype.html`](./webui-ui-redesign-v2-prototype.html)
> Session prompt 目录：[`docs/webui-ui-redesign-v2-sessions/`](./webui-ui-redesign-v2-sessions/)
> 工作分支：`feat/ui-v2`（v1.1 已合 main，PR #30）

## 出发点

v1.1 完工后 chat + 配置 + approvals + skills + safety 都打通，但 UI 仍是**扁平 9 项 sidebar**——chat、coworker 配置、零件管理、治理、observe 全平铺在一个层级。v2 重新设计 IA：把 **operate / configure / observe** 分成三个独立壳，coworker 提升为一等公民（成品），其它都是装配它的零件。

v1.1 是后端 + API + 协议骨架，v2 是**纯前端重构 + UX 重设计**——不动 schema、不加 endpoint、不变协议。

## Sources of truth（与 v1.1 一致的双轨）

- **数据契约**：`web/openapi.yaml` + `web/src/api/generated/types.ts` + `src/webui/schemas_v1.py`。冲突时以契约为准
- **样式 + 交互**：[`webui-ui-redesign-v2-prototype.html`](./webui-ui-redesign-v2-prototype.html)（Studio minimal 视觉语言、布局、文案）
- 设计文档 §8 列了原型 ↔ 契约的对照修正

## Locked decisions（执行前必读）

用户已确认的范围决策（避免每个下游 session 重新讨论）：

| # | 决策 | 锁定值 | 影响 |
|---|---|---|---|
| 1 | 工作模式 | v2 新 cycle（独立分支 `feat/ui-v2`） | 不在 feat/ui 上累；v1.1 已合 main |
| 2 | 严格度 | **dev 标准**（greenfield + 反 over-engineering 一致应用） | 与 v1.1 同款思维：no caller = 不做，"production-grade" 留 v3 |
| 3 | Activity Runs 页 | **不做** | Activity 只 Safety decisions + Approval log 两 tab；跨 conv run 列表留 v3 |
| 4 | Coworker `folder` 字段 | **自动派生**（`name` → kebab-slug）| 向导第 1 步不暴露；advanced 区可改 |
| 5 | Settings shell 命名 | **"Settings"**（spec 默认）| 不改成 Manage / Workspace |
| 6 | 视觉对照严格度 | **大致语言一致** | tokens / 字体 / 卡片样式 match；不做 pixel-perfect playwright diff |
| 7 | Safety rules 编辑器 | **基础 list + read-only 详情** | 完整 check-driven 编辑器（5 verdict + condition_expr）留 v3 |
| 8 | Approval policy 编辑器 | **基础 create/edit**（mcp_server_name + tool_name + approver_user_ids），`condition_expr` raw JSON textarea | 完整可视编辑器留 v3 |
| 9 | MCP 绑定粒度 | **整服务器**（`enabled_tools=null`）| per-tool 白名单 UI 留 v3 |
| 10 | `agent_role` 暴露 | **不暴露**（默认 `agent`）| super_agent A2A 编排是进阶概念 |
| 11 | Channel 在会话列表 | **不区分** | v1 全默认 web channel |
| 12 | `Bindings` 独立路由 | **废弃**（current placeholder） | 绑定收进 coworker 详情页 |
| 13 | `max_concurrent` 暴露 | **不暴露**（默认 2）| 同 agent_role |

## Phase + Session 断点

| Session | 内容 | 估算 LOC |
|---|---|---|
| v2-00 | 基建（design tokens / dialog/wizard primitive / router restructure / Lit shadow DOM 策略） | ~600 |
| v2-01 | Chat 主壳（coworker 切换器 + 顶栏 3 图标 + tenant pill） | ~700 |
| v2-02 | Settings shell（sidebar + 7 个 block/governance/workspace 内嵌页 reskin） | ~800 |
| v2-03 | Coworker wizard（6 步 + Models 按 provider 分组 + Credential per-provider extras） | ~1200 |
| v2-04 | Activity shell（Safety decisions + Approval log；**无 Runs**） | ~500 |
| v2-05 | 顶栏 Approvals popover（WS event 接现有 v1_client） | ~400 |
| v2-06 | 视觉 polish + 各页 playwright 截图对照 | ~300 |
| **合计** | | **~4500** |

## 执行顺序与依赖

```
v2-00 (foundations)
    v
v2-01 (chat shell)  -------+
    v                      |
v2-02 (settings shell)     |  v2-04/05 可与 v2-02/03 并行
    v                      |  但 single-dev 串行更简单
v2-03 (coworker wizard) <--+
    v
v2-04 (activity shell)
    v
v2-05 (approvals popover)
    v
v2-06 (visual polish)
```

## Session 状态跟踪

| Session | 标题 | 状态 | 完成日期 | 备注 |
|---|---|---|---|---|
| v2-00 | Foundations: tokens + primitives + router | not started | — | 详细 prompt 已写 |
| v2-01 | Chat shell with coworker switcher | not started | — | 详细 prompt 已写 |
| v2-02 | Settings shell + 7 block pages reskin | not started — DRAFT | — | 执行前 refresh |
| v2-03 | Coworker wizard + Models + Credential extras | not started — DRAFT | — | 执行前 refresh |
| v2-04 | Activity shell (sans Runs) | not started — DRAFT | — | 执行前 refresh |
| v2-05 | Top bar Approvals popover | not started — DRAFT | — | 执行前 refresh |
| v2-06 | Visual polish + playwright comparison | not started — DRAFT | — | 执行前 refresh |

## 如何执行一个 session

1. 开**新的 Claude Code session**（不复用前一个）
2. 输入：
   ```
   请读取 docs/webui-ui-redesign-v2-sessions/<session-id>.md，按描述完成所有 PR。
   完成后跑 Acceptance criteria + 更新 plan.md 状态 + 写 Findings + git push origin feat/ui-v2。
   ```
3. session 结束后：
   - 把 plan 状态改 `done` + 日期
   - 写 Findings（v1.1 这步特别有价值，retro 复用率高）
   - 下游 session prompt 可能需 refresh

## 跨 session 工作约定（继承 v1.1）

- 不开子 PR，所有 commit 直接累在 `feat/ui-v2`
- 每个"PR N"对应一个独立 commit（或几个紧耦合 commit 一组）
- session 结束时一次性 `git push origin feat/ui-v2`
- 每个 commit 用 `git commit -s`
- 代码 / 注释 / 文档字符串一律英文
- 测试遵循 CLAUDE.md "测试理念"
- v1.1 的 INV-* 全部继续守（特别 INV-6 / INV-7 / INV-VAULT-* 这些 invariant test 不能因为 UI 重构而退化）

## v1.1 学到的应用规则

参 `docs/webui-backend-v1.1-sessions/04-safety-ui.md` 末尾 retro。直接复用的几条：

1. **Read the design before estimating, read the code before the session.** 每个 session 第一件事是 grep 现有组件 + 看 v1.1 落地的接口
2. **Greenfield over compat-window**: v2 是新 IA，不需要"双 UI 共存"过渡——一刀切到新 shell（旧组件复用但挂到新 shell 下）
3. **Locked decision matters more than clever decision**: 上面 13 条 locked decisions 不再 re-litigate
4. **Test the wire, not the helper**: v2 的端到端测试用 playwright 真渲染 + 真 click，不 mock 组件
5. **Refreshes are cheap; rewrites are not**: v2-02 到 v2-06 是 DRAFT；每次执行前先看上游 Findings + grep
6. **One session = one Plan row = one commit train**: 7 sessions 各自独立 + 每个 session 结束 push

## 反 over-engineering 警惕（v2 阶段适用）

| 容易出现的 over-engineering | 应该 cut |
|---|---|
| State management 库（Redux / Pinia / Zustand）| `@state` + window CustomEvent 够用 |
| Storybook / Chromatic 视觉回归 | playwright 截图 + diff 就够 |
| 组件抽象 base class / mixin | Lit element 各自完整即可，DRY 留到第 3 个相似组件再抽 |
| Form 验证库（Vest / Formik 类） | HTML5 + 自写 validator 函数 |
| Date picker / dropdown / autocomplete 组件库 | 设计 spec 不要求；用原生 `<input>` 即可 |
| i18n 框架 | v2 全英文 hardcode |
| 测试 utility 库（Testing Library 类） | vitest + happy-dom 直接选 DOM 即可 |
| Animation 库（Framer Motion 类）| CSS transition + transform 够 |

## 参考文档

1. [`webui-ui-redesign-v2-design.md`](./webui-ui-redesign-v2-design.md) — 设计源
2. [`webui-ui-redesign-v2-prototype.html`](./webui-ui-redesign-v2-prototype.html) — 视觉对照
3. [`webui-backend-v1.1-design.md`](./webui-backend-v1.1-design.md) — v1.1 后端设计（INV-* 来源）
4. [`webui-backend-v1.1-sessions/04-safety-ui.md`](./webui-backend-v1.1-sessions/04-safety-ui.md) — v1.1 retro（执行经验）
5. `web/openapi.yaml` + `web/src/api/generated/types.ts` — 契约
6. [`CLAUDE.md`](../CLAUDE.md) — 用户偏好
