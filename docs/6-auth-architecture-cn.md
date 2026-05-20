# 认证与授权架构

本文档描述 RoleMesh 如何对用户进行认证、对操作进行授权，以及如何在 agent 执行管线中传递身份信息。文中涵盖了塑造系统的设计约束、我们考虑过的备选方案，以及当前架构为何呈现出现在的样子。

## 问题背景

RoleMesh 最初只有一个布尔字段：`is_main`。Admin Coworker 可以做任何事情——查看所有任务、管理其他 agent 的调度、注册新会话、访问项目根文件系统。非 admin 的 Coworker 在自身范围之外什么也做不了。

这种设计在单用户部署下是可行的。但当我们需要以下能力时，它就崩溃了：

1. **多租户隔离** —— 不同组织共享同一个 RoleMesh 实例
2. **细粒度的 agent 能力** —— 一个 agent 可以调度任务但不能调用其他 agent
3. **用户级访问控制** —— 谁能使用哪些 agent，谁能管理平台
4. **三种部署模式** —— 嵌入到既有 SaaS 中、独立平台、或与 OIDC 集成
5. **安全的身份转发** —— 当一个 agent 代表某个用户调用外部 MCP 服务器时，MCP 服务器需要知道是 _哪个_ 用户，并拿到一份新鲜的、由 IdP 签发的 token

## 设计原则

指导 RoleMesh 中所有 auth 决策的五条规则：

**1. AuthN 在外部，AuthZ 在内部。** 认证（"你是谁？"）委托给可插拔的 provider。授权（"你能做什么？"）始终是 RoleMesh 自身的逻辑。任何业务代码都不会去检查原始 JWT。

**2. 检查发生在边界处，而不在业务逻辑中。** 授权恰好在四个拦截点上执行。orchestration 代码、容器运行器、agent SDK 集成层中包含零权限检查。

**3. 权限要保持轻薄。** 只有纯粹的 yes/no 授权决策才放在权限模型里。资源限制（超时、并发数）、工具绑定（MCP 服务器）、安全策略（挂载白名单）、限流，都属于各自独立的模块。

**4. 用户权限和 agent 权限互相独立。** 用户被授权 _使用_ agent。agent 被授权 _执行_ 操作。这两次检查串行进行，绝不交叉。这就避免了用户 × agent 权限矩阵的组合爆炸。

**5. 分配 = 完整访问。** 一旦把某个 agent 分配给某用户，该用户就可以使用这个 agent 的所有能力。如果不同用户需要不同的能力级别，那就创建多个 agent，使用不同的权限配置，分别分配给他们。这能让模型保持简单。

## 架构总览

```
                     ┌──────────────────────────────┐
                     │      External Auth            │
                     │                               │
                     │  External: SaaS JWT           │
                     │  Builtin: RoleMesh JWT        │
                     │  OIDC: IdP id_token (PKCE)    │
                     └──────────────┬────────────────┘
                                    │
                                    ▼
                           AuthProvider (Protocol)
                           authenticate(token) → AuthenticatedUser
                                    │
                     ┌──────────────┼──────────────────┐
                     │              │                   │
                     ▼              ▼                   ▼
            ExternalJwtProvider  BuiltinProvider     OIDCAuthProvider
              (validates SaaS   (stub, not yet      (JWKS validation,
               JWT, maps        implemented)         claim mapping,
               claims)                               JIT provisioning)
                     │                                  │
                     └──────────┬───────────────────────┘
                                ▼
              ┌─────────────────────────────────────┐
              │  RoleMesh Core (same for all modes) │
              │                                     │
              │  User role check (owner/admin/member)│
              │  Agent assignment check              │
              │  Agent permissions check             │
              │  MCP token forwarding (TokenVault)   │
              └─────────────────────────────────────┘
```

## 三种部署模式

### External 模式

RoleMesh 运行在既有的 SaaS 内部。用户通过该 SaaS 进行认证，由它签发 JWT。RoleMesh 验证这些 JWT 并从中提取身份。

通过环境变量进行配置：

```
AUTH_MODE=external
EXTERNAL_JWT_SECRET=<symmetric-secret>          # or EXTERNAL_JWT_PUBLIC_KEY for RS256
EXTERNAL_JWT_ISSUER=https://auth.your-saas.com  # optional
EXTERNAL_JWT_ALGORITHMS=HS256                   # comma-separated
EXTERNAL_JWT_CLAIM_USER_ID=sub                  # claim name mapping
EXTERNAL_JWT_CLAIM_TENANT_ID=tid
EXTERNAL_JWT_CLAIM_ROLE=role
```

claim 映射是配置，不是代码。要接入一个新的 SaaS，只需设置环境变量，而不是去写适配器。

### OIDC 模式（当前重点）

RoleMesh 可以连接到任何符合 OIDC 规范的 IdP（Okta、Azure AD、Keycloak、Auth0），并通过 PKCE 处理完整的浏览器登录流程。用户在首次登录时会被 JIT 创建。

配置：

```
AUTH_MODE=oidc
OIDC_DISCOVERY_URL=https://idp.example.com/.well-known/openid-configuration
OIDC_CLIENT_ID=rolemesh
OIDC_CLIENT_SECRET=                             # optional (public clients)
OIDC_AUDIENCE=rolemesh                          # defaults to client_id
OIDC_SCOPES=openid profile email offline_access
OIDC_REDIRECT_URI=https://app.example.com/oauth2/callback

# Claim mapping for role resolution (all optional)
OIDC_CLAIM_ROLE=role                            # direct role claim
OIDC_CLAIM_GROUPS=groups                        # group membership claim
OIDC_GROUP_ROLE_MAP={"FirmAdministrators":"admin","Developers":"member"}
OIDC_SCOPE_ROLE_MAP={"admin:rolemesh":"admin"}  # fallback scope mapping
OIDC_CLAIM_TENANT_ID=tid                        # multi-tenant claim

# Auto-assignment
OIDC_AUTO_ASSIGN_TO_ALL=true                    # new users get all coworkers

# Token vault for MCP forwarding
ROLEMESH_TOKEN_SECRET=<any-secret>              # Fernet encryption key derivation
```

OIDC 模式下角色解析的优先级：直接角色 claim → 分组映射 → 作用域映射 → 兜底为 "member"。

### Builtin 模式（占位实现）

`BuiltinProvider` 目前作为占位存在。一旦实现，它会负责用户注册、登录、密码哈希（bcrypt）以及 JWT 签发。`users.password_hash` 列已经在 schema 中预留。

## OIDC 架构

### 子包：`src/rolemesh/auth/oidc/`

OIDC 实现被拆分为若干职责聚焦的模块：

| 文件 | 用途 |
|------|---------|
| `config.py` | `OIDCConfig` frozen dataclass + `from_env()`。聚合所有 IdP 级别的配置。Cookie 相关变量被排除（仅 webui 使用）。 |
| `discovery.py` | `DiscoveryDocument` dataclass（issuer、各 endpoint、jwks_uri）。 |
| `jwks.py` | `JWKSManager` —— 异步 JWKS 拉取 + 缓存，并处理密钥轮换。使用 `asyncio.Lock`。 |
| `algorithms.py` | `ALLOWED_ALGORITHMS` —— 8 种 JWT 算法的白名单，用于防御算法混淆攻击。 |
| `adapter.py` | `OIDCAdapter` Protocol + `DefaultOIDCAdapter`，提供可插拔的 claim 映射。 |
| `provider.py` | `OIDCAuthProvider` —— 主 provider：id_token 验证、JIT 租户/用户创建、自动分配。 |

### 登录流程

浏览器对接 IdP 走标准的 OIDC PKCE 流程，把得到的授权码与 WebUI 交换，拿到一个 `id_token`（以及一份配套的 httpOnly refresh cookie），并在过期前静默刷新。完整的用户体验（SPA 按何种顺序调用哪些 endpoint，`sessionStorage` 与 refresh cookie 如何配合，静默刷新时发生了什么）在 [`5-webui-architecture.md`](5-webui-architecture.md) 中——那才是拥有浏览器和 FastAPI 路由处理器的那一层。

无论由谁调用，`OIDCAuthProvider` 都负责：

- 用 IdP 的 JWKS 验证 `id_token` 的签名（并处理密钥轮换）。
- 验证 `iss`、`aud`、`exp`，并拒绝任何不在 `ALLOWED_ALGORITHMS` 中的算法（防御算法混淆攻击）。
- 通过 `OIDCAdapter` 把 claim 映射到一个租户/用户（见下文）。
- 首次见到时 JIT 创建租户和用户；把 IdP 签发的 refresh / access token 镜像到该用户的 vault 中，供下游 MCP 调用使用。

### JIT 创建

OIDC 首次登录时：

1. **租户解析**：`OIDCAdapter.map_tenant_id(claims)` 提取外部租户 ID。如果为空 → 单租户模式 → 使用 `default` 租户。否则 → 在 `external_tenant_map` 中查找 → 找不到则 JIT 创建新租户。
2. **用户创建**：按 `external_sub` 查找。找不到则 → `create_user_with_external_sub()` → 调用 `OIDCAdapter.on_user_provisioned()` hook。
3. **自动分配**（当 `OIDC_AUTO_ASSIGN_TO_ALL=true` 时）：新用户会被分配给该租户内所有 Coworker。已有用户在登录时 _不会_ 被重新分配（admin 可能是有意取消分配的）。

后续登录时：从 claim 同步可变字段（姓名、邮箱、角色）。

### OIDCAdapter 协议

可以通过 `OIDC_ADAPTER=module.path.ClassName` 接入针对特定 IdP 的自定义 claim 映射：

```python
class OIDCAdapter(Protocol):
    def map_role(self, claims: dict[str, Any]) -> str: ...
    def map_tenant_id(self, claims: dict[str, Any]) -> str: ...
    async def on_tenant_provisioned(self, tenant_id: str, claims: dict[str, Any]) -> None: ...
    async def on_user_provisioned(self, user_id: str, tenant_id: str, claims: dict[str, Any]) -> None: ...
```

`DefaultOIDCAdapter` 通过环境变量支持三种角色映射策略：

- **直接 claim**：`OIDC_CLAIM_ROLE` —— 取值必须是 owner/admin/member 之一
- **分组映射**：`OIDC_CLAIM_GROUPS` + `OIDC_GROUP_ROLE_MAP`（JSON 字典）
- **作用域映射**：`OIDC_SCOPE_ROLE_MAP`（JSON 字典）

非法的角色映射值会在启动时被 `logger.error` 拒绝；运行时未匹配上的分组/作用域会输出 `logger.warning`。

## 用户角色

只有三种角色，刻意保持极简：

| 角色 | 管理租户 | 管理 agent | 管理用户 | 查看所有会话 | 使用 agent |
|------|:---:|:---:|:---:|:---:|:---:|
| **owner** | 是 | 是 | 是 | 是 | 是 |
| **admin** | — | 是 | 是 | 是 | 是 |
| **member** | — | — | — | — | 仅限被分配的 |

实现：`src/rolemesh/auth/permissions.py` 中的 `user_can(role, action) -> bool`。一张查表，而不是规则引擎。

### 用户-Agent 分配

`user_agent_assignments` 表把用户映射到 Coworker。member 角色的用户只能看到并使用被显式分配给自己的 agent。admin/owner 会绕过分配检查。

```sql
CREATE TABLE user_agent_assignments (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    coworker_id UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    UNIQUE (user_id, coworker_id)
);
```

CRUD 实现位于 `src/rolemesh/db/user.py`（`assign_agent_to_user()`、`unassign_agent_from_user()`、`get_agents_for_user()`、`get_users_for_agent()`）。

## Agent 权限

### 为什么不直接用角色？

我们考虑过三种 agent 授权方案：

| 方案 | 优点 | 缺点 |
|----------|------|------|
| **单一布尔值**（`is_main`） | 简单 | 全有或全无。无法表达"能调度任务但不能管理别人任务"这类 agent。 |
| **完整 RBAC**（角色 + 权限 + 资源） | 灵活性最大化 | 对仅有 4 项能力来说严重过度设计。组合爆炸。难以推理。 |
| **角色作为模板 + 平铺覆盖** | 易于理解，覆盖真实用例，无抽象开销 | 无法表达深度嵌套的策略（但我们也不需要） |

我们选择了 **角色作为模板 + 平铺覆盖**。一个 agent 拥有一个角色（`super_agent` 或 `agent`），它会填入默认权限。每个 agent 的具体权限可以单独覆盖。

历史遗留的 `is_admin` 列已被完全移除。唯一的权威字段是 `agent_role` + 权限 JSONB。

### 四个权限字段

```python
@dataclass(frozen=True)
class AgentPermissions:
    data_scope: Literal["tenant", "self"] = "self"
    task_schedule: bool = False
    task_manage_others: bool = False
    agent_delegate: bool = False
```

| 权限 | `super_agent` 默认值 | `agent` 默认值 | 控制对象 |
|-----------|:---:|:---:|---|
| `data_scope` | `tenant` | `self` | 任务/快照可见性。`tenant` = 看到所有 Coworker 的数据。`self` = 仅自己的。同时控制项目根的挂载。 |
| `task_schedule` | `true` | `false` | 是否能创建调度任务（cron、interval、once）。 |
| `task_manage_others` | `true` | `false` | 是否能 暂停/恢复/取消/更新 其他 agent 的任务。 |
| `agent_delegate` | `true` | `false` | 是否能调用其他 agent（为将来的多 agent 编排预留）。 |

### Agent 权限中 _不_ 包含的内容

这一点的重要程度至少不亚于"包含什么"：保持权限轻薄（设计原则 3）意味着资源和工具相关的关注点应当属于别处。

| 关注点 | 归属位置 | 为何不放进权限里 |
|---------|---------------|-------------------|
| 最大并发容器数 | `coworkers.max_concurrent` | 资源限制，不是授权 |
| 容器超时 | `container_config.timeout` | 资源限制 |
| 可用的 MCP 服务器有哪些 | `coworkers.tools[]` | 工具绑定，与 auth 正交 |
| 挂载限制 | `mount_security.py` 加外部白名单 | 安全策略，不是能力 |
| 限流 | 凭据代理 | 运维层防护 |
| 跨会话发消息 | 对所有 agent 都已移除 | 架构决策，而非按 agent 配置 |

### 存储和 IPC 契约

权限作为一个 JSONB 列存储在 `coworkers` 表中，与 `agent_role` 并列。它们通过 `AgentInitData`（NATS KV bootstrap 载荷——见 [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md)）流入正在运行的 agent。IPC 契约一句话即可概括：**载荷只携带 `tenantId + coworkerId`，但永不携带权限本身；orchestrator 在响应任何 Channel 4 / Channel 5 请求之前，会查找该 Coworker 的权威权限**，所以 agent 无法通过编辑载荷来提权。

历史遗留的 `is_main: true/false` 字段在反序列化时仍被接受（会被转换为等价的 `AgentPermissions` 模板），这样在滚动发布时较旧的容器也能完成 bootstrap。

## MCP Token 转发：TokenVault

### 问题

当用户要求一个 agent 通过 MCP 访问外部数据时，MCP 服务器需要知道 _是哪个用户_ 在发起请求。简单的 token 转发行不通，因为 agent 可能运行 30 分钟以上，超过了 IdP token 通常 1 小时的 TTL。

### 解决方案：按用户的服务端 Token Vault

我们不签发 RoleMesh 自己的 JWT（MCP 服务器没有 RoleMesh 的密钥也无法验证），而是直接转发 IdP 自身的 access token——MCP 服务器本就通过 OIDC discovery 信任这些 token。

```
Login (once):
  Browser → /api/auth/exchange → backend gets id_token + refresh_token
  Backend stores (encrypted) in oidc_user_tokens; sets httpOnly refresh cookie

Agent execution (many MCP calls):
  Container → MCP request via credential proxy with X-RoleMesh-User-Id
  Credential proxy:
    1. Look up cached access_token for this user
    2. If close to expiry → refresh against IdP, persist new tokens
    3. Inject Authorization: Bearer <fresh access_token>
    4. Forward to MCP server
  MCP server validates the access_token via OIDC discovery (standard flow)
```

**位置**：`src/rolemesh/auth/token_vault.py`。该 vault 在落盘时加密 refresh token，按用户去重并发刷新，并在 IdP 颁发新 token 时处理 refresh-token 轮换。详细机制（加密方案的选型、锁的粒度、阈值调优）属于该模块内部的实现细节——不影响契约。

### MCP 服务器认证模式

每个 MCP 服务器都可以配置一个 `auth_mode`：

| auth_mode | 每服务器的 header | 用户 token | 适用场景 |
|-----------|:---:|:---:|---|
| `user`（默认） | 注入，但 `Authorization` 会被用户 token 覆盖 | ✓ | 支持 OIDC 的 MCP 服务器 |
| `service` | 完整注入（包含管理员设置的 `Authorization`） | ✗ | 服务到服务 / 旧式 MCP |
| `both` | 注入 + 通过 `X-User-Authorization` header 携带用户 token | ✓ | 双层校验 |

token 如何接入到具体的 MCP 服务器（`AgentInitData.mcp_servers` 中的 proxy URL、宿主侧的 `Authorization` 改写）在 [`external-mcp-architecture.md`](external-mcp-architecture.md) 中有详细描述。

## 授权执行：四个拦截点

所有授权都恰好发生在四个地方。业务逻辑保持干净。

### 1. WebUI / HTTP 中间件

`src/webui/auth.py` 对每个 REST 和 WebSocket 处理器，通过 `AuthProvider.authenticate()` 验证请求 token。`ADMIN_BOOTSTRAP_TOKEN` 快捷通道和 OIDC PKCE 流程都在此处接入。各种表面细节（涉及哪些路径、刷新如何处理、`?token=` 查询参数的语义）见 [`5-webui-architecture.md`](5-webui-architecture.md)。

### 2. IPC 任务处理器

agent 能力的核心强制点。每一次任务 IPC 请求都会经过 `src/rolemesh/ipc/task_handler.py` 中的 `process_task_ipc()`，并携带 agent 的 `AgentPermissions`：

```python
async def process_task_ipc(
    data: dict,
    source_group: str,
    permissions: AgentPermissions,   # <-- authorization context
    deps: IpcDeps,
    tenant_id: str,
    coworker_id: str,
) -> None:
```

授权检查使用 `src/rolemesh/auth/authorization.py` 中的纯函数：

```python
if not can_schedule_task(permissions):
    return  # blocked

if not can_manage_task(permissions, task.coworker_id, self_coworker_id):
    return  # blocked
```

这些函数没有副作用，不访问 DB，不打日志。它们返回 `bool`。这让它们的单元测试轻而易举，推理也轻而易举：相同的输入永远得到相同的授权结论。

### 3. IPC 消息处理器

在 orchestrator 的消息分发路径上：所有 agent 只能向自己的会话发送消息。这里没有 admin 后门——即便是 `super_agent` 的 `data_scope=tenant` 也不会解锁跨会话发消息，因为这是一项架构选择，而不是一项权限（参见 "Agent 权限中 _不_ 包含的内容"）。

### 4. 容器构建器

`src/rolemesh/container/runner.py:build_volume_mounts()` 通过 `data_scope` 来管控 volume 挂载和快照可见性：

```python
def build_volume_mounts(coworker, tenant_id, conversation_id, permissions=None):
    if permissions.data_scope == "tenant":
        mounts.append(VolumeMount("/workspace/project", readonly=True))
```

`data_scope="self"` 的 agent 永远看不到项目根目录，也看不到其他 agent 在快照中的任务——orchestrator 会预先过滤 Channel 6 的快照，因此即便有缺陷的 `list_tasks` 调用也读不到其他租户的数据。

## 权限传递

权限从 `coworkers.permissions`（DB）经由 `AgentInitData`（NATS KV）流入容器，agent_runner 在容器内把它们读取为一个普通的 `dict[str, object]`，然后把这个 dict 传给 IPC 工具的 gating 层。

IPC 线上格式是 `dict` 而不是 `AgentPermissions` dataclass，这是有意为之：agent_runner 运行在一个 Python 依赖被刻意精简的 Docker 容器内，因此容器一侧可以直接 `permissions.get("task_schedule")`，而不必导入 dataclass 模块。该 dataclass 在宿主侧使用（那里更适合丰富的类型表达），并在 IPC 边界用 `to_dict()` 转换。

完整的 IPC 载荷契约——包括 `tenantId` / `coworkerId` 是如何由 agent_runner（而非 LLM）设置、并由 orchestrator 重新校验的——在 [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md) 中。

## 数据库 Schema

| 表 | 用途 |
|-------|---------|
| `users` | 用户账号（本地 ID、用于 OIDC 的 `external_sub`、role、用于 builtin 模式的 `password_hash`） |
| `coworkers` | agent 定义（`agent_role`、`permissions` JSONB、tools、container_config） |
| `user_agent_assignments` | 用户 ↔ Coworker 的多对多映射 |
| `external_tenant_map` | 把 `(provider, external_tenant_id) → local tenant_id` 映射，用于 OIDC 多租户 |
| `oidc_user_tokens` | 为 TokenVault 加密存储每用户的 refresh_token + 缓存的 access_token |

Schema 迁移机制（`is_admin → agent_role` 的回填、默认租户的创建、幂等的 `_create_schema()` 形态）在 [`4-multi-tenant-architecture.md`](4-multi-tenant-architecture.md) 中描述。

## Admin API

`src/webui/admin.py` 在 `/api/admin/` 下暴露 RESTful endpoint，用于租户、用户、agent、绑定、会话和任务的管理——由 `ADMIN_BOOTSTRAP_TOKEN` 或用户角色检查保护。完整接口表面（存在哪些 endpoint、各由哪个模块拥有）记录在 [`5-webui-architecture.md`](5-webui-architecture.md) 的 "Beyond chat: Admin surface" 一节。

## 文件清单

| 文件 | 用途 |
|------|---------|
| `src/rolemesh/auth/permissions.py` | `AgentPermissions`、`AgentRole`、`UserRole`、`user_can()` |
| `src/rolemesh/auth/authorization.py` | 纯 auth 函数：`can_schedule_task()`、`can_manage_task()`、`can_see_data()`、`can_delegate()` |
| `src/rolemesh/auth/provider.py` | `AuthProvider` 协议、`AuthenticatedUser` dataclass |
| `src/rolemesh/auth/external_jwt_provider.py` | 验证外部 SaaS JWT |
| `src/rolemesh/auth/builtin_provider.py` | 未来 builtin auth 的占位 |
| `src/rolemesh/auth/factory.py` | `create_auth_provider(mode)` 工厂 |
| `src/rolemesh/auth/token_vault.py` | 按用户加密的 token 存储，附带自动 IdP 刷新 |
| `src/rolemesh/auth/oidc/{config,discovery,jwks,algorithms,adapter,provider}.py` | OIDC 子模块（见上文 "子包" 表格） |
| `src/rolemesh/db/user.py`、`db/coworker.py`、… | 按实体的 CRUD（在 refactor/db PR 中从历史遗留的 `pg.py` 中拆分而来） |
| `src/rolemesh/db/schema.py` | DDL —— 表 / 索引 / RLS / 迁移步骤（幂等的 `_create_schema()`） |
| `src/webui/auth.py` | WebUI auth 初始化与请求 token 校验 |
| `src/webui/oidc_routes.py` | OIDC PKCE endpoint（config、exchange、refresh、logout、callback） |
| `src/webui/admin.py` | RESTful Admin API |
| `src/rolemesh/security/credential_proxy.py` | 带按用户 token 注入的 MCP 代理 |
| `web/src/services/oidc-auth.ts` | 客户端 PKCE 流程 + token 管理 |

## 尚未实现

| 特性 | 状态 | 备注 |
|---------|--------|-------|
| BuiltinProvider | 占位 | 需要 登录/注册 endpoint、密码哈希、JWT 签发 |
| `agent_delegate` 强制执行 | 仅有 schema | 多 agent 委托协议尚未定义 |
| Agent `visibility` 字段 | 未启动 | 面向非 admin 用户的 `public` / `restricted` 可见性 |
| 多 IdP 支持 | 仅有结构上的就绪 | `OIDCConfig` + provider key 是实例级别的；注册表尚未构建 |

（审批工作流已被单独实现——见 [`approval-architecture.md`](approval-architecture.md)——已不再列在此处。）
