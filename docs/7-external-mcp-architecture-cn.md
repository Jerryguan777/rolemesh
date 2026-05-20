# 外部 MCP 工具架构

本文档描述 RoleMesh 如何集成外部 MCP（Model Context Protocol）服务器——为何在多种方案中选择凭据代理（credential-proxy）路线、MCP 配置如何从数据库流转到 agent 容器、用于对容器隐藏凭据的 URL 重写机制，以及决定每个 MCP 请求如何鉴权的 auth 模式。

## 背景：为何需要外部 MCP？

RoleMesh agent 运行在 Docker 容器中，使用 Claude SDK 或 Pi 后端。每个后端都自带内置工具（Bash、Read、Write……），并附带一个进程内的 `rolemesh` MCP 服务器，用于与 orchestrator 进行 IPC（`send_message`、`schedule_task`……）。

但是 agent 经常需要访问**外部服务**——以 MCP 服务器形式暴露的内部 API、数据库、第三方平台。这些外部服务器与 orchestrator 一同运行（或部署在更远的地方），使用 SSE 或 streamable-HTTP 传输，并需要鉴权（通常是放在 `Authorization` header 中的 JWT）。

两种后端都原生支持 MCP 服务器：

```python
mcp_servers={
    "my-server": {
        "type": "sse",  # or "http"
        "url": "http://...",
        "headers": {"Authorization": "Bearer <token>"},
    }
}
```

本文要解决的问题是：**我们如何安全地把 MCP 服务器 URL 与鉴权 token 送进容器？**

## 设计约束

1. **容器不得持有鉴权 token。** 这条规则已经用来保护 LLM API 密钥（Anthropic / OpenAI / Bedrock）——容器看到的是占位符，凭据代理负责注入真实密钥。MCP token 遵循同样的模式。
2. **按 coworker 配置。** 不同的 coworker 需要不同的 MCP 服务器。配置存放在 `coworkers.tools`（JSONB）中，而不是作为全局环境变量。
3. **Token 来源在外部。** MCP token 来自用户的 IdP（通过 OIDC），不是由 RoleMesh 签发。每用户、自动刷新的 token 模型由 `TokenVault` 管理——见下文"Token forwarding"。
4. **新增服务器无需重新构建容器镜像。** 添加一个 MCP 服务器只应需要更新数据库，并（可选）发出热重载信号——不需要重新构建，理想情况下连重启都不需要。

## 已考虑的备选方案

### 方案 A：直接把 token 传给容器

```
Orchestrator → AgentInitData.mcp_servers[].token → Container → MCP Server
```

最简单的做法：在 `AgentInitData` 中包含 JWT。

**优点**：代码量极少，不涉及 proxy。

**缺点**：token 在容器内可见。Agent 会执行任意工具调用（Bash 等）——任何工具调用都可能从内存或环境中读到 token。一旦 MCP token 泄漏，往往会授予对内部服务的广泛访问权限；爆炸半径过大。

**已否决**——违反"容器不持有凭据"原则。

### 方案 B：在容器中签发 JWT

把 JWT 签名密钥传入容器；agent runner 在每次 MCP 调用前签发短期 token。

**优点**：token 始终是新鲜的，长期运行的容器也不存在过期问题。

**缺点**：签名密钥**比任何单个 token 都更敏感**——一旦泄漏便能无限制签发 token。而且，对于这些 MCP 服务器而言，RoleMesh 并*不控制* JWT 的签发——token 来自 IdP。

**已否决**。

### 方案 C：凭据代理转发（已选定）

```
Container → Credential Proxy → injects Authorization → MCP Server
```

容器把 MCP 请求发给凭据代理，**不带**任何 auth header。代理查找已注册的 MCP 服务器，挑选合适的 `Authorization`（按用户的 IdP token，或一个静态服务密钥——见下文"auth_mode"），然后转发。

**优点**：

- 容器永远看不到 token——与 LLM API 密钥使用相同的安全模型。
- 复用现有的凭据代理基础设施（已经位于 LLM API 之前）。
- Token 语义与容器生命周期解耦——自动刷新的每用户 token 可以支持持续数小时的 agent 运行，远远超过任何单个 IdP token 的 TTL。

**缺点**：

- 所有 MCP 流量都会多经过 proxy 一跳网络。
- 代理必须能在不缓冲响应的前提下处理 SSE / streamable-HTTP。

**已选定**——在保留安全边界的同时，能干净地接入既有基础设施。

## 凭据代理的部署位置

凭据代理最初是宿主机上的一个进程，绑定在 `host.docker.internal:3001`。**自 EC-2 / Egress Control V1 起，它运行于 `egress-gateway` 容器内**，与正向代理和权威 DNS 解析器并列。Agent 容器通过 Docker DNS 以 `http://egress-gateway:3001` 访问它。Agent 网络（`rolemesh-agent-net`）是 `Internal=true`，所以网关是唯一的出网通路——无论是 LLM API 调用、MCP 调用还是其他出站流量。拓扑细节见：[`egress/deployment.md`](egress/deployment.md)。

旧的导入路径 `src/rolemesh/security/credential_proxy.py` 被保留为对真实实现 `src/rolemesh/egress/reverse_proxy.py` 的薄重新导出，以便旧的调用点仍能解析。

## 架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                         CONFIGURATION                                │
│                                                                      │
│  PostgreSQL                          OIDC IdP                        │
│  ┌──────────────────────────────┐    ┌──────────────────────────────┐│
│  │ coworkers.tools (JSONB):     │    │ Per-user access tokens,      ││
│  │ [{"name":"my-server",        │    │ refreshed automatically      ││
│  │   "type":"sse",              │    │ (TokenVault — see auth doc)  ││
│  │   "url":"http://.../mcp/",   │    └──────────────────────────────┘│
│  │   "auth_mode":"user"}]        │                                   │
│  └──────────────┬───────────────┘                                    │
└─────────────────┼────────────────────────────────────────────────────┘
                  │
                  ▼
   ORCHESTRATOR registers each server with the credential proxy:
     (name, origin URL, per-server static headers, auth_mode)

   And distributes the registry over NATS:
     egress.mcp.snapshot.request   (request-reply; gateway pulls on boot)
     egress.mcp.changed            (broadcast on admin edit; hot-reload)

                  ▼
       ┌────────────────────────────────────────────┐
       │  Credential Proxy   (in egress-gateway)    │
       │  Routes  /mcp-proxy/{name}/**  :           │
       │    1. lookup name → (origin, headers,      │
       │       auth_mode)                           │
       │    2. inject Authorization per auth_mode   │
       │    3. forward to origin                    │
       └────────────────────────────────────────────┘
                  ▲
                  │  http://egress-gateway:3001/mcp-proxy/my-server/mcp/
                  │  (no Authorization header)
                  │
       ┌──────────┴────────────────────────────────┐
       │   Agent container (Claude SDK or Pi)       │
       │   Reads AgentInitData.mcp_servers from KV │
       │   → registers with the agent SDK           │
       └────────────────────────────────────────────┘
```

### 请求流程（一次工具调用）

1. Agent 决定调用 `mcp__my-server__some_tool`。
2. SDK 向 `http://egress-gateway:3001/mcp-proxy/my-server/mcp/` 发送 HTTP 请求，且不携带 `Authorization` header。容器侧从未知晓 token。
3. 凭据代理：
   - 剥掉 `/mcp-proxy/{name}` 前缀；剩余路径 = `/mcp/`。
   - 在注册表中查找 `my-server` → `(origin URL, per-server static headers, auth_mode)`。
   - 按 `auth_mode` 选择 `Authorization`（见下文）。
   - 转发到原始 URL。
4. MCP 服务器校验 token（它并不知道前面有一个 proxy），处理工具调用，返回 SSE / streamable-HTTP。
5. 代理把响应不缓冲地流式回传给容器。

## auth_mode：三种鉴权策略

同一个代理同时为支持 OIDC 的 MCP 服务器和遗留 MCP 服务器服务。`auth_mode`（设置在 `McpServerConfig` 上）告诉代理对每个服务器使用哪种鉴权形态。

| `auth_mode` | 代理注入的内容 | 适用场景 |
|---|---|---|
| **`user`**（默认） | 用户的 IdP 签发的 access token，作为 `Authorization: Bearer <fresh access_token>`。Per-server 静态 headers 会被透传，但 `Authorization` 会被覆盖。 | 支持 OIDC 的 MCP 服务器——它们通过 OIDC discovery 校验 token，并据此识别调用的用户。 |
| **`service`** | 原样使用 per-server 静态 headers（包括管理员设置的任何 `Authorization`）。**没有 per-user token。** | 服务到服务 / 使用共享服务密钥的遗留 MCP。 |
| **`both`** | Per-server 静态 headers 保持不变；用户的 access token 通过 `X-User-Authorization` 携带。 | 双层校验——MCP 服务器同时检查一个服务密钥（针对自身）和一个用户 token（针对发起请求的用户）。 |

`user` 和 `both` 模式需要每次请求都带上用户身份。容器通过 `X-RoleMesh-User-Id` 转发该身份，由 agent runner 从 `AgentInitData` 中读出并设置。如果 `TokenVault` 中没有该用户的新鲜 token，请求会**不带**用户 token 直接转发——MCP 服务器返回 401，agent 把错误暴露出来，并提示用户重新登录。绝不会静默回退到另一个用户的 token。

Token 机制（静态加密、自动刷新、轮换处理）见 [`6-auth-architecture.md`](6-auth-architecture.md) ——"MCP Token Forwarding: TokenVault"。本文只关心凭据代理与 MCP 服务器之间的契约。

## 数据模型

### `McpServerConfig`（orchestrator 端）

存储在 `coworkers.tools` JSONB 中。携带代理转发请求所需的全部信息，外加在别处使用的 per-tool 元数据：

```python
@dataclass(frozen=True)
class McpServerConfig:
    name: str             # registered name in the SDK, e.g. "my-server"
    type: str             # "sse" or "http" (streamable-HTTP)
    url: str              # actual MCP server URL on the host network
    headers: dict[str, str] = ...   # per-server static headers (service keys, ...)
    auth_mode: str = "user"          # "user" | "service" | "both"
    tool_reversibility: dict[str, bool] = ...
```

`tool_reversibility` 字段是 per-tool 元数据，供 Safety Framework V2 的 cost-class × reversibility 守卫使用——只读查询为 `True`，状态变更类工具为 `False`（默认值）。缺失的条目会回退到一张内置表；reversibility 不由 agent 决定，而由运维人员决定。见 [`safety/safety-framework.md`](safety/safety-framework.md)。

### `McpServerSpec`（容器端）

通过 `AgentInitData.mcp_servers` 传入（NATS KV bootstrap 载荷——见 [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md)）。只包含容器被允许看到的内容：

```python
@dataclass(frozen=True)
class McpServerSpec:
    name: str
    type: str
    url: str              # rewritten proxy URL — no token, no upstream host
    tool_reversibility: dict[str, bool] = ...
```

没有 `headers`、没有 `auth_mode`、没有 token。鉴权决策严格保留在 orchestrator/proxy 这一侧的边界内。

### URL 重写

orchestrator 在写入 `AgentInitData` 之前，会把每个宿主侧 URL 转换为一个 proxy URL：

```
Input:  http://localhost:9100/mcp/
              ↓
Parse:  scheme=http, host=localhost, port=9100, path=/mcp/
              ↓
Output: http://egress-gateway:3001/mcp-proxy/my-server/mcp/
        ├── proxy host:port ───────┤├── prefix ──┤├─ path ─┤
```

代理在每次请求时反向执行该重写：

```
Request: /mcp-proxy/my-server/mcp/
              ↓
Strip:   server_name = "my-server", remaining_path = "/mcp/"
              ↓
Lookup:  registry["my-server"] → "http://localhost:9100"
              ↓
Forward: http://localhost:9100/mcp/
```

管理员提供的 `localhost` URL 也会被重写为 Docker 可达的主机名（在 Linux 与 Darwin 上是 `host.docker.internal`，在每个发布边界上重新选择），从而让运行在容器里的网关可以真正到达上游服务。这些跨平台的修正在 egress 系列 PR（#13–#17）中落地。

## 注册表分发

MCP 注册表存在于两个必须保持一致的位置：orchestrator 在宿主侧的 dict，以及网关容器内的同一份 dict。它们通过 NATS 同步：

- **`egress.mcp.snapshot.request`** —— request-reply RPC。网关在启动时调用它来拉取当前注册表。
- **`egress.mcp.changed`** —— 广播。当管理员通过 WebUI admin API 添加、编辑或删除 MCP 服务器时，orchestrator 发布一个增量，网关在不重启的情况下更新其内存缓存。

因此通过管理员 REST API 编辑 `coworkers.tools` 是一项热操作。新的 MCP 服务器会在下一次 agent 启动时变得可用（agent 从 `AgentInitData` 中读取 proxy URL）；现有 agent 仍能继续工作，因为它们的 proxy URL 没有变化。

## 安全模型

| 关注点 | 应对方式 |
|---|---|
| Token 存储 | 每用户的 IdP refresh + access token 在 `oidc_user_tokens` 中静态加密存储；从不进入容器内存 |
| Token 注入 | 凭据代理按 `auth_mode` 添加 `Authorization` |
| 容器访问 | 容器只知道 proxy URL——没有 token，也没有上游主机 |
| Token 过期 | TokenVault 对 IdP 自动刷新；永久失败时强制重新登录 |
| MCP 服务器校验 | MCP 服务器通过 OIDC discovery 校验 token——RoleMesh 是穿透方，不是签发方 |
| 代理范围 | MCP 路由只转发到**已注册**的服务器名——未知名称返回 404 |

### 被攻陷的容器能做什么

一个运行任意代码（通过 Bash 工具）的容器可以：

- **看到** proxy URL（`http://egress-gateway:3001/mcp-proxy/my-server/...`）。
- **通过 proxy 调用** MCP 服务器——proxy 不对*调用方*做鉴权，它鉴权的是该调用所代表的*用户*。被攻陷的容器因此可以以它启动时所代表的用户身份行事，但无法冒充其他用户（其他用户的 token 并不存在于这个容器中）。
- **不能**看到任何鉴权 token，无论是 per-user 还是 service。
- **不能**直接到达 MCP 服务器——agent 网络是 `Internal=true`，唯一的出网路径就是经过网关。

这条边界与 LLM API 路径完全一致：容器可以通过 proxy 发起 API 调用，但无法提取或伪造凭据。

## 相关文档

- [`6-auth-architecture.md`](6-auth-architecture.md) —— `TokenVault` 机制、OIDC 集成、每用户 token 模型
- [`safety/safety-framework.md`](safety/safety-framework.md) —— `tool_reversibility` 以及它如何拦截风险工具调用
- [`egress/deployment.md`](egress/deployment.md) —— agent 网络拓扑、凭据代理实际部署位置、网关启动序列
- [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md) —— `AgentInitData.mcp_servers` 字段；`egress.mcp.*` subjects
