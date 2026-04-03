# 认证与授权 — 完整实施指南

> 这是实施 RoleMesh 认证系统的唯一权威文档。
> 在编写任何代码之前，请从头到尾阅读本文档。

## 1. 我们要构建什么

RoleMesh 需要在两种部署模式下都能工作的认证和授权系统：

- **嵌入模式**：RoleMesh 接入现有 SaaS。用户已由 SaaS 完成认证。RoleMesh 接收 JWT/令牌，需要理解"这个用户是谁、能做什么"。
- **独立模式**：RoleMesh 作为独立 AaaS 平台运行。必须自行处理用户登录、注册和会话管理。

认证系统的设计必须确保同一套 RoleMesh 核心代码在两种模式下运行，唯一的区别是可插拔的身份提供者适配器。

## 2. 设计决策（已确定）

以下决策已最终确定。实施过程中不再重新讨论。

**认证外置，授权内置。** 认证由 IdentityProvider 适配器处理。授权逻辑始终在 RoleMesh 内部。

**用户权限与 Agent 权限完全独立。** 不做交集计算。用户被授权"使用"Agent。Agent 被授权"执行"操作。这两项检查串行执行，互不交叉引用。

**分配即完全访问。** 一旦 Agent 被分配给用户，该用户即可使用该 Agent 的全部能力。如果不同用户需要不同的能力级别，创建多个不同权限配置的 Agent，分别分配。

**权限保持精简。** 只有纯粹的授权决策（是/否）才放入权限模型。资源限制（超时、并发）、工具绑定（MCP 服务器）、安全策略和限流归各自模块管理。

**检查发生在边界。** 授权在拦截点（中间件、IPC 处理器、凭证代理）执行。业务逻辑代码中不包含任何权限检查。

**外部系统认证通过 Token 透传。** RoleMesh 不强制执行业务级权限（如"该用户能否访问项目 X"）。RoleMesh 将用户身份传递给 MCP 服务器，由外部业务系统自行执行权限检查。

**data:scope 只有两级：** `own` 和 `tenant`。没有 `team` 级别（RoleMesh 没有团队概念）。

## 3. 身份契约

所有 RoleMesh 内部代码依赖于一个结构。任何代码都不直接检查原始 JWT 或会话令牌。

```python
@dataclass(frozen=True)
class ResolvedIdentity:
    tenant_id: str
    user_id: str
    role: str                          # owner / admin / member / viewer
    permissions: dict[str, Any]        # RoleMesh 用户权限
    metadata: dict[str, str]           # 透传字段（邮箱、姓名等）
```

## 4. 身份提供者

```python
class IdentityProvider(Protocol):
    async def resolve(self, request: Request) -> ResolvedIdentity | None:
        ...
```

通过环境变量 `AUTH_PROVIDER` 选择：
- `builtin`（默认）— 独立模式，验证 RoleMesh 签发的 JWT
- `external` — 嵌入模式，验证 SaaS JWT 并映射角色

### 嵌入模式：角色映射

SaaS 角色与 RoleMesh 角色不匹配。通过声明式 JSON 映射配置（按租户存储在 `tenants` 表中）进行转换：

```json
{
  "role_map": {
    "saas:org-owner": "owner",
    "saas:admin": "admin",
    "saas:member": "member",
    "*": "viewer"
  },
  "permission_overrides": {
    "saas:admin": {
      "task:schedule": true
    }
  }
}
```

解析流程：验证 JWT → 提取外部角色 → 通过 `role_map` 映射为 RoleMesh 角色 → 加载角色默认权限 → 应用 `permission_overrides` → 返回 ResolvedIdentity。

映射是配置而非代码。接入新的 SaaS 意味着编写 JSON 映射，而非修改源代码。

## 5. 用户权限

RoleMesh 中的用户权限很简单：

**角色**（owner / admin / member / viewer）— 决定平台管理能力：
- owner：租户设置、账单、admin 的所有权限
- admin：创建/编辑/删除 Agent，管理用户，分配 Agent，配置系统
- member：使用已分配的 Agent，查看自己的对话历史
- viewer：查看公开 Agent 列表，只读访问

**Agent 分配** — 决定用户可以与哪些 Agent 交互：
- admin/owner：无需分配即可访问所有 Agent
- member：只能使用被显式分配的 Agent
- viewer：可以看到公开 Agent 但不能发消息

实现方式：`user_agent_assignments` 表（user_id, coworker_id）。已分配 = 可使用。未分配 = 不可使用（admin/owner 除外）。

### Agent 可见性

Agent 有 `visibility` 字段（`public` 或 `restricted`）：
- `public` + 已分配 → 可见且可用
- `public` + 未分配 → 可见但不可用（member 可以看到它的存在）
- `restricted` + 未分配 → 对非管理员用户不可见
- admin/owner → 始终可见且可用

## 6. Agent 权限（能力）

### Agent 角色

Agent 有两个预定义角色。角色是设置默认权限值的模板：

**super_agent** — 替代旧的 `is_main=True`。全局可见性，跨 Agent 管理能力。

**agent** — 替代旧的 `is_main=False`。仅限于自身数据和任务。

角色存储在 `coworkers.agent_role`。权限存储在 `coworkers.permissions`（JSONB）。选择角色会自动填充默认值，但可以对个别权限做单独调整。

### 权限字段（共 4 个）

| 权限 | super_agent 默认值 | agent 默认值 | 含义 |
|------|------------------|-------------|------|
| `data:scope` | `tenant` | `own` | Agent 能看到哪些 RoleMesh 数据（任务、Group 快照） |
| `task:schedule` | `true` | `false` | 能否创建定时任务 |
| `task:manage-others` | `true` | `false` | 能否管理其他 Agent 的任务（暂停/恢复/取消/修改） |
| `agent:delegate` | `true` | `false` | 能否调用其他 Agent |

这些在 `task_handler.py` 的 IPC 拦截点执行检查。Agent 的业务逻辑（LLM 执行）不感知这些检查。

### 不属于 Agent 权限的内容

| 关注点 | 存放位置 | 原因 |
|--------|---------|------|
| max_concurrent | `coworkers.max_concurrent` | 资源配置，非授权 |
| timeout | `container_config.timeout` | 资源配置 |
| 哪些 MCP 服务器 | `coworkers.tools[]` | 工具绑定配置 |
| 网络/挂载限制 | 安全模块 | 安全策略 |
| 限流 | 凭证代理 | 运维防护 |
| @提及人类 | 不做控制 | Agent 根据上下文自行决定 |
| 跨对话发消息 | 不支持 | 所有 Agent 只在已分配的对话中发消息 |

## 7. is_main 迁移处理

`is_main` 布尔字段完全由 `agent_role` + `permissions` 替代。以下是每个 `is_main` 相关功能的处理方式：

### 需要删除的功能（不再需要）

| 功能 | 当前代码位置 | 操作 |
|------|------------|------|
| 对话管理（register_conversation, refresh_conversations） | task_handler.py, ipc_mcp.py | 移除 MCP 工具。未来：admin API |
| 远程控制 | main.py | 移除。系统管理通过 admin API |
| 项目根目录特殊挂载 | runner.py:91-108 | 移除。使用现有 `additional_mounts` 配置 |
| 触发机制豁免 | main.py:337-347 | 已由 conversation 的 `requires_trigger` 字段控制，与 is_main 解耦 |
| 跨对话发消息（message:send=all） | main.py:575-593 | 移除。所有 Agent 只在自己的对话中发消息 |

对于这些功能，按 `is_main=False` 简化代码，然后删除废弃的代码分支。

### 需要替换为权限检查的功能

| 当前代码模式 | 替换为 |
|------------|--------|
| `if is_main:` 允许跨 coworker 创建任务 | `if permissions["task:schedule"]:` |
| `if is_main:` 允许跨 coworker 暂停/恢复/取消/修改任务 | `if permissions["task:manage-others"]:` |
| `if is_main:` 显示所有任务/Group 快照 | `if permissions["data:scope"] == "tenant":` |
| `if is_main:` 允许委托给其他 Agent | `if permissions["agent:delegate"]:` |
| `if is_main:` 允许读写额外挂载 | 移除特殊情况，使用 mount_security 正常校验 |

### 数据迁移

```sql
ALTER TABLE coworkers ADD COLUMN agent_role TEXT NOT NULL DEFAULT 'agent';
ALTER TABLE coworkers ADD COLUMN permissions JSONB NOT NULL DEFAULT
  '{"data:scope":"own","task:schedule":false,"task:manage-others":false,"agent:delegate":false}';

UPDATE coworkers SET
  agent_role = 'super_agent',
  permissions = '{"data:scope":"tenant","task:schedule":true,"task:manage-others":true,"agent:delegate":true}'
WHERE is_main = true;

ALTER TABLE coworkers DROP COLUMN is_main;
```

### IPC 协议变更

在 `AgentInitData`（ipc/protocol.py）中：将 `is_main: bool` 替换为 `permissions: dict[str, Any]`。Agent Runner 从此字典读取权限，而非检查布尔标志。

## 8. 用户身份透传到 MCP 服务器

当 Agent 调用访问外部业务系统的 MCP 工具时，RoleMesh 不检查业务级权限。而是将用户身份传递给 MCP 服务器，由业务系统自行执行权限检查。

### 双层 Header

凭证代理向 MCP 请求注入两种 Header：

- **服务认证**（`X-Service-Auth` 或配置的 `McpServerConfig.headers`）：证明请求来自 RoleMesh。这是现有机制。
- **用户身份**（`X-User-Token`）：代表 Agent 所代表的用户的委托令牌。

### 委托令牌

RoleMesh 不转发原始 SaaS JWT（它可能在 Agent 执行期间过期）。相反，Orchestrator 签发短时效的委托令牌：

- 使用 RoleMesh 自己的密钥签名
- 包含：user_id、tenant_id、role 及相关元数据
- 过期时间 = Agent 执行超时（如 10 分钟）
- MCP 服务器使用 RoleMesh 的公钥验证签名
- 遵循 OAuth 2.0 Token Exchange 模式（RFC 8693）

### 流程

1. 用户携带 SaaS JWT 发送请求
2. Orchestrator 验证 SaaS JWT，提取身份
3. Orchestrator 签发包含用户身份的委托令牌
4. 委托令牌通过 AgentInitData 传入容器
5. Agent 调用 MCP 工具时，凭证代理同时注入服务认证 Header 和委托令牌
6. MCP 服务器验证 RoleMesh 签名（可信来源），读取用户身份，检查业务权限

### 定时任务（无在线用户）

定时任务在没有活跃用户会话的情况下运行。创建任务时记录 `created_by` user_id。执行时，以任务创建者的身份签发委托令牌。

## 9. 完整授权流程

```
用户发送消息
  │
  ① 认证：IdentityProvider.resolve() → ResolvedIdentity
  │  失败 → 401
  │
  ② 用户访问检查：该 Agent 是否已分配给该用户？
  │  （admin/owner 跳过检查）
  │  失败 → 403
  │
  ③ Agent 执行：LLM 处理消息，决定调用哪些工具
  │
  ④ Agent 能力检查：该 Agent 是否有此操作的权限？
  │  在 IPC task_handler 拦截点检查
  │  失败 → 返回 PermissionDenied 给 Agent → Agent 告知用户
  │
  ⑤ 外部认证：MCP 工具收到委托令牌
  │  业务系统检查用户的业务级权限
  │  失败 → 工具返回错误 → Agent 告知用户
  │
  ⑥ 成功
```

四层检查，每层独立，每层检查不同的关注点。

## 10. 数据库 Schema 变更

### 新表：user_agent_assignments

```sql
CREATE TABLE user_agent_assignments (
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    user_id      UUID NOT NULL REFERENCES users(id),
    coworker_id  UUID NOT NULL REFERENCES coworkers(id),
    assigned_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (user_id, coworker_id)
);
```

### coworkers 表变更

```sql
ALTER TABLE coworkers ADD COLUMN agent_role TEXT NOT NULL DEFAULT 'agent';
ALTER TABLE coworkers ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public';
ALTER TABLE coworkers ADD COLUMN permissions JSONB NOT NULL DEFAULT
  '{"data:scope":"own","task:schedule":false,"task:manage-others":false,"agent:delegate":false}';
ALTER TABLE coworkers DROP COLUMN is_main;
```

### tenants 表变更

```sql
ALTER TABLE tenants ADD COLUMN auth_mapping_config JSONB DEFAULT '{}';
```

## 11. 新模块结构

```
src/rolemesh/auth/
  __init__.py
  types.py                # ResolvedIdentity, IdentityProvider Protocol
  permissions.py           # 角色默认值，Agent 角色默认值，检查辅助函数
  middleware.py            # 请求级认证拦截
  builtin_provider.py      # 独立模式：验证 RoleMesh JWT
  external_provider.py     # 嵌入模式：验证 SaaS JWT + 角色映射
  mapping.py               # 声明式角色/权限映射引擎
  delegation.py            # 签发/验证 MCP 透传的委托令牌
```

## 12. 与现有代码的集成点

### main.py
- 启动时：根据 AUTH_PROVIDER 环境变量创建身份提供者
- 在 `_handle_incoming` 中：解析身份 → 检查用户-Agent 访问权限 → 通过或拒绝

### core/types.py
- Coworker：添加 `agent_role`、`visibility`、`permissions` 字段
- 移除 `is_main` 字段（迁移完成后）

### db/pg.py
- 添加新列和新表（参见上述 Schema 变更）
- 更新 `_record_to_coworker()` 以反序列化新字段
- 添加 user_agent_assignments 的 CRUD 函数

### ipc/protocol.py
- AgentInitData：将 `is_main: bool` 替换为 `permissions: dict`
- 添加 `user_delegation_token: str | None` 字段

### ipc/task_handler.py
- 将每个 `if is_main:` 替换为对应的 `permissions[key]` 检查
- 删除 register_conversation 和 refresh_conversations 的处理器

### agent/container_executor.py
- 将委托令牌传入 AgentInitData
- 移除 is_main 相关逻辑

### container/runner.py
- 移除 is_main 的项目根目录特殊挂载
- 移除 is_main 的 Group 快照可见性逻辑，改用 permissions["data:scope"]

### agent_runner/main.py
- 从 AgentInitData 读取 permissions 而非 is_main 标志
- 调用 MCP 服务器时将委托令牌作为 Header 传递

### security/credential_proxy.py
- 在 MCP 代理请求中添加委托令牌注入（与现有服务认证 Header 并列）

## 13. 实施阶段

### 阶段 1：基础设施（非破坏性）
- 创建 `auth/` 模块：类型、权限、映射引擎
- 添加带默认值的数据库列（现有行为不变）
- 添加 user_agent_assignments 表（空表 = 不限制访问，向后兼容）
- 添加委托令牌的签发/验证功能

### 阶段 2：身份提供者
- 实现 BuiltinAuthProvider
- 实现 ExternalAuthProvider
- 将中间件接入 WebUI 和 API 端点
- 空分配表 = 全部可访问（向后兼容）

### 阶段 3：Agent 角色迁移
- 将 task_handler.py 中的 is_main 检查替换为权限检查
- 将 runner.py 中的 is_main 检查替换（快照、挂载）
- 更新 IPC 协议（is_main → permissions）
- 数据迁移：is_main=true → super_agent，is_main=false → agent
- 删除 is_main 列

### 阶段 4：清理
- 删除已移除的功能（register_conversation、refresh_conversations 工具、远程控制）
- 删除 is_main 相关的废弃代码路径
- 更新所有测试

## 14. 测试

### 新增测试
- `tests/auth/test_types.py` — ResolvedIdentity 辅助方法
- `tests/auth/test_permissions.py` — 角色默认值、Agent 角色默认值
- `tests/auth/test_mapping.py` — 映射引擎（角色解析、覆盖、通配符、回退）
- `tests/auth/test_middleware.py` — 使用 mock 提供者的认证中间件
- `tests/auth/test_delegation.py` — 委托令牌签发/验证/过期
- `tests/auth/test_external_provider.py` — 使用测试密钥的 JWT 验证
- `tests/auth/test_builtin_provider.py` — RoleMesh JWT 验证
- `tests/auth/test_access_check.py` — 用户-Agent 分配查询

### 更新的测试
- `tests/ipc/test_task_handler.py` — 验证权限检查替代 is_main 检查
- `tests/test_e2e.py` — 在端到端测试中添加认证流程
- `tests/container/test_runner.py` — 验证无 is_main 特殊情况的挂载逻辑

## 15. 不在范围内（未来工作）

以下内容明确不属于本次实施：

- **审批模块** — 敏感工具调用的人工审批流程（参见 `docs/design/approval-module.md`）
- **对话管理 Admin API** — 替代被移除的 register_conversation Agent 工具
- **内置登录/注册 UI** — 仅独立模式需要，单独开发
- **安全模块** — 网络策略、挂载限制、域名白名单
- **限流** — 凭证代理层的 MCP 调用频率限制
- **审计日志** — 记录所有认证决策以满足合规要求
