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

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §3 / **§4 + §4.1**（含 Stop vs Cancel 两个 control surface 区分）/ §6.3 J（reauth banner）
2. 01a / 01b Findings —— ws-ticket 绑定方式、idempotency_key 策略、awaiting_reauth 语义
3. **01b Findings § "chore A: orchestrator-side cancel subscriber"** —— Stop vs Cancel UX 约束的来源
4. `web/src/` 现有 chat-panel 组件 + ws client（特别看现有 Stop 按钮调的是 `{type:"stop"}` → NATS `agent.{job_id}.interrupt`）
5. 00c 落下的 `<rm-app-shell>` 和 `web/src/api/generated/types.ts`
6. `web/openapi.yaml` —— 拿 typed client

## Scope — PR breakdown

### PR 1 — 新 WS client（事件总线 + 重连先 GET truth）

- 新建 `web/src/ws/v1_client.ts`（命名带 `v1_` 区分旧 client）：
  - 事件总线模式：`onEvent("event.run.token", (e) => ...)`
  - 连接前先调 `POST /api/v1/auth/ws-ticket` 拿 ticket
  - 重连策略：
    - 断开后先 `GET /api/v1/runs/{id}` 拿真值
    - run.status === 'completed' → 不重连
    - run.status === 'running' → 重连 + 从重连时刻订阅（不 replay 历史 token）
  - 自动用 `idempotency_key`（uuid4 per send）防 reconnect 重投
- **保留**现有旧 AgentClient / `/ws/chat` 连接代码（不要删）：
  - Stop 按钮（PR 2 详述）仍要发 `{type:"stop"}` 走旧路径，这是 SDK `interrupt_current_turn` 的唯一前端入口
  - 新 streaming + Cancel 走新 v1 client；两者负责不同语义，不冲突
  - 旧 client 内除了 Stop 之外的方法（如发送用户输入、订阅 token stream）应**标 deprecated**——chat-panel 切到新 client 后这些路径就不再被调用，但代码留着避免 Stop 路径被牵连
- 单测：用 mock WS 测重连先 GET 逻辑
- 手动测：chat 一次正常对话；中途断网恢复，验不重发不丢

### PR 2 — chat-panel 切到 v1 endpoints + 全局 reauth banner + Stop/Cancel 双按钮

- chat-panel 内所有 fetch URL 切 `/api/v1/coworkers/{id}/conversations` 等
- 用 typed client（00c 的 `web/src/api/client.ts`），**禁止任何手写 URL 字面量**——CI 加一个 lint 扫描 `/api/admin/` 字面量在 v1 前端代码里不允许出现
- 全局 reauth banner（设计 §6.3 J）：
  - `<rm-reauth-banner>` 组件挂在 `<rm-app-shell>` 顶部
  - 监听 ws event bus 的 `event.run.requires_reauth` → 显示横幅 + 提供"Re-login"按钮（按钮在 bootstrap 模式下走"重新 input bootstrap token"模拟，给 dev 调试用）
  - bootstrap fast-path 不会真触发这个 event；banner 代码必须存在但不暴露入口（除非 dev 模式 force trigger）

**Stop vs Cancel 双按钮（设计 §4.1 硬约束）**：

- chat UI 必须**同时存在两个独立按钮**：
  - **Stop**（保留现有按钮 + 现有行为）—— 仍走旧 `/ws/chat {type:"stop"}` 触发 SDK `interrupt_current_turn`；中止本轮 turn，**容器不重启**；下一轮可以立刻继续
  - **Cancel**（新增按钮）—— 调 `POST /api/v1/runs/{id}/cancel`；走 chore A 接好的链路；硬杀容器 + `runs.status='cancelled'`
- 文案区分清楚：
  - Stop 按钮 tooltip："Interrupt this response (continue conversation)"
  - Cancel 按钮 tooltip："Cancel run and release container (next message starts fresh)"
- 两个按钮在 run 已 terminal 时都禁用（disabled state + tooltip 说明）
- **禁止**把 Stop 按钮重指向新 Cancel endpoint —— 那会让每次软中断都付容器冷启动税
- pinned test（playwright 或类似）：
  - 点 Stop → NATS `agent.*.interrupt` 发出 → 容器仍存活（next prompt 立即响应）
  - 点 Cancel → NATS `web.run.cancel.*` 发出 → 容器被 stop（next prompt 触发 1-3s 冷启动）+ runs.status='cancelled'

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
- [ ] **Stop 与 Cancel 两个按钮独立工作**（手动 smoke 必跑）：
  - Stop → 中止本轮 + 容器保留 + 立即可继续输入
  - Cancel → 容器被杀 + runs.status='cancelled' + 下次发消息冷启动
- [ ] 整套前端单测 + e2e（如有）通过
- [ ] **Phase 1 完整 smoke**（设计 §10 Phase 1 清单）：
  - 真 Anthropic key 创建 coworker（API）→ web 发消息（bootstrap as alice）→ token stream 显示 → run.completed → DB 里 runs 表 status/completed_at/usage 都写了
  - 切 BOOTSTRAP_USERS 不同 token → user 身份变了（GET /api/v1/me 显示不同）
- [ ] 更新 plan 状态

## Out of scope

- ❌ Coworker 创建向导完整 UI —— 留 02a
- ❌ 详情页 tabs (overview/skills/mcp/bindings/...) —— Phase 2+
- ❌ Approvals / Skills / Credentials 页面 —— 留对应 Phase
- ❌ **合并旧 `/ws/chat` 与新 `/api/v1/conversations/{id}/stream` 两个 WS endpoint** —— Stop / Cancel 双语义当前各走一条；统一方案（让 SDK interrupt 也走新 endpoint）涉及 backend 协议扩展，留下游 session 单独处理

## Open questions

1. **idempotency_key 是 client 永久持久化还是 in-memory only**？in-memory 简单但页面刷新就丢；持久化 (sessionStorage) 更鲁棒。**推荐 in-memory** —— 页面刷新走重连先 GET truth 逻辑，已经覆盖
2. **reauth banner 在 dev 模式怎么触发**：URL query `?reauth=1` / console 命令 / 隐藏 dev menu？建议简单的 console 命令（`window.__forceReauth()`）
3. **`/api/admin/` 切换是一刀切还是渐进**？01c 一次切完 chat 路径，但 admin 路径（用户管理等）继续用 admin endpoint。Phase 2-4 再各自迁
4. **Lit 还是其它框架**：确认现有 `web/` 用的是 Lit + Tailwind（设计写了），如果是其它框架本 prompt 的组件示例要调整
5. **Stop 按钮在 UI 上放哪**：典型方案 a) 放在聊天输入框旁（agent 正在生成时显示）；b) 放在每条 in-progress assistant message 上。chore A Findings 没给意见；现有 chat-panel 实现可能已有放置——保留现有位置，不为本 session 重新设计

## Pitfalls

- 删除旧 ws client 前确认**没有任何调用方残留**——`grep -r "old-ws-client-name" web/`
- 重连先 GET truth 的逻辑必须真生效——容易写成"reconnect 就直接订阅"，那 INV-6 端到端就废
- typed client 的 error response 类型不能丢——`ErrorResponse` schema 错误必须在 TS 类型上能 narrow
- chat-panel 内部状态（messages array）在切 conversation 时必须重置——容易漏，导致两个对话内容串
- 全局 reauth banner 不要每次 ws disconnect 就显示——只在收到 `event.run.requires_reauth` 时显示
- **Stop 与 Cancel 绝不能合并**（设计 §4.1）—— 合并的代价是每次软中断付 1-3s 容器冷启动税；如果觉得 UI 上两个按钮难放，宁可把 Cancel 藏进右键菜单 / overflow menu，也不要把它绑到 Stop 按钮的 onClick
- 旧 `/ws/chat {type:"stop"}` 路径**不要删**——它是 Stop 按钮唯一的实现路径；本 session 完工后 chat 仍同时连旧 `/ws/chat`（Stop 用）与新 `/api/v1/conversations/{id}/stream`（streaming + Cancel 用）。两套 WS 并存到下游 session（02+）有更好的统一方案再合

## Findings (after execution)

_(empty — 重点记录：现有前端框架确认、admin → v1 切换是否真一刀切、idempotency 选择)_
