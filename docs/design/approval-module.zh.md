# 审批模块设计

> 状态：提案
> 范围：用于敏感 Agent 工具调用的人工审批独立模块

## 问题

Agent 可以调用执行真实操作的外部工具（MCP 服务器）：退款、广告出价调整、基础设施变更等。其中部分操作属于高风险，应当在执行前要求人工确认。

这**不是**权限问题。权限回答的是"这个 Agent 是否被允许调用此工具？"（静态的是/否）。审批回答的是"这次调用是被允许的，但这次具体的调用需要人工确认"（动态的，逐次判断）。

## 两种审批模式

### 模式 A：拦截式（安全网）

Agent 自主决定调用某个工具。平台拦截该调用，暂停执行，并请求人工审批。

```
Agent 调用 refund(amount=50000)
  → 拦截器匹配规则：refund + amount > 10000
  → 执行暂停
  → 向指定审批人发送审批请求
  → 批准 → 执行
  → 拒绝 → 返回"审批被拒绝"给 Agent
```

使用场景：Agent 执行定时任务或自主工作流，没有人类在对话中主动参与。

### 模式 B：方案式（协作）

Agent 向用户展示一个方案。用户审核并批准。然后 Agent 携带审批令牌执行。

```
Agent："建议将广告 X 的出价从 5 元调整为 15 元。是否执行？"
用户："可以，执行吧"
Agent 调用 request_approval(tool="adjust_bid", args={ad: X, price: 15})
  → 系统向用户（即"发起者"）发送审批请求
  → 用户点击批准
  → Agent 收到审批令牌
  → Agent 调用 adjust_bid(ad=X, price=15, approval_token=...)
  → 拦截器验证令牌 → 执行
```

使用场景：交互式对话，Agent 和人类协作决策。

### 两种模式的关系

模式 B 是主路径。模式 A 是安全网。两者协同工作：

- 如果 Agent 持有有效的审批令牌 → 跳过拦截，直接执行
- 如果 Agent 没有令牌 → 触发拦截式审批（模式 A）

## 架构

### 组件

```
src/rolemesh/approval/
  types.py      # ApprovalRule, ApprovalRequest, ApprovalToken
  rules.py      # 规则匹配引擎
  gate.py       # 拦截器（从 IPC task_handler 调用）
  notify.py     # 通过现有频道发送审批请求
```

### 审批规则（声明式，以 JSONB 存储在数据库中）

```json
{
  "rules": [
    {
      "tool": "refund",
      "condition": "args.amount > 10000",
      "approvers": ["role:admin", "user:finance-lead"],
      "timeout": "24h"
    },
    {
      "tool": "adjust_bid",
      "condition": "args.new_price / args.old_price > 2.0",
      "approvers": ["role:admin", "initiator"],
      "timeout": "4h"
    },
    {
      "tool": "delete_campaign",
      "condition": "true",
      "approvers": ["role:owner"],
      "timeout": "24h"
    }
  ]
}
```

关键字段：`"initiator"` 指的是与 Agent 发起对话的用户。这自然地支持了模式 B。

### 审批请求生命周期

```
PENDING → APPROVED
        → REJECTED
        → EXPIRED（超时）
```

### 审批令牌

```python
@dataclass(frozen=True)
class ApprovalToken:
    request_id: str       # 关联到审批请求
    tool: str             # 被批准的工具
    args_hash: str        # 被批准参数的 SHA-256 哈希（防篡改）
    approved_by: str      # 审批人
    approved_at: str      # 审批时间
    expires_at: str       # 短时效（分钟级，而非小时级）
    constraints: dict     # 可选：审批人附加的约束条件
```

`args_hash` 是关键：对 `refund(amount=50000)` 的审批不能被用于执行 `refund(amount=500000)`。如果 Agent 在审批后修改了参数，令牌将失效。

### 执行流程

```
工具调用到达 IPC task_handler
  │
  ├─ ① 权限检查（auth 模块）→ 允许 / 拒绝
  │
  ├─ ② 审批关卡（approval 模块）→ 检查规则
  │     │
  │     ├─ 无匹配规则 → 直接通过
  │     ├─ 规则匹配 + 有效令牌 → 直接通过
  │     └─ 规则匹配 + 无令牌 → 创建审批请求 → 返回 PENDING
  │
  └─ ③ 执行工具
```

权限检查和审批关卡相邻但独立。它们互不知道对方的存在。

### Agent 侧工具

审批模块通过现有的 rolemesh MCP 服务器向 Agent 暴露工具：

- `request_approval(tool, args)` — 在调用工具前显式请求审批
- `check_approval(request_id)` — 轮询审批状态
- 审批结果也可以通过 NATS IPC 推送给 Agent

### 通知

审批请求通过现有频道（Telegram、Slack、WebUI）投递。不需要新的通知基础设施。`notify.py` 组件格式化审批请求，并调用 `route_outbound()` 发送给相应的审批人。

## 数据库 Schema

```sql
CREATE TABLE approval_rules (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    tool        TEXT NOT NULL,
    condition   TEXT NOT NULL DEFAULT 'true',
    approvers   JSONB NOT NULL,        -- ["role:admin", "initiator"]
    timeout     INTERVAL NOT NULL DEFAULT '24 hours',
    enabled     BOOLEAN DEFAULT true,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE approval_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    coworker_id     UUID NOT NULL REFERENCES coworkers(id),
    conversation_id UUID,
    rule_id         UUID REFERENCES approval_rules(id),
    tool            TEXT NOT NULL,
    args            JSONB NOT NULL,
    args_hash       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected/expired
    approvers       JSONB NOT NULL,
    decided_by      TEXT,
    decided_at      TIMESTAMPTZ,
    constraints     JSONB DEFAULT '{}',
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

## 设计决策

1. **独立模块** — 不属于认证/权限的一部分。不同的关注点，不同的生命周期。
2. **声明式规则** — 以配置形式存储在数据库中，非硬编码。管理员无需修改代码即可管理。
3. **令牌与参数哈希绑定** — 防止审批后篡改参数。
4. **短时效令牌** — 审批令牌在分钟级过期，而非小时级。减少被滥用的窗口期。
5. **复用现有频道** — 通知通过 Telegram/Slack/WebUI 发送，无需新的投递机制。
6. **优先使用 Agent 显式流程** — 模式 B（Agent 调用 `request_approval`）比尝试从自然语言中推断用户意图更可靠。

## 未来考虑

- 多级审批链（经理 → 总监 → VP）
- 审批委托（外出转发）
- 批量审批（一次批准多个类似请求）
- 审计日志集成
- 审批分析（平均响应时间、通过率）
