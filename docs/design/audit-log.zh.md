# 审计日志设计

> 状态：提案
> 范围：用于认证、审批和管理操作的横切审计日志基础设施

## 问题

RoleMesh 需要结构化地记录"谁在什么时候对什么做了什么，结果如何"，用于合规、安全分析和问题排查。这不同于运行日志（structlog），后者用于调试，可以随时删除。

## 范围

审计日志不属于 auth 模块。它是独立的基础设施模块。Auth、审批、Admin API 以及未来的任何模块都可以发出审计事件，彼此不产生依赖。

## 记录什么

### 认证事件
- 用户登录成功/失败
- JWT 验证失败（无效签名、过期）
- 身份解析结果

### 访问事件
- 用户请求使用某个 Agent → 通过/拒绝
- 访问受限 Agent

### Agent 执行事件
- Agent 调用工具 → 权限检查通过/拒绝
- 哪个用户触发的、哪个 Agent、调了什么工具

### 管理操作
- Agent 被创建/删除/权限修改
- 用户被分配/取消分配 Agent
- 角色变更

## 架构

```
Auth 模块          → emit_audit_event()
Approval 模块      → emit_audit_event()
Admin API          → emit_audit_event()
                         │
                         ▼
                   Audit 模块
                   （收集、存储、查询）
```

事件发送方不知道事件如何存储。审计模块可以不存在——emit 调用静默跳过。审计是可选增强，不是核心依赖。

## 审计事件结构

```python
@dataclass(frozen=True)
class AuditEvent:
    timestamp: str
    tenant_id: str
    actor_type: str          # "user" / "agent" / "system"
    actor_id: str
    action: str              # "auth.login" / "access.granted" / "tool.denied"
    resource_type: str       # "agent" / "task" / "conversation"
    resource_id: str
    result: str              # "success" / "denied" / "error"
    detail: dict             # 附加上下文（如拒绝原因）
```

## Action 命名规范

```
auth.login.success
auth.login.failed
auth.token.invalid
access.agent.granted
access.agent.denied
agent.tool.granted
agent.tool.denied
admin.agent.created
admin.agent.deleted
admin.agent.permissions_changed
admin.assignment.created
admin.assignment.deleted
admin.user.role_changed
```

## 存储

使用数据库，不用文件。审计日志需要按租户、时间范围、操作者和操作类型查询。

```sql
CREATE TABLE audit_log (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL,
    timestamp     TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_type    TEXT NOT NULL,
    actor_id      TEXT NOT NULL,
    action        TEXT NOT NULL,
    resource_type TEXT,
    resource_id   TEXT,
    result        TEXT NOT NULL,
    detail        JSONB DEFAULT '{}',
);

CREATE INDEX idx_audit_tenant_time ON audit_log(tenant_id, timestamp DESC);
CREATE INDEX idx_audit_actor ON audit_log(actor_id, timestamp DESC);
```

## 模块结构

```
src/rolemesh/audit/
  types.py       # AuditEvent 数据类
  emitter.py     # emit_audit_event() — 被其他模块调用
  store.py       # 写入数据库
  query.py       # 查询接口（供 Admin API 使用）
```

## 设计决策

1. **独立模块** — 不属于 auth。任何模块都可以发出事件。
2. **可选** — 如果审计模块未初始化，emit 调用静默跳过。Auth 无需审计也能正常工作。
3. **只追加** — 审计日志永远不被应用代码更新或删除。保留策略另行处理。
4. **结构化** — 固定 Schema，JSONB detail 字段提供扩展性。不是自由文本。
5. **数据库存储** — 可按租户、操作者、时间范围查询。不使用文件。

## 未来考虑

- 日志保留策略（N 天后自动删除）
- 导出到外部 SIEM 系统
- 可疑模式实时告警（如重复的访问拒绝）
- WebUI 中的审计日志查看器
