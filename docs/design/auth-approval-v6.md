# Auth + Approval v6.1 — Frozen Design

**状态**: 冻结（v6.1 = v6 + 评估修订）。后续修改请新建 v7 而非原地编辑。
**适用范围**: Phase 1（身份/渠道底座）+ Phase 2（自审审批）。两个 phase 共三个 implementation session。
**日期**: 2026-05-27
**与现有架构文档关系**: 本文是 `12-approval-architecture-cn.md` 的演进。当两者冲突时，以本文为准；待 v6.1 落地后回填 12 号文档。

---

## 0. 阅读须知

- 本文**自包含**。所有实施 session 不需要回看 v5 / v6 历史。
- 中文文档，但代码注释/字符串/docstring 一律英文（项目 CLAUDE.md 强制）。
- `file:line` 为现状代码参照；行号会随实施漂移，**实施前以 grep 实定位为准**。
- 关键变更点统一标记 `[P1-X]` / `[P2a-X]` / `[P2b-X]`，对应三个 session 的工作单元。

---

## 1. 系统目标

1. **审批模型收敛为"自审"**：发起人即审批人。删除三级 fallback、删除 `approval_default_mode` 三档。
2. **定时任务正确归属创建者**：调度行带 `created_by_user_id`，运行时透传为 `AgentInput.user_id`。
3. **身份以 Web/SSO 为根，IM 经强关联挂上**：整个 IM 收紧 1:1，未关联用户默认不建会话。
4. **审批 UX**：Web 卡片 + 按钮 + Telegram 原生按钮；NL 仅引导，绝不自动决策。
5. **为 SoD 预留接缝**：保留 `approver_user_ids` DB 列与 viewer-aware 比对位置，本期不实现。

---

## 2. 关键决策与理由（已冻结）

| #   | 决策                                                                                                              | 理由                                              |
| --- | ----------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| 1   | 自审：`resolved_approvers=[requester]`，删 `_resolve_approvers` 三级链                                            | 现实无"指定他人"需求；framing = "人 gate AI"      |
| 2   | `approver_user_ids` 保留 DB 列、移出 API/UI                                                                       | 真 SoD 接缝                                        |
| 3   | 批准后/执行前发"已开始执行"，结果完成发结果报告                                                                   | 长任务可见性                                       |
| 4   | 定时任务新增 `created_by_user_id`，自 `ToolContext.user_id` 捕获                                                  | 让任务受管控动作走正常自审                         |
| 5   | 空 requester / 投递落空 → owner FYI 文本（独立消息类型，**不含按钮**）、**不落审批行**、owner 不变 requester；绝不静默 | fail-close；拒绝越权 + 拒绝刷屏                    |
| 6   | 创建者离开租户 → 必做软取消其定时任务（`cancelled`，保审计）；与 delete_user 同事务 cancel-before-delete           | 防僵尸 task；强耦合于 `ON DELETE SET NULL` 的存在  |
| 7   | 删 `approval_default_mode`；未命中策略 = 自动批准 + 执行 + 留审计行（`source='auto_execute'`）。迁移可逆           | 三档过度设计；显式声明 default-allow 姿态          |
| 8   | 身份 = Web/SSO 根；IM 经关联挂上；本期只做 Telegram；IM 收紧 1:1                                                  | 单一身份源；1:1 让 user_id 单值、复用 Web 路径     |
| 9   | 审批 UX：Web + Telegram 原生按钮 + 退化 deep-link；NL 仅引导。两条发送路径：approver→卡片+按钮 / owner→FYI 文本；viewer-aware 降为 SoD 接缝 | 1:1 下 viewer 比对恒真，是 YAGNI                  |
| 10  | `decide` 保持非幂等，UI 层捕获 `ConflictError` 渲染"已处理"                                                       | 不动引擎语义                                       |
| 11  | IM 1:1 默认关闭准入：未关联 sender 不建会话/不喂 agent；公开-bot 模式作未来 opt-in                                | 防陌生人耗额度/看输出；fail-close                  |
| 12  | safety 路径**不自审**，继续使用 `_tenant_owner_ids`                                                               | safety gate 的本意是绕开用户判断，自审违背语义     |
| 13  | 一 user 允许绑多 Telegram 账号（不加 `UNIQUE(user_id, platform)`）                                                | 现实场景：个人号 + 工作号                          |
| 14  | 删除 `security/sender_allowlist.py` 及其 settings 字段                                                            | 新的默认关闭准入更严格、语义更清晰，两层并存只造迷雾 |
| 15  | 运行中 task 不中断；下一 tick 不再触发（scheduler 已两道 `status='active'` 过滤）                                  | 无 "running" 中间状态需要处理；保持调度模型简洁    |

---

## 3. 两阶段总览

```
Phase 1 (身份/渠道底座) — 1 session
   ↓ 不变式: Web 沿用 conv.user_id; IM 1:1 经关联后入站解析 user_id; 定时任务带 created_by
Phase 2a (后端 engine + Web 卡片按钮) — 1 session
   ↓ 不变式: 自审统一; auto-execute 留 system audit 行; Web 卡片可决策
Phase 2b (Telegram 按钮 + 收尾) — 1 session
```

依赖单向：P2b → P2a → P1。`H`（删 `approval_default_mode`）可与任何顺序解耦，但本设计将其归入 P2a 一并做。

---

# PHASE 1 — 身份 / 渠道底座

## P1.1 确立的不变式（完成判定）

完成后：
- Web 会话沿用 `conv.user_id`（bootstrap 伪用户等管理边缘除外）。
- IM 1:1 经强制关联后**每条入站都能解析出 user_id 并回填 conv.user_id**。
- 定时任务行带 `created_by_user_id`，运行时透传为 `AgentInput.user_id`。
- "任何人可 DM bot 自由对话"的开放门**已关闭**。

## P1.2 数据模型 [P1-DB]

### 新表 `user_channel_identities`
```sql
CREATE TABLE user_channel_identities (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    platform    TEXT NOT NULL,                -- "telegram" | future: "slack" | ...
    channel_id  TEXT NOT NULL,                -- normalized: telegram → numeric string of from.id
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (tenant_id, platform, channel_id)  -- DB-level race protection
);
CREATE INDEX idx_uci_user_platform ON user_channel_identities(user_id, platform);
CREATE INDEX idx_uci_lookup        ON user_channel_identities(tenant_id, platform, channel_id);
```

**注意**：**不加** `UNIQUE(user_id, platform)`，因决策 #13 允许一 user 多账号。

### 新表 `link_tokens`
```sql
CREATE TABLE link_tokens (
    token       TEXT PRIMARY KEY,             -- random URL-safe, ≥ 22 chars
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    platform    TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,         -- now() + ~10 min
    used_at     TIMESTAMPTZ,                  -- NULL until consumed
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_link_tokens_expiry ON link_tokens(expires_at) WHERE used_at IS NULL;
```

**GC**: 未做（已知缺陷，列入未来项）。本期数据量小，过期行残留无害。

### 修改 `scheduled_tasks`
```sql
ALTER TABLE scheduled_tasks
  ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
```

### 修改 `ScheduledTask.status` Literal
```python
# core/types.py:307
status: Literal["active", "paused", "completed", "cancelled"]  # add cancelled
```

### `users.channel_ids` JSONB
**降级**为冗余展示字段，不再承担唯一性/反查。本期**不删**（dev 数据，无害）；列入未来项的"删除冗余列"。

## P1.3 重置存量 IM 状态 [P1-DB]

**保留** `channel_bindings`（coworker↔bot 凭据，bot 必须可用）。
**重置** 用户侧 IM 身份关联与存量 IM 会话状态（dev 数据可重建）：

```sql
-- Suggested cleanup (executed as part of P1-DB migration script)
DELETE FROM conversations WHERE channel_type IN ('telegram', 'slack');
-- user_channel_identities is a new table, no prior rows
```

让"强制关联"模型从干净状态开始。

## P1.4 Telegram 关联流程（UX） [P1-LINK]

**安全前提**：证明用户拥有该 Telegram 身份——令消息真从该账号发出，顺带捕获正确格式 id。

### 流程
1. WebUI 设置 → "已连接的渠道"，点 "连接 Telegram"。
2. 后端 `POST /api/v1/me/channel-links/telegram` → `link_tokens` 插入一行（绑定 `user_id/tenant_id/"telegram"`，`expires_at ≈ now() + 10 min`，`used_at=NULL`）→ 返回 token。
3. Web 给入口：**首选 deep-link** `https://t.me/<bot>?start=<token>`（自动发 `/start <token>`）；**兜底**可复制短码。进入等待态（前端轮询 `GET /api/v1/me/channel-links/telegram` 检测关联状态，倒计时显示）。
4. 用户在自己 Telegram 里点 Start / 粘贴码 → 入站带真实 `from.id`（纯数字）→ 网关在**常规 message handler 之前**识别 `/start <token>`（python-telegram-bot 的 `CommandHandler("start", ...)` 注册顺序天然先于 `MessageHandler(filters.TEXT)`）：
   - **原子消费令牌**：
     ```sql
     UPDATE link_tokens
        SET used_at = now()
      WHERE token = $1
        AND used_at IS NULL
        AND expires_at > now()
     RETURNING user_id, tenant_id;
     ```
     check-and-mark 一步杜绝并发重复用。
   - **写身份**：`INSERT INTO user_channel_identities(...)`。唯一性由 DB UNIQUE 保证（撞约束即拒）。`channel_id` 归一化为 `str(update.effective_user.id)`（纯数字字符串）。
   - Bot 回 `✅ 已关联（<user.name>）`；Web 翻为 "已连接 @handle"（poll 命中后从该 ID 反查 username 展示）。

### 边界
| 场景                          | 处理                                                                                  |
| ----------------------------- | ------------------------------------------------------------------------------------- |
| 令牌过期/已用                  | 原子 UPDATE 返回空 → bot 回 "链接失效，请重新发送"                                    |
| 该 Telegram 身份已绑别账号    | 撞 UNIQUE 约束 → bot 回 "此 Telegram 账号已关联到另一个 RoleMesh 账号，请先解绑"     |
| 断开连接（Web 操作）           | `DELETE FROM user_channel_identities WHERE ...`；该用户该平台的 1:1 会话 `conv.user_id` 置 NULL（不删会话，保留历史） |
| 跨 coworker                    | 身份是 (user, platform) 级；关联一次对该平台所有 coworker 私聊生效                    |

**刻意不做**：多因子；按 coworker 粒度关联。

## P1.5 IM 收紧 1:1 + 准入姿态 [P1-ADMIT]

### 群聊短路
`channels/telegram_gateway.py` 入站（grep `is_group` 或 `chat.type`）：
- `chat.type in ("group", "supergroup", "channel")` → 不建会话、不处理，回 "我目前仅支持私聊"。
- **不拆除**现有群聊机制（`requires_trigger` 等可能在 main.py 中），仅入口短路。理由：未来项可能恢复，机制留作休眠代码胜过删除。

### 默认关闭准入（1:1）
未关联 sender 在 1:1 下：
- **不建会话**、**不喂 agent**、**fail-close**。
- bot 回 "请先在 RoleMesh Web 关联你的 Telegram 账号（设置 → 已连接的渠道）"。

公开-bot 模式作显式 opt-in，列未来项。

## P1.6 conv.user_id 解析 / 惰性回填 [P1-RESOLVE]

### 新增反查 DB 函数
```python
# src/rolemesh/db/user.py (or new module: src/rolemesh/db/channel_identity.py)
async def resolve_user_from_channel_sender(
    tenant_id: str, platform: str, channel_id: str
) -> str | None:
    """Look up user_id from a normalized channel sender. Returns None
    when no linkage exists. INFO-log misses so admins can spot
    silent admission failures during onboarding."""
```

走 `(tenant_id, platform, channel_id)` 索引，热路径安全。

### main.py 入站 1:1 改造
（实施时 grep `_handle_incoming` / `main.py:598` 实定位）
```python
if is_group:
    # group short-circuit (P1.5)
    ...
    return

resolved_user_id = await resolve_user_from_channel_sender(tenant_id, "telegram", sender_id)
if not resolved_user_id:
    # admission denied — not linked
    await sender.reply("请先在 RoleMesh Web 关联...")
    logger.info("im_admission_denied", platform="telegram", sender=sender_id)
    return

# Lazy backfill conv.user_id when missing (legacy convs created before P1).
if conv.user_id is None:
    await update_conversation_user_id(conv.id, resolved_user_id)
    conv.user_id = resolved_user_id

# Continue down the existing path — main.py:782/980 etc. unchanged.
```

之后 Web/IM 走同一条 `conv.user_id` 路径。

## P1.7 定时任务创建者透传 [P1-TASK]

四处协同修改：

| File                                              | 改动                                                                                  |
| ------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `agent_runner/tools/rolemesh_tools.py:~232`      | `schedule_task` payload 加 `userId: ctx.user_id`                                       |
| `ipc/task_handler.py:~70-163`                     | `process_task_ipc` 解包 → 传 `create_task(..., created_by_user_id=...)`               |
| `src/rolemesh/db/task.py:~32-50`                  | `create_task` 加参数；行映射读 `created_by_user_id`                                   |
| `src/rolemesh/orchestration/task_scheduler.py:~215-227` | `_run_task` 把 `task.created_by_user_id` 塞进 `AgentInput.user_id`                |
| `src/rolemesh/core/types.py:~307` (ScheduledTask) | 加 `created_by_user_id: str \| None` + status Literal 补 `cancelled`                  |

## P1.8 F 离职软取消（必做，与 P1.7 强耦合） [P1-CLEANUP]

为何必做：`created_by_user_id` 用 `ON DELETE SET NULL`；若用户硬删时不先 cancel，残留 `active` 任务的 `user_id` 变 NULL → 下个 tick 带空 `user_id` 跑 → 落 Phase 2 的 E 路径刷 owner + 无审计。SET NULL 与"F 可选"不能并存。

### DB 改动
```python
# src/rolemesh/db/task.py
async def cancel_tasks_for_user(user_id: str, tenant_id: str) -> int:
    """Soft-cancel all active tasks owned by user. Returns count for
    logging. status flips to 'cancelled' so the scheduler skips them
    via its existing `status = 'active'` filter."""
```

### `delete_user` 同事务改造
（grep `db/user.py:344` 或 `webui/admin.py:450` 实定位）
```python
async with db_pool.acquire() as conn:
    async with conn.transaction():
        await cancel_tasks_for_user(user_id, tenant_id, conn=conn)
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)
        # SET NULL fires here, but status='cancelled' already, scheduler skips harmlessly.
```

## P1.9 Phase 1 测试 [P1-TEST]

**严格遵守 ~/.claude/CLAUDE.md 测试理念**：先理解规格→写边界用例→集成测试优先→变异思维。

至少覆盖：

| ID    | 测试                                                                                                                |
| ----- | ------------------------------------------------------------------------------------------------------------------- |
| T1.1  | `link_tokens` 原子单用：并发两次 `/start` 同令牌只成功一次（用 asyncio.gather 并发两个 UPDATE）                       |
| T1.2  | 过期令牌被拒；已用令牌被拒（两个独立用例）                                                                            |
| T1.3  | `user_channel_identities` UNIQUE：同 (tenant, platform, channel_id) 第二次 INSERT 被 DB 拒；用 `asyncpg.UniqueViolationError` 验证错误类型 |
| T1.4  | 解绑：删行后再次 `/start` 新 token 可重新关联                                                                       |
| T1.5  | `resolve_user_from_channel_sender` 命中走索引（pg EXPLAIN 不强求；至少验证返回 user_id）                            |
| T1.6  | 反查未命中返回 None 并写 INFO 日志（捕获 caplog）                                                                   |
| T1.7  | 1:1 入站 未关联 sender → 不建会话、不喂 agent、bot 回引导文本                                                       |
| T1.8  | 1:1 入站 已关联 sender → 惰性回填 `conv.user_id`                                                                    |
| T1.9  | 群聊入站短路：返回引导文本，不进 agent                                                                              |
| T1.10 | IM 创建的 schedule_task → 行 `created_by_user_id` 已填                                                              |
| T1.11 | Web 创建的 schedule_task → 同上                                                                                     |
| T1.12 | scheduler `_run_task` 取出任务 → `AgentInput.user_id == created_by_user_id`                                         |
| T1.13 | `delete_user` 同事务：删用户前后，原 active task 状态为 `cancelled`；scheduler 不再触发                              |
| T1.14 | `ScheduledTask.status` 接受 `"cancelled"`（types 序列化/反序列化）                                                  |
| T1.15 | 变异测试：把 `expires_at > now()` 改成 `>=` 是否被 T1.1/T1.2 抓到（脑内变异，不强求工具）                            |

## P1.10 Phase 1 独立价值

即使 Phase 2 未上：Phase 1 已关闭 "任何人可 DM bot 自由对话" 的开放门（1:1 + 强制关联 + 默认关闭准入），并为定时任务建立创建者归属与离职清理。**独立交付、独立可测、独立有安全价值**。

---

# PHASE 2 — 自审审批（建立在统一身份上）

## P2.0 Phase 2 切分点

| Session | 范围                                                                                                                                       |
| ------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| **P2a** | H (删 `approval_default_mode`) + A (自审) + B (隐藏 approver_user_ids) + C (开始消息) + E (边缘兜底 + `:488` 拆分) + G (投递) + I-web (卡片+按钮+pending turn) |
| **P2b** | I-IM Telegram 按钮 + 端到端冒烟测试 + 文档收尾                                                                                              |

**Branch 策略**：P2a 与 P2b **共用同一 branch `feat/self-approval`**。P2a 完成后**不单独合入 main**，P2b 在同 branch 上继续 push commit，最终整个 Phase 2 一个 PR。理由：避免 main 出现 "Web 能审批但 Telegram 不能"的中间状态——Telegram bot 用户会以为 bot 坏了。代价：PR 较大、回滚粒度粗；通过 commit 边界清晰（每 checkpoint 一组 commit）来缓解 review 难度。

## P2.1 统一身份带来什么

Phase 1 保证交互式/任务轮次恒有 `user_id`（除 bootstrap 等管理边缘外）。自审模型**无条件分支**：`resolved_approvers=[requester]` 且 requester 恒存在；E 路径缩回真正边缘（机器链式自建、纯系统轮次、bootstrap 伪用户）。

## P2.2 A 自审解析 [P2a-A]

`approval/engine.py:_resolve_approvers`（grep 实定位，约 `:961`）：
```python
async def _resolve_approvers(
    self,
    tenant_id: str,
    coworker_id: str,
    policy: ApprovalPolicy,
    requester_user_id: str,           # NEW
) -> list[str]:
    """v6.1 self-approval: requester is the sole approver. Empty
    requester signals an edge case (bot-chained task / pure system
    turn / bootstrap actor) — caller routes those to the owner-FYI
    edge path (P2.6); never invents an approver."""
    if not requester_user_id:
        return []
    return [requester_user_id]
```

**签名变更影响 3 个调用点**：
1. Case A（`approval/engine.py:~368`）
2. `handle_auto_intercept`（`approval/engine.py:524`）
3. `create_from_safety`（`approval/engine.py:~593`）—— **此点不变行为**：safety 不自审（决策 #12）。具体改法：safety 路径**不调用** `_resolve_approvers`，继续直接用 `_tenant_owner_ids(tenant_id)`（现状已如此，只是把命名/注释明确成"safety 故意不走自审"）。

`_tenant_owner_ids`（`engine.py:1016`）**保留**，供 safety 路径 + E 路径使用。

`decide` 鉴权 `user_id ∈ resolved_approvers` 不变 → 自审下 requester 决策放行、他人决策拒绝（同 SoD 未来路径）。

## P2.3 H 删 approval_default_mode（可独立/最先做） [P2a-H]

### 代码删除
- `db/tenant.py:~54-111`：删读取兜底/构造/校验
- `db/schema.py:61-62`：删列定义
- `core/types.py:117`：Tenant 类删 `approval_default_mode` 字段

### Case A 塌缩（`approval/engine.py:~368-432`）
```python
# Before (v6):
# if tenant.approval_default_mode == 'deny': create rejected; return
# if tenant.approval_default_mode == 'require_approval': create pending; ...
# else: create pending+approved; execute

# After (v6.1):
# Always: build pending → mark approved with source='auto_execute' → publish decided → execute
```

`skipped` / `rejected` 状态他处仍用（safety、user-rejected 等），**勿全删**。

### Audit row 标记 (M4 决议)
利用 `approval_requests.source` 现有列。

```sql
ALTER TABLE approval_requests DROP CONSTRAINT IF EXISTS approval_requests_source_check;
ALTER TABLE approval_requests ADD CONSTRAINT approval_requests_source_check CHECK (
    source IN ('proposal', 'auto_intercept', 'safety_require_approval', 'auto_execute')
);
```

Case A 塌缩后的 INSERT 用 `source='auto_execute'`，不冒充人工决策。

### 可逆迁移
在 schema.py 同 migration 注释里**显式备好反向 SQL**：
```python
# REVERSAL (manual; not auto-run):
#   ALTER TABLE tenants ADD COLUMN approval_default_mode TEXT
#     DEFAULT 'auto_execute' CHECK (approval_default_mode IN
#     ('auto_execute', 'require_approval', 'deny'));
# Continued context: v6.1 删除 default-deny 逃生口，未来由"白名单 =
# policy 加 allow 动作 + 配套 default-deny 姿态"接回。
```

## P2.4 B 隐藏指定审批人字段 [P2a-B]

`webui/schemas_v1.py` 的 `ApprovalPolicyCreate` / `ApprovalPolicyUpdate` 移除 `approver_user_ids`；前端策略表单去掉。
**DB 列保留**作 SoD 接缝。

## P2.5 C 长批量"开始执行" [P2a-C]

`approval/executor.py:claim_approval_for_execution`（约 `:223`）成功后、执行前：
```python
await self._channel.send_to_conversation(
    req.conversation_id,
    format_execution_started(req),     # NEW in notification.py
)
# best-effort: catch & log, do not block execution
```

新增 `format_execution_started(req)` 至 `approval/notification.py`，按现有 `format_execution_*` 风格。

## P2.6 E 边缘兜底 + 拆分 `engine.py:488` [P2a-E]

### 必做前置：拆分合并判断
（`approval/engine.py:488`）
```python
# Before:
if not user_id or not server or not tool:
    logger.warning("approval: malformed auto_approval_request dropped")
    return

# After:
if not server or not tool:
    logger.warning("approval: malformed auto_approval_request dropped",
                   has_user=bool(user_id), has_server=bool(server), has_tool=bool(tool))
    return
# user_id may be empty here — handled below by E path, not dropped.
```

### E 路径行为
当 `user_id` 为空 OR `_resolve_approvers` 返回 `[]`：
```python
owner_ids = await _tenant_owner_ids(tenant_id)
if not owner_ids:
    logger.error("approval: edge fallback — no tenant owners; nothing to notify",
                 tenant_id=tenant_id, server=server, tool=tool)
    return  # action stays fail-closed by hook; nothing more to do

# Dedup/rate-limit (same source short window) — see below
if _edge_dedup_seen(tenant_id, coworker_id, server, tool):
    return

for owner_id in owner_ids:
    await self._send_owner_fyi(owner_id, format_edge_fyi(server, tool, params))
# Crucially: do NOT create an approval_request row.
```

### 限流细节
"同一来源短窗去重限流" 具体化为：
- Key: `(tenant_id, coworker_id, server, tool)`
- Window: 5 分钟（in-process LRU；进程重启重置，可接受）
- 实现：简单的 `OrderedDict` 容量上限 1000 或 `expiringdict`

### `_send_owner_fyi` 与 FYI 格式
新增 helper（`approval/notification.py`）：返回独立消息模板，**不含按钮**，模板示例：
```
⚠️ FYI（无人可归属的受管控动作）
租户: <tenant>
Coworker: <coworker_name>
拦截动作: <server>/<tool>
时间: <ts>
原因: 触发动作的轮次无可识别用户（系统/链式/bootstrap）。
该动作已被拦截，不会执行。请在 Web 端查看活动日志确认上下文。
```

### `create_skipped` 分支处理 (M1 决议)
原 `handle_auto_intercept:530` 的 `create_skipped(...)` 分支：**移除**。
不再用 "skipped" 状态承载"无 approvers"的语义；改为 E 路径（owner FYI 不落行）。`skipped` 状态仍由 safety 等其他路径在合理场景下产生。

## P2.7 G 投递（无 viewer 比对，两条发送路径） [P2a-G]

`approval/notification.py:NotificationTargetResolver`（约 `:56-186`）：
- 自审 Tier-2/3 命中**发起人对话**。
- 定时任务优先投**创建对话**（task.conversation_id 或创建者与该 coworker 的现存对话）。

**approver 路径** → 审批卡片（含按钮）
**owner 兜底路径** → FYI 文本（独立消息类型，不含按钮）

绝不新建对话、绝不静默；不为兜底创建 pending 行。

`ChannelSender`（`notification.py:35-45`）增加可选审批卡分发方法：
```python
async def send_approval_card(
    self,
    conversation_id: str,
    card: ApprovalCardPayload,   # structured: title, summary, request_id, actions
) -> None: ...
```

Telegram gateway 实现该方法发 `InlineKeyboardMarkup`；其他渠道默认退化为纯文本 + Web deep-link。

不需要携带 `resolved_approvers`（viewer 比对本期不做）。

## P2.8 I-web 审批 UX [P2a-Iweb]

### 现有结构化事件
`engine.py:596-622` → `webui/v1/ws_stream.py:283-310` 已发 `web.approval.required` 事件。

### 前端补卡片（Lit + Tailwind v4）
- 新增 `web/src/components/approval-card.ts`（Lit element）：title / summary / ✅/❌ 按钮。
- 接入 `web/src/ws/user_approvals_client.ts`（已存在）的事件流。
- 按钮 click → `POST /api/v1/approvals/<id>/decide` → 命中 `ConflictError` 渲染"已处理"。
- 卡片不需要 viewer 比对（决策 #9）。

### pending 期新 turn 引导 (M3 决议)
**插入点**：后端 `agent.run()` 入口（或 main.py 入站 dispatch）——具体由 P2a 实施时定位。
**实现**：
```python
# pseudo
pending = await has_pending_approvals_for_conversation(conv.id)
if pending:
    await sender.reply(_GUIDE_TEXT)
    logger.info("turn_blocked_by_pending_approval", conv=conv.id)
    return
```

新增 DB helper：
```python
# src/rolemesh/db/approval.py
async def has_pending_approvals_for_conversation(conv_id: str) -> bool:
    """Whether the conversation has any approval_requests in 'pending' status."""
```

引导文本统一一处，前后端复用（前端从 OpenAPI 返回的 error code 渲染，后端 IM 直接 reply 文本）：
```
该会话有审批待处理。请先在 Web 或 Telegram 中决策后再继续。
```

**注意**：本规则**主要消除"agent 说好的却没动静"的混乱**；并非堵执行后门（受管控工具被 hook 独立 fail-close）。因此实现上**只在交互式入口检查**，不需要在每个 tool call 处插钩子。

## P2.9 Phase 2a 测试 [P2a-TEST]

| ID    | 测试                                                                                                                |
| ----- | ------------------------------------------------------------------------------------------------------------------- |
| T2a.1 | 自审解析：`_resolve_approvers(req=<user>, ...)` 返回 `[<user>]`；空 requester 返回 `[]`                              |
| T2a.2 | `engine.py:488` 拆分后：server 空 → 仍 drop；user_id 空 → 进 E 路径（不 drop）                                       |
| T2a.3 | H 后 Case A：无策略命中 → 自动批准 + 执行 + 审计行 `source='auto_execute'`                                          |
| T2a.4 | safety 路径仍用 `_tenant_owner_ids`，不退化为自审（建一个 safety 触发的 fixture，断言 approvers 为 owners）         |
| T2a.5 | 可逆迁移：DROP `approval_default_mode` 后，给出的反向 SQL 在干净 schema 上能跑通（脚本测试，不强求自动）             |
| T2a.6 | `format_execution_started` 在 `claim_approval_for_execution` 成功后被调用（用 spy / mock channel）                  |
| T2a.7 | E 路径：空 requester → owner 收到 FYI 文本、不落 `approval_requests` 行（断言表行计数）                              |
| T2a.8 | E 路径无 owner：ERROR 日志、不抛、return（caplog 断言）                                                              |
| T2a.9 | E 路径限流：同 (tenant, coworker, server, tool) 5 分钟内第二次触发不再发 owner FYI                                  |
| T2a.10 | `create_skipped` 不再因"approvers 空"而触发（grep + 行为测试）                                                      |
| T2a.11 | I-web: WS 收到 `web.approval.required` → 渲染卡片（vitest + happy-dom）                                             |
| T2a.12 | I-web: 点 ✅ → 调 decide API → 卡片消失/标记完成                                                                    |
| T2a.13 | I-web: ConflictError → 卡片渲染 "已处理"                                                                            |
| T2a.14 | pending 期新 turn：有 pending → 不进 agent，回引导（建 fixture 制造 pending）                                       |
| T2a.15 | NL "approve" / "批准" 等文本**绝不**自动触发决策（关键负测试）                                                      |
| T2a.16 | 删除/改写旧用例：三级 fallback、`approval_default_mode` 校验、`require_approval` Case A 分支                        |
| T2a.17 | 变异思维核对：把 `if not requester_user_id` 改成 `if requester_user_id`、把 `source='auto_execute'` 写错——T2a.1/T2a.3 能否抓到 |

## P2.10 Phase 2a 行为规格（边界）

- **默认安全姿态 = default-allow**：未命中策略 = 自动批准 + 执行 + 留审计行（`source='auto_execute'`）。本期刻意删除 default-deny 逃生口，继任 = 未来白名单。
- **fail-close 不破**：hook 该拦的仍拦；E 路径"不落行"是审批侧，不是执行侧（执行侧由 hook 兜底）。
- **绝不静默**：未映射/空 requester/落空 → 统一走 E（owner FYI 文本、不落行）。
- **两条发送路径**：approver→卡片+按钮 / owner→FYI 文本；本期不建 viewer 比对。
- **pending 期新 turn 只回引导**（仅交互式入口检查，非每 tool 检查）。
- **NL 绝不自动决策**；决策人必须 ∈ resolved_approvers。
- **防重**：UI 层吸收 `ConflictError`；引擎非幂等。
- **auto-approve 审计**：`source='auto_execute'`，不冒充人工决策。

---

# PHASE 2b — Telegram 按钮 + 收尾

## P2b.1 I-IM Telegram 按钮 [P2b-IM]

### 出站
`channels/telegram_gateway.py` 新增 `send_approval_card`：
```python
async def send_approval_card(self, conversation_id: str, card: ApprovalCardPayload) -> None:
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 批准", callback_data=f"apr:{card.request_id}"),
        InlineKeyboardButton("❌ 拒绝", callback_data=f"rej:{card.request_id}"),
    ]])
    await bot.send_message(chat_id=..., text=card.text, reply_markup=keyboard)
```

**callback_data 长度**：Telegram 限 64 字节。`"apr:" + uuid(36) = 40 字节`，安全。

owner 兜底路径调用现有 `send_to_conversation`（纯文本），与卡片路径并行。

### 入站
新增 `CallbackQueryHandler`：
```python
from telegram.ext import CallbackQueryHandler

app.add_handler(CallbackQueryHandler(_on_approval_callback))

async def _on_approval_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()  # ack within 10s (Telegram requirement)
    data = query.data or ""
    if not (data.startswith("apr:") or data.startswith("rej:")):
        return
    decision = "approve" if data.startswith("apr:") else "reject"
    request_id = data[4:]
    sender_id = str(query.from_user.id)

    # 1. Tenant resolution: bot token → channel_bindings → coworker → tenant
    binding = await get_channel_binding_for_bot_token(bot.token)  # NEW helper
    tenant_id = binding.tenant_id

    # 2. Identity resolution (Phase 1 lookup, scoped to this tenant)
    user_id = await resolve_user_from_channel_sender(tenant_id, "telegram", sender_id)
    if not user_id:
        await query.edit_message_text("⚠️ 你的 Telegram 账号未关联到 RoleMesh，无法决策。")
        return

    # 3. Engine decide (shared with Web path)
    try:
        await engine.decide(request_id=request_id, user_id=user_id, decision=decision)
        await query.edit_message_text(f"{'✅ 已批准' if decision == 'approve' else '❌ 已拒绝'}")
    except ConflictError:
        await query.edit_message_text("该审批已被处理。")
    except PermissionError:
        await query.edit_message_text("⚠️ 你无权决策此审批。")
    except Exception as e:
        logger.warning("telegram_approval_decision_failed", error=str(e))
        await query.edit_message_text("⚠️ 决策失败，请在 Web 端重试。")
```

### Edit 失败兜底
`edit_message_text` 可能失败（消息太老、权限改变）。捕获并 fallback 至发送新消息：
```python
try:
    await query.edit_message_text(...)
except telegram.error.BadRequest:
    await bot.send_message(chat_id=query.message.chat_id, text=...)
```

### 租户路由的安全性 (S5 决议)
**关键不变式**：CallbackQuery 的 tenant 必须从**接收 callback 的 bot 凭据**反查，绝不全局反查 sender_id。否则跨租户 sender_id 碰撞会泄漏决策。

新增 helper：
```python
# src/rolemesh/db/chat.py
async def get_channel_binding_for_bot_token(token: str) -> ChannelBinding | None: ...
```

## P2b.2 其他渠道（占位）

Slack / 其它：本期纯文本 + deep-link 到 Web。`send_approval_card` 默认实现 = 退化为纯文本含 Web URL。

## P2b.3 Phase 2b 测试 [P2b-TEST]

| ID    | 测试                                                                                                                |
| ----- | ------------------------------------------------------------------------------------------------------------------- |
| T2b.1 | Telegram 出站审批卡 → `InlineKeyboardMarkup` 正确构造，callback_data 长度 ≤ 64                                       |
| T2b.2 | CallbackQuery (apr:) → `engine.decide(approve)`；按钮消息被 edit 为 "✅ 已批准"                                       |
| T2b.3 | CallbackQuery (rej:) → `engine.decide(reject)`                                                                       |
| T2b.4 | CallbackQuery ConflictError → edit 为 "已处理"                                                                       |
| T2b.5 | 未关联 sender 点按钮 → edit 为 "未关联"，绝不 decide                                                                  |
| T2b.6 | 跨租户安全：bot A (tenant 1) 收到 callback，tenant 由 bot token 决定（断言 binding 路径，模拟 sender_id 在 tenant 2 也存在场景） |
| T2b.7 | Edit 失败 → fallback 发新消息（mock BadRequest）                                                                     |
| T2b.8 | Owner FYI 路径不发卡片，发纯文本（断言 InlineKeyboardMarkup 不出现）                                                |
| T2b.9 | E2E 冒烟：自审一个真实审批从触发→Telegram 卡片→点按钮→执行（如有 testcontainers/dev bot 可跑）                       |

## P2b.4 文档收尾

- 更新 `docs/12-approval-architecture-cn.md`（与 `-cn.md` 镜像版）反映 v6.1 实际落地。
- 本文（`docs/design/auth-approval-v6.md`）标记为 "✅ 已实施"，但**不删**（设计史）。
- 给 `STEPS.md` 或对应路线图勾上。

---

## 4. 跨 Phase 行为不变式（供回归测试用）

- **不变式 1**: 每个交互式 turn `AgentInput.user_id` 非空（bootstrap 等管理边缘除外）。
- **不变式 2**: 每个定时任务的运行 turn `AgentInput.user_id == ScheduledTask.created_by_user_id`。
- **不变式 3**: IM 1:1 未关联 sender 永不进 agent。
- **不变式 4**: IM 群聊永不进 agent。
- **不变式 5**: `approval_requests` 行的 `user_id` 永非空（schema 已强制 NOT NULL）。
- **不变式 6**: E 路径触发时**永不**创建 `approval_requests` 行；只发 FYI。
- **不变式 7**: NL 文本无法触发审批决策；决策必经按钮或 API。
- **不变式 8**: `decide` 鉴权 caller ∈ `resolved_approvers`，自审下即 `caller == requester`。

---

## 5. 不在本期的内容（未来项）

| 项                                       | 触发条件                                                          |
| ---------------------------------------- | ----------------------------------------------------------------- |
| Slack 关联 + Slack 按钮（或 SSO 自动关联） | 需要 Slack 集成铺垫                                                |
| 公开-bot 模式（opt-in）                  | 客户需求驱动；可在 `channel_bindings` 加 `public_mode` flag        |
| IM 群聊支持（多用户语义）                  | 现状 conversation 模型 1:1；多用户需重设计                         |
| SoD：`approver_user_ids` 重新启用 + 队列  | 复用现有 DB 列；新增 `approver_mode` 字段区分 self/designated      |
| 白名单 / default-deny                    | `policy.action` 加 `allow` 原语 + 租户策略姿态切换                  |
| Telegram 多 bot、Slack 多 workspace      | `channel_bindings` 已支持，UX 未做                                 |
| `link_tokens` GC                          | cron 或随便定期 `DELETE WHERE expires_at < now() - 7d`            |
| `users.channel_ids` JSONB 删除            | 已降级为冗余，可下个版本清                                         |
| viewer-aware 比对                        | 当出现 admin-impersonate 视图或多 viewer 场景时启用                |

---

## 6. 验收 Checklist（PR 评审用）

### Phase 1
- [ ] `user_channel_identities` / `link_tokens` 表迁移成功
- [ ] 令牌原子消费在并发下不重复使用（T1.1 通过）
- [ ] UNIQUE 约束在并发 INSERT 下阻止重复（T1.3 通过）
- [ ] IM 1:1 未关联 sender 被拒绝、群聊被短路（T1.7/T1.9 通过）
- [ ] 定时任务 `created_by_user_id` 端到端透传（T1.10/T1.11/T1.12 通过）
- [ ] `delete_user` 同事务先 cancel 后 delete（T1.13 通过）
- [ ] `security/sender_allowlist.py` 及其 settings 字段已删除（决策 #14）
- [ ] CLAUDE.md 测试理念被遵守：至少 3 条测试是因为先想到边界条件而写、且能抓到至少 1 个变异

### Phase 2a
- [ ] `_resolve_approvers` 改为自审；3 个调用点全部更新（A）
- [ ] `approval_default_mode` 列与代码引用全删；可逆迁移注释已写（H）
- [ ] `approval_requests.source` 加 `'auto_execute'` 枚举值（M4）
- [ ] `:488` 合并判断拆分（E 前置）
- [ ] E 路径：空 requester → owner FYI、不落行、限流生效（T2a.7/T2a.9）
- [ ] `create_skipped` 不再因 "approvers 空" 触发（T2a.10）
- [ ] `format_execution_started` 发送（T2a.6）
- [ ] I-web 卡片 + 按钮 + ConflictError 渲染（T2a.11-13）
- [ ] pending 期新 turn 引导生效（T2a.14）
- [ ] NL 负测试通过（T2a.15）
- [ ] 旧用例删除/改写（T2a.16）

### Phase 2b
- [ ] Telegram InlineKeyboard 出站正确（T2b.1）
- [ ] CallbackQuery 决策 + edit 回写（T2b.2/T2b.3）
- [ ] 未关联 sender 点按钮被拒（T2b.5）
- [ ] **跨租户安全**：tenant 由 bot token 决定，不可由 sender 决定（T2b.6）
- [ ] Edit 失败 fallback（T2b.7）
- [ ] Owner FYI 不发卡片（T2b.8）
- [ ] E2E 冒烟通过（T2b.9）

---

**文档结束**
