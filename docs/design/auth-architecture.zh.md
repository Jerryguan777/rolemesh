# 认证与授权架构

> 状态：已批准的设计方案
> 读者：RoleMesh 的贡献者和集成方

## 概述

RoleMesh 支持两种部署场景：

1. **嵌入模式** — 作为现有 SaaS 系统的 AI Agent 子系统接入。用户认证由宿主 SaaS 处理；RoleMesh 只需理解"这个用户是谁"。
2. **独立模式** — 作为独立的 AaaS（Agent-as-a-Service）平台部署。RoleMesh 需要自行处理用户注册、登录和会话管理。

本文档描述了认证系统如何在不将业务逻辑与任何特定认证机制耦合的前提下，干净地支持这两种场景。

## 设计原则

1. **认证外置，授权内置。** 认证（你是谁？）委托给可插拔的 `IdentityProvider` 适配器。授权（你能做什么？）始终由 RoleMesh 自身的逻辑处理。

2. **用户权限与 Agent 权限完全独立。** 用户被授权"使用"Agent。Agent 被授权"执行"操作。这两项检查串行执行，互不交叉。运行时不做交集计算。

3. **分配即完全访问。** 一旦 Agent 被分配给用户，该用户即可使用该 Agent 的全部能力。如果不同用户需要不同的能力级别，则创建多个不同权限配置的 Agent，分别分配。

4. **权限保持精简。** 只有纯粹的授权决策（是/否判断）才放在权限模型中。资源限制（超时、并发）、工具绑定（MCP 服务器）、安全策略和限流由各自的模块管理——不在权限中重复。

5. **检查发生在边界，而非业务逻辑中。** 所有授权检查在拦截点（IPC 处理器、中间件）执行。业务逻辑代码中不包含任何权限检查。

6. **外部系统认证通过 Token 透传实现。** RoleMesh 不强制执行业务级权限（例如"该用户能否访问 SaaS 中的项目 X"）。相反，RoleMesh 通过委托令牌将用户身份传递给 MCP 服务器，由外部业务系统自行执行权限检查。

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│  外部（因部署模式而异）                                       │
│                                                             │
│  嵌入模式: SaaS JWT/Session  │  独立模式: RoleMesh JWT      │
└──────────────┬───────────────┴──────────────┬───────────────┘
               │                              │
               ▼                              ▼
      ExternalAuthProvider           BuiltinAuthProvider
               │                              │
               └──────────┬───────────────────┘
                          ▼
                  IdentityProvider (Protocol)
                  resolve(request) → ResolvedIdentity
                          │
                          ▼
               ┌─────────────────────┐
               │  RoleMesh 核心      │
               │  （两种模式共用      │
               │   同一套核心代码）    │
               └─────────────────────┘
```

## 身份契约

连接外部认证与内部逻辑的桥梁：

```python
@dataclass(frozen=True)
class ResolvedIdentity:
    tenant_id: str
    user_id: str
    role: str                         # owner / admin / member / viewer
    permissions: dict[str, Any]       # RoleMesh 特有的权限
    metadata: dict[str, str]          # 透传字段（邮箱、姓名等）
```

所有 RoleMesh 代码仅依赖此结构。任何代码都不直接检查原始 JWT 或会话令牌。

## 身份提供者

```python
class IdentityProvider(Protocol):
    async def resolve(self, request: Request) -> ResolvedIdentity | None:
        """从请求中提取并验证身份。失败返回 None。"""
        ...
```

### 嵌入模式：ExternalAuthProvider

验证宿主 SaaS 的 JWT/令牌，并使用声明式 JSON 映射配置将外部角色映射为 RoleMesh 角色。映射配置按租户存储在数据库中：

```json
{
  "role_map": {
    "saas:org-owner": "owner",
    "saas:admin": "admin",
    "saas:member": "member",
    "*": "viewer"
  },
  "permission_overrides": {
    "saas:admin": { "task:schedule": true }
  }
}
```

解析流程：验证 JWT → 提取外部角色 → 通过 `role_map` 映射 → 加载角色默认权限 → 应用 `permission_overrides` → 返回 ResolvedIdentity。接入新的 SaaS 只需编写映射配置，无需修改代码。

### 独立模式：BuiltinAuthProvider

验证 RoleMesh 自身签发的 JWT。角色和权限直接从 JWT claims 中读取。

## 用户权限

用户权限回答两个问题：

### 1. 该用户能在平台上管理什么？（角色）

| 角色 | 能力 |
|------|------|
| owner | 租户设置、账单、admin 的所有权限 |
| admin | 创建/编辑/删除 Agent，管理用户，分配 Agent |
| member | 使用已分配的 Agent，查看自己的对话历史 |
| viewer | 查看公开 Agent 列表，只读 |

### 2. 该用户可以与哪些 Agent 交互？（分配）

Agent 被视为资源。管理员将 Agent 分配给用户。已分配 = 可使用该 Agent 的全部能力。

- admin/owner：无需分配即可访问所有 Agent
- member：只能使用被显式分配的 Agent
- viewer：可以看到公开 Agent 但不能发消息

### Agent 可见性

Agent 有一个 `visibility` 字段（`public` 或 `restricted`）：

- `public` + 已分配 → 可见且可用
- `public` + 未分配 → 可见但不可用
- `restricted` + 未分配 → 对非管理员用户不可见
- admin/owner → 始终可见且可用

## Agent 权限（能力）

Agent 权限与用户权限完全独立。它们定义了 Agent 在 RoleMesh 平台内被允许做什么。

### Agent 角色

两个预定义角色，用作权限模板：

**super_agent**（替代旧的 `is_main=True`）：全局可见性，跨 Agent 管理能力。
**agent**（替代旧的 `is_main=False`）：仅限于自身数据和任务。

### 权限字段

| 权限 | super_agent | agent | 含义 |
|------|------------|-------|------|
| `data:scope` | `tenant` | `own` | RoleMesh 数据可见范围（任务、快照） |
| `task:schedule` | `true` | `false` | 能否创建定时任务 |
| `task:manage-others` | `true` | `false` | 能否管理其他 Agent 的任务 |
| `agent:delegate` | `true` | `false` | 能否调用其他 Agent |

角色设定默认值。每个 Agent 的权限可单独调整。

### 不属于 Agent 权限的内容

| 关注点 | 存放位置 | 原因 |
|--------|---------|------|
| max_concurrent | `coworkers.max_concurrent` | 资源配置 |
| timeout | `container_config.timeout` | 资源配置 |
| 哪些 MCP 服务器 | `coworkers.tools[]` | 工具绑定 |
| 网络/挂载限制 | 安全模块 | 安全策略 |
| 限流 | 凭证代理 | 运维防护 |
| @提及人类 | 不做控制 | Agent 根据上下文自行决定 |
| 跨对话发消息 | 不支持 | 所有 Agent 只在自己的对话中发消息 |

## 外部系统认证：Token 透传

当 Agent 调用 MCP 工具访问外部业务系统时，RoleMesh 不检查业务级权限。而是将用户身份透传过去。

### MCP 请求的双层 Header

- **服务认证**：证明请求来自 RoleMesh（已有的 `McpServerConfig.headers`）
- **用户委托令牌**：标识 Agent 代表哪个用户在操作

### 委托令牌

RoleMesh 签发短时效令牌（不转发原始 SaaS JWT，因为它可能在 Agent 执行期间过期）：
- 使用 RoleMesh 自己的密钥签名
- 包含：user_id、tenant_id、role
- 过期时间：匹配 Agent 执行超时
- MCP 服务器使用 RoleMesh 的公钥验签
- 遵循 OAuth 2.0 Token Exchange 模式（RFC 8693）

对于定时任务（没有活跃用户），委托令牌以任务创建者的身份签发。

## 完整授权流程

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
  │  失败 → 返回 PermissionDenied → Agent 告知用户
  │
  ⑤ 外部认证：MCP 工具收到委托令牌
  │  业务系统检查用户的业务权限
  │  失败 → 工具返回错误 → Agent 告知用户
  │
  ⑥ 成功
```

四层检查。每层独立。每层检查不同的关注点。

## 模块边界

```
Auth 模块          — 认证 + 用户→Agent 访问 + Agent 能力
Security 模块      — 网络、挂载、域名策略
Approval 模块      — 敏感操作的人工审批
限流器             — 凭证代理层的 MCP 调用频率限制
Config             — 资源限制（超时、并发）
```

五个关注点，五个模块。在拦截点组合。业务逻辑保持干净。
