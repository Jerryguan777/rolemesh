# Session 01c — 前端 chat 接入新 WS 协议

| field | value |
|---|---|
| Phase | 1 |
| Prerequisites | 01a + 01b done；建议 01b 末尾的 e2e smoke 在 backend 实跑通过后再开这个 session |
| Estimated PRs | 2-3 |
| Estimated LOC | ~1000 |
| Status | not started |

## Goal

把现有 chat 前端从 `/api/admin/*` + 旧 WS 切到 `/api/v1/*` + 新 WS 协议，行为不退化。这一步完成后 Phase 1 主路径全部走完，可以 e2e smoke。

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §3 / §4 / §6.3 J（reauth banner）
2. 01a / 01b Findings —— ws-ticket 绑定方式、idempotency_key 策略、awaiting_reauth 语义
3. `web/src/` 现有 chat-panel 组件 + ws client
4. 00c 落下的 `<rm-app-shell>` 和 `web/src/api/generated/types.ts`
5. `web/openapi.yaml` —— 拿 typed client

## Scope — PR breakdown

### PR 1 — 新 WS client（事件总线 + 重连先 GET truth）

- 新建 `web/src/ws/client.ts`：
  - 事件总线模式：`onEvent("event.run.token", (e) => ...)`
  - 连接前先调 `POST /api/v1/auth/ws-ticket` 拿 ticket
  - 重连策略：
    - 断开后先 `GET /api/v1/runs/{id}` 拿真值
    - run.status === 'completed' → 不重连
    - run.status === 'running' → 重连 + 从重连时刻订阅（不 replay 历史 token）
  - 自动用 `idempotency_key`（uuid4 per send）防 reconnect 重投
- 删除现有旧 ws client（不要并存）
- 单测：用 mock WS 测重连先 GET 逻辑
- 手动测：chat 一次正常对话；中途断网恢复，验不重发不丢

### PR 2 — chat-panel 切到 v1 endpoints + 全局 reauth banner

- chat-panel 内所有 fetch URL 切 `/api/v1/coworkers/{id}/conversations` 等
- 用 typed client（00c 的 `web/src/api/client.ts`），**禁止任何手写 URL 字面量**——CI 加一个 lint 扫描 `/api/admin/` 字面量在 v1 前端代码里不允许出现
- 全局 reauth banner（设计 §6.3 J）：
  - `<rm-reauth-banner>` 组件挂在 `<rm-app-shell>` 顶部
  - 监听 ws event bus 的 `event.run.requires_reauth` → 显示横幅 + 提供"Re-login"按钮（按钮在 bootstrap 模式下走"重新 input bootstrap token"模拟，给 dev 调试用）
  - bootstrap fast-path 不会真触发这个 event；banner 代码必须存在但不暴露入口（除非 dev 模式 force trigger）

### PR 3 — Coworkers 列表占位 + chat 入口集成

- 把 sidebar 的 "Coworkers" 项链到一个**最简列表页**：调 `GET /api/v1/coworkers` 显示列表 + "Start chat" 按钮跳到 chat
- 创建向导留 02a 详细做；本 session 只让 chat 有入口
- 现有 chat 直接进入功能保留（hash router 默认 `#/`）
- 手动测：sidebar → Coworkers → 列表显示 → 点 coworker → 进 chat → 收发消息

## Acceptance criteria（session 级）

- [ ] 前端 `web/src/` 内**无** `/api/admin/` URL 字面量（lint 验证）
- [ ] chat 全流程不退化：发消息 / token streaming / 中断重连 / 切 conversation
- [ ] 重连后不重发 + 不漏数据（手动 smoke）
- [ ] Coworkers 列表能从 sidebar 进入；选一个进 chat 能用
- [ ] reauth banner 组件存在且 dev 模式能强制触发显示
- [ ] 整套前端单测 + e2e（如有）通过
- [ ] **Phase 1 完整 smoke**（设计 §10 Phase 1 清单）：
  - 真 Anthropic key 创建 coworker（API）→ web 发消息（bootstrap as alice）→ token stream 显示 → run.completed → DB 里 runs 表 status/completed_at/usage 都写了
  - 切 BOOTSTRAP_USERS 不同 token → user 身份变了（GET /api/v1/me 显示不同）
- [ ] 更新 plan 状态

## Out of scope

- ❌ Coworker 创建向导完整 UI —— 留 02a
- ❌ 详情页 tabs (overview/skills/mcp/bindings/...) —— Phase 2+
- ❌ Approvals / Skills / Credentials 页面 —— 留对应 Phase

## Open questions

1. **idempotency_key 是 client 永久持久化还是 in-memory only**？in-memory 简单但页面刷新就丢；持久化 (sessionStorage) 更鲁棒。**推荐 in-memory** —— 页面刷新走重连先 GET truth 逻辑，已经覆盖
2. **reauth banner 在 dev 模式怎么触发**：URL query `?reauth=1` / console 命令 / 隐藏 dev menu？建议简单的 console 命令（`window.__forceReauth()`）
3. **`/api/admin/` 切换是一刀切还是渐进**？01c 一次切完 chat 路径，但 admin 路径（用户管理等）继续用 admin endpoint。Phase 2-4 再各自迁
4. **Lit 还是其它框架**：确认现有 `web/` 用的是 Lit + Tailwind（设计写了），如果是其它框架本 prompt 的组件示例要调整

## Pitfalls

- 删除旧 ws client 前确认**没有任何调用方残留**——`grep -r "old-ws-client-name" web/`
- 重连先 GET truth 的逻辑必须真生效——容易写成"reconnect 就直接订阅"，那 INV-6 端到端就废
- typed client 的 error response 类型不能丢——`ErrorResponse` schema 错误必须在 TS 类型上能 narrow
- chat-panel 内部状态（messages array）在切 conversation 时必须重置——容易漏，导致两个对话内容串
- 全局 reauth banner 不要每次 ws disconnect 就显示——只在收到 `event.run.requires_reauth` 时显示

## Findings (after execution)

_(empty — 重点记录：现有前端框架确认、admin → v1 切换是否真一刀切、idempotency 选择)_
