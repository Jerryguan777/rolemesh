# RoleMesh UI & Backend 设计文档 v1.1（合并版）

> 基于 troposai v3 适配 + 实施复盘 L1-L13 教训。
> **本文档不依赖任何外部 IdP**。OIDC / token_vault / user-mode MCP 架构与代码保留并单测覆盖，e2e live smoke 推迟到 Keycloak + mock-tropos-mcp 分支合入后再补。
> Live smoke 全部走 **bootstrap fast-path**（含方案 A 多 user 扩展）。

## 0. 文档约定

- 所有 tenant-scoped SQL = RLS policy + 显式 `WHERE tenant_id = $1` 谓词（**INV-1**）
- 所有 IPC dataclass deserialize = filter unknown keys（**INV-2**）
- 容器 orphan cleanup = image whitelist，不是 name substring（**INV-3**）
- 所有 audit FK actor_user_id 写入 = 真 user 或返 503 `BOOTSTRAP_NEEDS_TENANT_OWNER`（**INV-4**）
- `SKILL_MANIFEST_NAME` 常量在 DB CHECK / Python validator / TS validator 三处共享（**INV-5**）
- `runs.{status, completed_at, usage}` 在每条终止路径都被 UPDATE（**INV-6**）
- Wire enum 与 engine enum 在 handler 边界翻译，不污染引擎（**INV-7**）

每条不变量配 pinned test，CI 强制。详见 §11。

---

## 1. 命名映射（troposai → rolemesh）

rolemesh 前后端**术语统一**为 `coworker`，丢掉 troposai ADR-008 的双语层。

| 维度 | troposai | rolemesh |
|---|---|---|
| URL / API DTO | `/api/v1/agents/...` | `/api/v1/coworkers/...` |
| Service / DB / log | `coworker` | `coworker` |
| UI 文案 | "Agent" | "Coworker" |
| Lint 规则 | 双向限制 | **N/A** |
| Routing | `/agents/:id` | `/coworkers/:id` |
| Contract 物理位置 | 跨 repo submodule | **同 repo `web/src/api/generated/`**（单 repo 优势）|

ADR-008 删除；其它 troposai ADR 全部保留。

---

## 2. 数据模型

### 2.1 新增表

```sql
-- 平台层（无 RLS）
CREATE TABLE models (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider        VARCHAR(50) NOT NULL,         -- anthropic|openai|google|bedrock
  model_id        VARCHAR(200) NOT NULL,        -- claude-opus-4-7, gpt-4o, ...
  model_family    VARCHAR(50) NOT NULL,         -- claude|gpt|gemini|...
  display_name    VARCHAR(200) NOT NULL,
  is_platform     BOOLEAN NOT NULL DEFAULT TRUE,  -- v2 演进点
  is_active       BOOLEAN NOT NULL DEFAULT TRUE,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (provider, model_id)
);

-- 租户层（RLS）
CREATE TABLE tenant_model_credentials (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  provider         VARCHAR(50) NOT NULL,
  credential_data  BYTEA NOT NULL,              -- Fernet-encrypted JSON {api_key, ...}; see §8.1
  created_at       TIMESTAMPTZ DEFAULT NOW(),
  updated_at       TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (tenant_id, provider)
);

CREATE TABLE mcp_servers (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name                VARCHAR(200) NOT NULL,
  type                VARCHAR(50) NOT NULL,     -- sse|http
  url                 TEXT NOT NULL,
  auth_mode           VARCHAR(50) NOT NULL,     -- user|service|both
  credential_ref      TEXT,                     -- service 模式才有
  extra_headers       JSONB DEFAULT '{}',
  tool_reversibility  JSONB DEFAULT '{}',
  description         TEXT,
  created_at          TIMESTAMPTZ DEFAULT NOW(),
  updated_at          TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (tenant_id, name)
);

-- 关系层（RLS via 父级）
CREATE TABLE coworker_mcp_servers (
  coworker_id     UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
  mcp_server_id   UUID NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
  enabled_tools   TEXT[] DEFAULT NULL,          -- NULL=全启用，[]=全禁
  PRIMARY KEY (coworker_id, mcp_server_id)
);

CREATE TABLE coworker_skills (
  coworker_id   UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
  skill_id      UUID NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  enabled       BOOLEAN NOT NULL DEFAULT TRUE,
  PRIMARY KEY (coworker_id, skill_id)
);

CREATE TABLE runs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID NOT NULL,
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  status          VARCHAR(20) NOT NULL,         -- running|completed|failed|cancelled|awaiting_reauth
  started_at      TIMESTAMPTZ DEFAULT NOW(),
  completed_at    TIMESTAMPTZ,
  usage           JSONB,
  error           JSONB
);
```

### 2.2 现有表修改

```sql
-- coworkers
ALTER TABLE coworkers
  ADD COLUMN model_id              UUID REFERENCES models(id),
  ADD COLUMN created_by_user_id    UUID REFERENCES users(id);     -- L6 必须 NULLABLE
-- tools JSONB 走三阶段下线（见 §9.3）

-- skills: per-coworker → per-tenant
ALTER TABLE skills
  ADD COLUMN created_by_user_id UUID REFERENCES users(id);         -- L6 必须 NULLABLE
ALTER TABLE skills ADD CONSTRAINT skills_tenant_name_unique UNIQUE (tenant_id, name);
-- coworker_id 列保留双写期，最后 drop

-- messages: 关联 run
ALTER TABLE messages ADD COLUMN run_id UUID REFERENCES runs(id);
```

**L6 强约束**：所有 `created_by_user_id` 必须 `NULLABLE`。audit 表（`approval_audit_log.actor_user_id` / `safety_rules_audit.actor_user_id`）写入路径用 helper：

```python
async def _bootstrap_actor_user_id(tenant_id) -> UUID:
    """Bootstrap path: resolve tenant's first owner; no owner -> 503."""
    user = await fetch_first_owner(tenant_id)
    if not user:
        raise BootstrapError(
            code="BOOTSTRAP_NEEDS_TENANT_OWNER",
            status=503,
            message="audit write requires a real user; bootstrap tenant has no owner",
        )
    return user.id
```

注：方案 A 多 bootstrap user 存在时，audit 写入应优先使用 bootstrap 当前 user_id（如 alice/bob 真实落 users 表），仅在裸 `ADMIN_BOOTSTRAP_TOKEN` (user_id="bootstrap") 时走 helper。

### 2.3 Backend 兼容矩阵（代码常量，L1）

```python
# src/rolemesh/core/backend_capabilities.py  <- new file
from dataclasses import dataclass

@dataclass(frozen=True)
class BackendCapability:
    name: str
    supported_providers: frozenset[str]
    supported_model_families: frozenset[str] | None   # None = unrestricted
    description: str

CLAUDE_BACKEND = BackendCapability(
    name="claude",
    supported_providers=frozenset({"anthropic", "bedrock"}),
    supported_model_families=frozenset({"claude"}),
    description="Claude Agent SDK — Anthropic-family models",
)
PI_BACKEND = BackendCapability(
    name="pi",
    supported_providers=frozenset({"anthropic", "openai", "google", "bedrock"}),
    supported_model_families=None,
    description="Pi runtime — multi-provider",
)
ALL_BACKENDS = {b.name: b for b in [CLAUDE_BACKEND, PI_BACKEND]}

def validate_combo(backend_name: str, provider: str, family: str) -> None:
    b = ALL_BACKENDS[backend_name]
    if provider not in b.supported_providers:
        raise BackendCompatError(...)
    if b.supported_model_families is not None and family not in b.supported_model_families:
        raise BackendCompatError(...)
```

校验是 **(provider x family) 二元矩阵**，单维 enum 不够（Bedrock 既能跑 Claude 又能跑 Llama）。

---

## 3. API 端点

前缀 `/api/v1/*`；现有 `/api/admin/*` 保留 6 个月兼容期。

### Phase 1 — Chat 主路径

```
Auth/Session     GET  /api/v1/auth/config
                 POST /api/v1/auth/ws-ticket
                 GET  /api/v1/me

Backends         GET  /api/v1/backends                  <- Cache-Control: max-age=3600

Coworkers        GET/POST    /api/v1/coworkers
                 GET/PATCH/DELETE /api/v1/coworkers/{id}

Conversations    GET/POST    /api/v1/coworkers/{id}/conversations
                 GET         /api/v1/conversations/{id}
                 GET         /api/v1/conversations/{id}/messages
                 DELETE      /api/v1/conversations/{id}

Runs             WS   /api/v1/conversations/{id}/stream
                 GET  /api/v1/runs/{id}
                 POST /api/v1/runs/{id}/cancel
```

### Phase 2 — 配置生态

```
Models           GET  /api/v1/models?provider=&family=
                 GET  /api/v1/models/{id}
                 POST/PATCH/DELETE /api/v1/admin/models/{id}       <- 推迟到 v2

Credentials      GET  /api/v1/tenant/credentials
                 PUT  /api/v1/tenant/credentials/{provider}
                 DELETE /api/v1/tenant/credentials/{provider}

MCP Servers      GET/POST       /api/v1/mcp-servers
                 GET/PATCH/DELETE /api/v1/mcp-servers/{id}

Coworker <-> MCP GET    /api/v1/coworkers/{id}/mcp-servers
                 POST   /api/v1/coworkers/{id}/mcp-servers/{mcp_id}
                 PATCH  /api/v1/coworkers/{id}/mcp-servers/{mcp_id}   <- enabled_tools
                 DELETE /api/v1/coworkers/{id}/mcp-servers/{mcp_id}

Bindings         GET/POST       /api/v1/coworkers/{id}/bindings
                 GET/PATCH/DELETE /api/v1/bindings/{id}

Schedules        GET    /api/v1/coworkers/{id}/schedules       <- 只读
                 DELETE /api/v1/coworkers/{id}/schedules/{sid} <- 紧急刹车
```

### Phase 3 — Approvals + Skills

```
Approval Policies GET/POST    /api/v1/approval-policies
                  GET/PATCH/DELETE /api/v1/approval-policies/{id}

Approval Requests GET  /api/v1/approvals
                  GET  /api/v1/approvals/{id}
                  POST /api/v1/approvals/{id}/decide
                  GET  /api/v1/approvals/{id}/audit-log

Skills            GET/POST    /api/v1/skills
                  GET/PATCH/DELETE /api/v1/skills/{id}

Skill Files       GET    /api/v1/skills/{id}/files
                  GET    /api/v1/skills/{id}/files/{path:path}
                  PUT    /api/v1/skills/{id}/files/{path:path}
                  DELETE /api/v1/skills/{id}/files/{path:path}        <- SKILL.md 受保护

Coworker <-> Skill GET    /api/v1/coworkers/{id}/skills
                   POST   /api/v1/coworkers/{id}/skills/{skill_id}
                   DELETE /api/v1/coworkers/{id}/skills/{skill_id}
```

### Phase 4 — Safety UI

```
GET /api/v1/safety/rules
GET /api/v1/safety/rules/{id}
GET /api/v1/safety/rules/{id}/audit
GET /api/v1/safety/checks
GET /api/v1/safety/decisions
GET /api/v1/safety/decisions/{id}
```

### DELETE 语义（L13）—— 不一定是 409

默认 409，但每个 DELETE 在 OpenAPI `description` 显式声明引用语义：

| 资源 | DELETE 行为 |
|---|---|
| `models/{id}` | 被 coworker 引用 -> 409 |
| `mcp-servers/{id}` | 被 coworker 引用 -> 409 |
| `skills/{id}` | 被 coworker 启用 -> 409 |
| `approval-policies/{id}` | pending requests 的 `policy_id` -> **SET NULL**（不阻塞已发出审批） |
| `coworkers/{id}` | 级联删除 conversations/runs/messages |
| `tenant_model_credentials/{provider}` | 被使用中的 coworker 引用 -> 409 |
| 单文件 `skills/{id}/files/SKILL.md` | **409**（manifest 保护） |

409 错误体统一格式：

```json
{
  "code": "RESOURCE_IN_USE",
  "message": "Cannot delete model: 3 coworkers are using it",
  "details": {
    "in_use_by": "coworkers",
    "count": 3,
    "sample_ids": ["uuid1", "uuid2", "uuid3"]
  }
}
```

### Wire enum 与 engine enum 翻译（L3）—— 审批

```
HTTP POST /api/v1/approvals/{id}/decide
     body: {action: "approve" | "reject", note?}

WS   client->server: request.approval
     body: {approval_id, decision: "approve" | "deny", note?}

WS   server->client: event.approval.resolved
     body: {approval_id, decision: "approve"|"deny"|"expired"|"cancelled"}

Engine internal: ApprovalOutcome = Literal["approved","rejected","expired","cancelled"]
```

每个 transport 各自的 closed enum，handler 在 wire 边界翻译。pinned test `TestResolvedDecisionMap` 防回归。

---

## 4. WS 协议

单一端点 `WS /api/v1/conversations/{id}/stream?ticket=<jwt>`，事件驱动。

```
client -> server:
  request.run         {input, run_id?}
  request.cancel      {run_id}
  request.approval    {approval_id, decision: "approve"|"deny", note?}

server -> client:
  event.run.started        {run_id}
  event.run.token          {run_id, delta}
  event.run.tool_call      {run_id, tool_name, input}
  event.run.tool_result    {run_id, tool_name, output}
  event.run.completed      {run_id, final_message, usage}
  event.run.error          {run_id, code, message}
  event.run.requires_reauth {run_id, reason}              <- user-mode MCP token 失效（架构保留）
  event.approval.required  {run_id, approval_id, summary}
  event.approval.resolved  {approval_id, decision}
```

**握手**：短期 JWT ticket（exp <= 60s）颁发自 `POST /api/v1/auth/ws-ticket`，握手期校验 user 对 path conversation_id 的访问权限。bootstrap fast-path 下 ticket 由 bootstrap user 签发。

**重连**：客户端断线 -> 先 `GET /api/v1/runs/{id}` 拿 truth -> 已完成不订阅，进行中订阅增量。

**Run 状态机完整性（INV-6 / L10）**：所有终止路径必须 UPDATE `runs.{status, completed_at, usage}`：
- WS 正常完成 / 错误
- `POST /runs/{id}/cancel`
- 调度器 schedule 异步完成
- approval reject 终止
- coworker 容器 crash / OOM / timeout
- user-mode MCP token 失效（status = `awaiting_reauth`，架构保留）

`tests/test_run_state_machine.py` 枚举每条路径。

### 4.1 Stop vs Cancel —— 两个独立 control surface

用户中止 agent 工作有**两个语义不同的控制**，UI 必须明确区分，不能合并到同一个按钮：

| 控制 | endpoint / wire | 中止粒度 | 容器命运 | 适用场景 |
|---|---|---|---|---|
| **Stop**（soft） | 旧 `/ws/chat {type:"stop"}` → NATS `agent.{job_id}.interrupt` → SDK `interrupt_current_turn` | **本轮 turn 生成中止**；同 conversation 下一轮可立即继续 | **保留**（agent process / MCP connections / 凭证 inject 都不变） | 用户对当前回复不满意，想打断后继续追问 |
| **Cancel**（hard） | `POST /api/v1/runs/{id}/cancel` 或 WS `request.cancel` → NATS `web.run.cancel.{run_id}` → `runtime.stop` + `terminate_run_via_user_cancel` | **整个 run 终止**；agent 不再为本 run 触发任何 turn | **硬杀**（释放资源、清凭证；下次新消息冷启动容器） | 用户想终结整个任务，资源立刻释放 |

**为什么不合并**：

- Stop 走 SDK 的 turn-cancellation 原语，**毫秒级生效**且无副作用；用户点 Stop 后立刻点 "再试一次" 体验顺滑
- Cancel 必须 `runtime.stop` 容器才能真正"停"——下次 chat 1-3 秒冷启动；如果把 Stop 按钮映射到 Cancel endpoint，每次软中断都付重启税
- 两者落入 `runs.status` 的状态也不同：Stop 不改 runs 状态（run 仍可能 completed），Cancel 写 `status='cancelled'`

**前端实现约束**：

- chat UI **两个按钮并存**（典型：聊天框旁边的 ⏸ Stop 与对话顶部的 ✕ Cancel）
- 文案区分清楚（避免用户误以为 Stop 会"完全停下来"）
- 已 terminal 的 run 上两个按钮都禁用

`tests/orchestration/test_run_cancel_subscriber.py` 钉死 Cancel 路径的语义（容器停 + status 写）；旧 Stop 路径走的 NATS subject 与 INV-6 状态机解耦，由 channel 层独立保证。

---

## 5. 认证与 User-mode MCP

### 5.1 Auth 入口（四条独立路径）

警告："builtin" 在 troposai/rolemesh 里是两个东西，常被混为一谈。代码层面是分开的：

| 名字 | 入口 | 状态 | 用途 |
|---|---|---|---|
| `AUTH_MODE=external` | `ExternalJwtProvider` | 已实现 | 上游签发 JWT（生产典型）|
| `AUTH_MODE=oidc` | `OIDCAuthProvider` + PKCE 链路 | 已实现 | 生产 / Keycloak / Okta / Auth0；**当前 rolemesh 无可用 IdP，本设计不依赖** |
| `AUTH_MODE=builtin` | `BuiltinProvider` | Stub（所有方法 `NotImplementedError`） | 自管理用户/密码/JWT 的未来扩展；**不要依赖** |
| **Bootstrap fast-path** | `webui/auth.py:54-67` 硬接的 dev fallback；任意 `AUTH_MODE` 下生效 | 已实现，live smoke 用的就是这个 | 跳过任何 IdP，token 命中 -> 虚拟 user |

Bootstrap fast-path 不走 `AuthProvider.authenticate()`，是 `authenticate_ws()` 在调 provider 之前的 short-circuit；所以 `AUTH_MODE` 设什么都不影响它生效。

### 5.2 Live smoke 策略（仅 bootstrap fast-path）

本设计**不依赖任何 IdP**。所有 Phase 的 e2e smoke 走 bootstrap。

| Phase | 默认 smoke | 备注 |
|---|---|---|
| 0 | Bootstrap fast-path | — |
| 1 | Bootstrap fast-path | — |
| 2 | Bootstrap fast-path | `auth_mode=service` MCP 路径完整 e2e；`auth_mode=user` 路径**单测覆盖**，不 e2e |
| 3 | Bootstrap fast-path（多 user 走 §5.2.1）| approval 多 user 端到端跑 |
| 4 | Bootstrap fast-path | — |

#### 5.2.1 多 user smoke：方案 A — 扩展 bootstrap token 支持 user map

bootstrap fast-path 当前只产 `user_id="bootstrap"`。Phase 3 的 approval 业务需要发起者 != 审批者，所以扩展：

```bash
# 兼容旧用法
ADMIN_BOOTSTRAP_TOKEN=<single-token>           # -> user_id="bootstrap", role=owner

# 新增多 user map（任一存在即生效）
BOOTSTRAP_USERS='[
  {"token":"tok-alice","user_id":"alice","tenant":"default","role":"owner"},
  {"token":"tok-bob",  "user_id":"bob",  "tenant":"default","role":"member"}
]'
```

实际实现位于 `src/webui/auth.py:authenticate_ws()` + `src/rolemesh/auth/bootstrap_users.py`（spec 解析、token 索引、`ensure_bootstrap_user_row` upsert）。`authenticate_ws` 内仅串两路 fast-path，spec 索引与 upsert 在 `bootstrap_users.py` 中：

```python
async def authenticate_ws(token: str) -> AuthenticatedUser | None:
    # Resolution order (multi-user first so a dev configuring both
    # gets the richer identity, not the impoverished bootstrap one):
    #
    #   1. BOOTSTRAP_USERS multi-user map
    #   2. ADMIN_BOOTSTRAP_TOKEN single-user legacy fast-path
    #   3. configured AuthProvider (external JWT / OIDC / builtin)

    # 1. multi-user map
    spec = get_spec_for_token(token)
    if spec is not None:
        tenant = await get_tenant_by_slug(spec.tenant_slug)
        if tenant is None:
            # Fail closed: spec references a tenant that isn't in
            # the DB. Don't manufacture a fictitious tenant_id;
            # return None so the request is rejected as
            # unauthenticated.
            return None
        user_uuid = await ensure_bootstrap_user_row(spec, tenant.id)
        return AuthUser(
            user_id=user_uuid, tenant_id=tenant.id,
            role=spec.role, name=spec.user_id_slug,
        )

    # 2. single-token legacy fast-path
    if ADMIN_BOOTSTRAP_TOKEN and token == ADMIN_BOOTSTRAP_TOKEN:
        return _build_bootstrap_user(
            user_id="bootstrap", tenant_slug="default", role="owner",
        )

    # 3. fall through to provider
    return await authenticate_request(token)
```

`_build_bootstrap_user` 必须保证 user 真实落 `users` 表（首次见到时 upsert，role 设为 spec 提供值）——这样后续 audit FK 不需要走 `_bootstrap_actor_user_id()` 兜底。

**约束**：
- `BOOTSTRAP_USERS` 仅 dev/CI 生效；生产 deployment 不带该 env var（启动时若 `AUTH_MODE` 非 `external` 且未显式 opt-in，warn-log）
- 每个 spec 的 token 强度自管理；dev 用 `openssl rand -hex 32`
- spec 修改后需重启 webui（不 hot-reload）
- spec 引用的 `tenant_slug` 必须在 DB 中真实存在；找不到对应 tenant 时 `authenticate_ws` 返回 `None`（请求被拒为未鉴权），不伪造 `tenant_id`。这是有意的 fail-closed 行为，确保 audit FK 不会指向幽灵 tenant。

#### 5.2.2 OIDC / user-mode MCP 链路实现推迟（整条）

原计划（02c session）是"e2e 推迟，但单测+wiring 实现先合入"。v1.1 实施过程中应用反 over-engineering 原则后**进一步推迟**：`auth_mode=user` MCP 注入路径（IPC header + credential_proxy 反查 + reauth wire）**整条链路实现都不在 v1.1 范围**，留给 Keycloak / OIDC 分支单独 session 一次性做完。理由：0 当前 caller、0 攻击向量（无 token 注入路径 = header 伪造无意义）、~1050 LOC 投入服务的是"想象中的未来用户"。

在 OIDC 分支合入前的实际状态：

- `AUTH_MODE=oidc` 启动可以 work（OIDC client 代码 + TokenVault 在 v1.1 内已就位，OIDC 登录流程能跑——00a + 02a 同步落地的部分）
- **`auth_mode=user` MCP server** 配了也不会真注入 user token（credential_proxy 当前只处理 `service`）—— 见下 §5.4.1
- WS ticket 颁发链路单测覆盖
- `token_vault` 行为单测覆盖（mock IdP）
- ~~`auth_mode=user` MCP 路径单测覆盖（mock vault + mock MCP）~~ **不做**——单测无 caller 也是 over-engineering

未来 OIDC session 启动时，强烈建议先 `git log --follow docs/webui-backend-v1.1-sessions/02c-credential-proxy-user-mode.md` 拿回 retired 时记录的设计要点（特别是 conversation_id header 信任验证机制，那是 02c refresh 期间发现的安全要求）。

##### 5.4.1 `auth_mode=user` 当前的"配置但不工作"状态

02a 给 `mcp_servers.auth_mode` 落了 `user / service / both` 三值的 API + DB schema，但 02c retire 后 credential_proxy 没有 `user` 路径处理。两种处置选项（待选）：

- **A**（推荐）：API 层把 `auth_mode` 临时限制为只接受 `service`，UI dropdown 把 `user` / `both` 标灰提示 "Coming with OIDC integration"。配置时即拒绝，避免 silent failure。改动小（~30 LOC，Pydantic Literal 收窄 + frontend dropdown）
- B：DB / API 保持三值，runtime 命中 `user` / `both` 时 silently 跳过——差体验，不推荐

留作 follow-up，不在 02c retirement 范围内。

### 5.3 User-mode MCP 链路（架构保留）

```
Browser --Login--> rolemesh --code exchange--> IdP
              <-- access + refresh --
              v
        token_vault: encrypt and store refresh_token + cache access_token

Coworker --tool call--> credential_proxy
                       ^ X-RoleMesh-Conversation-Id header
                       |
                       +-- query conversations -> get user_id
                       +-- query token_vault -> get access_token (refresh if expired)
                       +-- inject Authorization: Bearer <user_token>
                       v
                  Upstream MCP server
                       ^ JWKS verify + extract sub -> permission table
```

**关键设计点**：coworker 容器本身不知道 user 是谁；credential_proxy 通过 conversation_id 反查 user_id。需补 IPC：
- Coworker 出站 MCP 调用必带 `X-RoleMesh-Conversation-Id` header
- credential_proxy 在 `auth_mode=user` 的 MCP 出站路径拦截 -> 查表 -> 注入 token

**Smoke 状态**：本节链路单测覆盖；e2e live smoke 推迟到 OIDC 分支合入后。

### 5.4 失败模式（架构保留）

| 场景 | 行为 |
|---|---|
| Access token 过期 | vault 自动 refresh，无感 |
| Refresh token 过期 | vault 返结构化 401 -> coworker 收到 401 -> `run.status=awaiting_reauth` -> WS `event.run.requires_reauth` -> UI banner 提示重登 |
| 用户显式登出 | `token_vault.revoke(user_id)` -> 后续 MCP 调用同上 |
| IdP 端 disable user | refresh 时 `invalid_grant` -> vault 清 token -> 同上 |
| Scheduled run + user 不在线 | **Phase 1 拒绝**：scheduled 路径检测 `auth_mode=user` MCP -> 结构化拒绝 `code=NEEDS_USER_PRESENCE` |

**Smoke 状态**：本节链路单测覆盖；e2e live smoke 推迟到 OIDC 分支合入后。

### 5.5 Audience 处理（未来 dev / 未来 prod）

| 方案 | 用途 |
|---|---|
| D1（dev）| mock MCP 不强制 audience，只验 issuer + 签名。 |
| D2（不推荐）| Keycloak 给主 client 加 audience mapper；扩展性差，跳过 |
| D3（prod）| RFC 8693 token exchange；vault 加 `exchange_for(audience)` 方法 |

**Smoke 状态**：vault 的 `exchange_for(audience)` 方法预留接口 + stub 实现 + 单测；D1 / D3 的真值实现 + e2e 推迟到 OIDC 分支。

---

## 6. UI 设计（Lit + Tailwind）

### 6.1 路由

保留 hash router（与现有 chat 一致），不引入 React Router。

```
#/                                  -> chat (default)
#/coworkers                         -> list
#/coworkers/new                     -> create wizard
#/coworkers/:id                     -> detail (subtab: overview/skills/mcp/bindings/schedules/conversations)
#/conversations/:id                 -> chat single conversation view
#/mcp-servers                       -> MCP list
#/mcp-servers/:id                   -> MCP edit
#/models                            -> platform model catalog (read-only)
#/credentials                       -> credential management
#/skills                            -> skills list (tenant catalog)
#/skills/:id                        -> skills editor (file tree)
#/bindings                          -> channel bindings overview
#/approvals                         -> approval queue
#/approvals/:id                     -> approval detail
#/admin/safety/rules                -> already exists
#/admin/safety/decisions            -> already exists
```

### 6.2 整体布局

复用 chat 页 shell：

```
+--------------------------------------------------------+
| <rm-app-shell>                                         |
| +----------+----------------------------------------+ |
| | Sidebar  | Topbar  Logo  Page Title   user menu   | |
| | (w-64)   +----------------------------------------+ |
| |          |                                        | |
| | - Chat   |   <main content>                       | |
| | - Co-    |   (per-page component)                 | |
| |  workers |                                        | |
| | - MCP    |                                        | |
| | - Models |                                        | |
| | - Skills |                                        | |
| | - Cred.  |                                        | |
| | - Bind.  |                                        | |
| | - App-   |                                        | |
| |  rovals  |                                        | |
| | - Safety |                                        | |
| +----------+----------------------------------------+ |
+--------------------------------------------------------+
```

颜色：`--color-brand` (indigo) / surface-0~3 / ink-0~4，dark mode 走 `dark:` 前缀。

### 6.3 关键页面

#### A. Coworkers 列表 `#/coworkers`

```
+- Coworkers ---------------------- [+ New coworker] -+
| +-------------------------------------------------+ |
| | Marketing Helper                  [...]         | |
| | claude * claude-opus-4-7                        | |
| | 3 MCPs * 2 skills * web+slack                   | |
| +-------------------------------------------------+ |
+-----------------------------------------------------+
```

#### B. Coworker 创建向导 `#/coworkers/new`（两步）

**Step 1：backend 卡片**
- 卡片下方实时显示该 tenant 的凭证状态
- `GET /api/v1/backends` x `GET /api/v1/tenant/credentials` 交叉显示

**Step 2：配置**
- Model 下拉只列兼容（按 backend 兼容矩阵过滤）
- name / description / system_prompt
- MCP servers / skills 多选

#### C. Coworker 详情 `#/coworkers/:id`

顶部 tabs：Overview / Skills / MCP / Bindings / Schedules / Conversations。每个 tab 是子路由 `#/coworkers/:id/skills` 等。

#### D. MCP Servers `#/mcp-servers`

列表 + 详情表单：name / type (sse|http) / url / **auth_mode (user|service|both)** / credential / extra_headers / tool_reversibility。

`auth_mode=user` 在 UI 显著标记 "requires user session"，并提示 "e2e 验收 pending — OIDC 分支合入后启用"。

#### E. Models `#/models`

只读，按 provider 分组卡片视图。Phase 1 不开 admin 写入 UI。

#### F. Credentials `#/credentials`

每 provider 一张卡，UNIQUE(tenant_id, provider)。`PUT` 时真 key 走 credential proxy 进 secret store，DB 只存 `credential_ref`。**响应永不回传 key 值**。

#### G. Skills `#/skills` / `#/skills/:id`

左：tenant catalog 列表。详情：分屏文件树 + 编辑器。SKILL.md 受保护（DELETE 单文件返 409）。

#### H. Bindings `#/bindings`

每个 coworker 一行，展开 web/slack/telegram 绑定状态。

#### I. Approvals `#/approvals`

```
+- Approvals ---- [Pending (3)] [Resolved] -----------+
| Send Slack message via slack-mcp                    |
|    Marketing Helper * 5min ago * requested by alice |
|    Args: {channel: #general, text: "..."}           |
|    [Approve] [Reject]                               |
+-----------------------------------------------------+
```

WS event `event.approval.required` 实时推；点 Approve/Reject 走 `POST /approvals/{id}/decide`。Phase 1 全 `auto_execute`，渲染占位提示。

Phase 3 smoke 在方案 A 多 bootstrap user 下跑：alice 发起 -> bob 审批，验 actor_user_id 落表正确。

#### J. 全局：reauth banner（架构保留）

任何页面顶部，监听 WS `event.run.requires_reauth` -> 显示：
```
Your session expired for some tools - [Re-login]
```

Bootstrap fast-path 下不会触发该 event（vault 不参与），banner 代码保留，e2e 验收推迟到 OIDC 分支。

---

## 7. Hot-load / per-call read 矩阵（L7）

每个配置 / 资源类型在 design 阶段就定策略，不允许"事后补 NATS 订阅"。

| 配置 | 策略 | 事件 / 路径 |
|---|---|---|
| `coworkers` 新增/删除 | JetStream -> orchestrator 内存字典 | `web.coworker.added`、`web.coworker.removed` |
| `coworker.system_prompt` 等编辑 | JetStream | `web.coworker.updated` |
| `coworker.model_id` 改变 | JetStream -> 重启 coworker | `web.coworker.restart` |
| MCP server 配置变更 | JetStream（已有） | `egress.mcp.changed` |
| coworker <-> mcp 关系变更 | JetStream | `web.coworker.mcp_changed` |
| Bindings 增删 | JetStream | `web.binding.added`、`web.binding.removed` |
| `tenant.approval_default_mode` | per-call DB read | 引擎每次 evaluate SELECT |
| `safety_rules` 变更 | per-call DB read（engine stateless）| 已是 |
| `approval_policies` 变更 | per-call DB read | 已是 |
| `tenant_model_credentials` | per-call DB read（credential_proxy 拉真值）| 已是 |
| `skills` 文件内容 | coworker 启动时一次性投影到 tmpfs | 不 hot-load |
| `skills` 启用/禁用关系 | JetStream + 重启 coworker | `web.coworker.skills_changed` |
| `tenant_model_credentials` 删除 | per-call DB read（下次请求即生效） | 已是 |

---

## 8. 安全 / 多租户

| 维度 | 设计 |
|---|---|
| 多租户隔离 | Postgres RLS + 显式 `WHERE tenant_id = $1`（INV-1 双层防御）|
| 凭证存储 | Envelope encryption：DB BYTEA 列存 Fernet 密文，master key 从 env 派生（详见 §8.1）；API list/get 永不回传明文 |
| 凭证注入 | credential_proxy 在 egress HTTP 层换真 key；容器内永远是 placeholder |
| User token 注入 | credential_proxy 在 `auth_mode=user` 路径查 conversation -> user -> vault，注入 Bearer（架构保留，e2e 推迟）|
| Coworker 容器隔离 | readonly rootfs / cap drop / userns / 可选 gVisor |
| Network | Internal=true bridge + dual-homed egress gateway；agent 无直连外网 |
| Auth | 生产路径 OIDC + external；dev 路径 bootstrap fast-path（含方案 A 多 user 扩展）|
| WS 鉴权 | 短 exp ticket (JWT) + 握手期资源化权限校验 |
| Safety pipeline | INPUT_PROMPT / PRE_TOOL_CALL / MODEL_OUTPUT 三阶段（已有）|
| Coworker 可见性 | Phase 1 全租户共享；预留 `created_by_user_id` 字段 |
| Coworker 编辑权限 | 创建者 + tenant admin |
| Skill 文件投影 | DB -> tmpfs -> bind mount read-only；容器内 SDK 扫描 |
| Container orphan cleanup | **image 白名单**，不是 name substring（INV-3）|
| IPC 反序列化 | filter unknown keys（INV-2），跨版本前向兼容 |

### 8.1 Envelope encryption（secrets at rest）

**Scope**：所有需要保密的 application-managed secrets。当前与 v1.1 直接相关的两个 vault：

| Vault | DB 表 | 列 | 装什么 |
|---|---|---|---|
| `TokenVault` (现有) | `oidc_user_tokens` | `refresh_token_encrypted` / `access_token_encrypted` BYTEA | OIDC user tokens |
| `CredentialVault` (02a 新加) | `tenant_model_credentials` | `credential_data` BYTEA | LLM provider API key（Anthropic / OpenAI / Bedrock / ...） |

**Channel binding token**（Slack/Telegram bot_token，目前 `channel_bindings.credentials` JSONB 明文）属于**已知技术债**，不在 v1.1 范围内迁，但应该走同套 vault primitive——留待独立 chore。

**模式**：Fernet (AES-128-CBC + HMAC-SHA256, IETF RFC) symmetric encryption + envelope。

```
write 路径：plain JSON  ─Fernet.encrypt(master_key)→  BYTEA 密文 → INSERT
read 路径：BYTEA 密文  ─Fernet.decrypt(master_key)→  plain JSON
```

**Master key 来源**：env var `CREDENTIAL_VAULT_KEY`（与 OIDC vault 用同一种 derive 机制 / `derive_fernet_key(secret)` helper）。

**为什么不直接每个 tenant credential 做成 k8s Secret**：

- N tenants × M providers = N × M 个 k8s Secret 对象，运维爆炸
- k8s Secret 不是 multi-tenant database 设计：没 RLS / 没事务 / 没 audit / 改一条要 kubectl
- 跨 region / 多集群同步噩梦
- Rotate 单 tenant 的某个 key 要 hit k8s API + propagation 延迟

**正确分层**：

```
[ Vault / AWS Secrets Manager ]   (optional prod 加固层)
            ↓ (External Secrets Operator pulls)
       [ k8s Secret ]               CREDENTIAL_VAULT_KEY=<32-byte master key>
            ↓ (envFrom / volumeMount)
   [ env var in pod ]                CREDENTIAL_VAULT_KEY=...
            ↓ (app boot)
   [ CredentialVault / TokenVault derive Fernet key ]
            ↓
   [ DB BYTEA columns ]              每行是该 master key 加密的密文
```

**这是 SaaS multi-tenant 加密的标准 envelope 模式**：infra ops 管 1 个 master key（k8s Secret 范畴），app 管 N×M 条 per-tenant data key（DB row 范畴）。AWS KMS / Google Cloud KMS / HashiCorp Transit 都是这套思路。

**Dev / Prod 一致性**：

| 环境 | Master key 来源 |
|---|---|
| Dev | shell env：`export CREDENTIAL_VAULT_KEY=<openssl rand -base64 32>` |
| Dev (docker-compose) | `.env` 文件，`compose.env_file` 注入 |
| Prod K8s | k8s Secret → pod env |
| Prod hardened | Vault → External Secrets Operator → k8s Secret → pod env |

**关键点**：application code **完全不区分 dev/prod**——都是从 `os.environ["CREDENTIAL_VAULT_KEY"]` 读。基础设施层负责注入。

#### 8.1.1 Rotation —— 推迟，不在 v1.1 范围

Master key rotation（如合规要求年度 rotate / 怀疑泄漏 / 离职处理）当前**不实现**。理由：

- LLM API key 是长期 static 凭证（Anthropic / OpenAI / Google 等用户在 console 手动管理）——**与 OIDC token 自动 refresh 不同**，不需要应用层的周期 rotation
- 项目当前 dev 阶段，无生产部署 / 无合规要求 / 无已知泄漏——0 当前需求
- Single Fernet → MultiFernet 是**非破坏性升级**：Fernet 密文格式与 MultiFernet 完全兼容（`MultiFernet([new, old]).decrypt(fernet_token)` 能解开历史 Fernet 写入的 token）。未来真要 rotation 时，vault 包装类几行代码改造即可，DB 数据不动

**用户能做的"换 key"**：直接 `PUT /api/v1/tenant/credentials/{provider}` 带新值——DB 行覆盖，不需要 application 层 rotation 机制。

如果未来真需要 master key rotation（合规 / incident response），单独 chore 加上即可。

#### 8.1.2 失败模式

| 场景 | 行为 |
|---|---|
| `CREDENTIAL_VAULT_KEY` 未设置 | app 启动 fail-loud（不 silent fallback 到明文） |
| 密文解不开（key 错 / 数据损坏） | raise + audit log；endpoint 返 503（不返 500，503 表 "we know this is broken, retry doesn't help"） |
| 想从加密迁明文（绝不应该发生） | 没这条路径——`credential_data BYTEA NOT NULL` schema 拒 |

#### 8.1.3 不变量（pinned tests）

| INV | 内容 |
|---|---|
| `INV-VAULT-1` | 未设 `CREDENTIAL_VAULT_KEY` 时 vault 构造抛 + app 启动 fail-loud |
| `INV-VAULT-2` | Encrypt 后 DB 行不含明文 substring（用一个 sentinel string 真写真读，grep BYTEA 转 utf8 不命中） |
| `INV-VAULT-3` | API list/get `tenant_model_credentials` 响应**永不**含明文字段（response_model 排除） |

---

## 9. 工作清单（按 Phase + 关键依赖）

### 9.1 前端

| 任务 | Phase |
|---|---|
| 路由 shell 抽离 `<rm-app-shell>`（sidebar + topbar） | 0 |
| 把现有 chat-panel 包进 shell，保留所有行为 | 0 |
| OpenAPI 生成 + `openapi-typescript` codegen -> `web/src/api/generated/` | 0 |
| WS 客户端按新协议重写（事件总线、重连先 GET /runs/{id}）| 1 |
| 全局 reauth banner（监听 `event.run.requires_reauth`，bootstrap 下不触发）| 1 |
| Coworkers 列表 + 创建向导 | 1-2 |
| Credentials 页面 | 2 |
| MCP 列表/编辑（含 `auth_mode=user` 标记）| 2 |
| Bindings 页面 | 2 |
| Schedules 只读 + 删除按钮 | 2 |
| Approvals 队列 + WS 事件触发 | 3 |
| Skills 文件树编辑器（共享 `SKILL_MANIFEST_NAME` 常量） | 3 |
| Coworker 详情页 skills/mcp 子面板 | 3 |
| Safety 页迁到 `/api/v1` | 4 |

### 9.2 后端

| 任务 | Phase |
|---|---|
| 建 `models`、`tenant_model_credentials`、`mcp_servers`、`coworker_mcp_servers`、`coworker_skills`、`runs` 表 + RLS | 0 |
| `coworkers.created_by_user_id` / `skills.created_by_user_id` 改 NULLABLE | 0 |
| `_bootstrap_actor_user_id()` helper + 503 错误码 | 0 |
| `core/backend_capabilities.py` + `GET /api/v1/backends` | 0 |
| `core/skills.py` 抽 `SKILL_MANIFEST_NAME` / `SKILL_FILE_PATH_RE` 单一常量 | 0 |
| `/api/v1` 路由 namespace + 鉴权复用 `webui/auth.py` | 0 |
| IPC dataclass deserialize 加 unknown-keys filter（`src/rolemesh/ipc/`）| 0 |
| Container orphan cleanup 审计 + image 白名单 | 0（**P0**）|
| **`webui/auth.py` 扩展：`BOOTSTRAP_USERS` env 多 user map（方案 A）+ 首次见到时 upsert users 表** | 0 |
| Coworkers CRUD `/api/v1/coworkers/*` + 启动校验链 | 1 |
| Conversations/Runs + WS 新协议 | 1 |
| `messages.run_id` 写入路径打通 | 1 |
| Run 状态机：枚举所有终止路径 + UPDATE | 1 |
| Models CRUD（admin only）+ migration 种子数据 | 2 |
| Credentials API + credential_proxy 集成 | 2 |
| MCP Servers CRUD | 2 |
| `coworkers.tools` 三阶段下线：(1)双写 (2)grep 全仓 reader 切换 (3)drop 列 | 2 |
| Credential_proxy `auth_mode=user` 路径：conversation -> user -> vault 注入（单测覆盖即合入；e2e 等 OIDC 分支）| 2 |
| `token_vault.exchange_for(audience)` 接口 + stub + 单测 | 2 |
| Approvals API 迁 `/api/v1` | 3 |
| Skills 表迁 per-tenant（双写期保留 `coworker_id` NULLABLE）| 3 |
| Safety API 迁 `/api/v1/safety/*` | 4 |
| DELETE 409 + 统一错误体贯穿全 phase | 1 起 |

### 9.3 `coworkers.tools` 三阶段下线流程（L2）

| Stage | 内容 | 验证 |
|---|---|---|
| 1 | schema 加 `coworker_mcp_servers` + `mcp_servers`，写入路径双写；reader 仍读 `tools` JSONB | 单元测试双写一致 |
| 2 | 所有 reader 切到关系表；`grep -rn "coworker.*\.tools\b\|cw\.tools\b" src/ tests/ scripts/ container/` 完成 reader 盘点；典型位置：runtime / router / channels / pi/mcp/client / tests/conftest / onboarding | grep 输出为空 |
| 3 | 单独 commit drop `coworkers.tools` 列 + DB migration | 全套 smoke 通过 |

---

## 10. 测试策略（L13 三层 + bootstrap-only smoke）

| 类型 | 谁跑 | 验证 |
|---|---|---|
| Unit | CI / 开发 | 函数 / 类正确性 |
| Integration | CI（含 docker testcontainer）| 多模块互动 |
| Live smoke | 每 Phase 末尾手动 | 真 NATS / 真 docker / 真 LLM API；**仅 bootstrap fast-path** |

每个 Phase 的 smoke 清单（全部走 bootstrap）：

| Phase | Smoke 内容 |
|---|---|
| 0 | `GET /api/v1/backends` 返代码常量；bootstrap token（单 + 多 user map）颁发 ws ticket；IPC forward-compat（升级 orchestrator 不升级 coworker 容器不崩）；container cleanup 不误删外来容器 |
| 1 | 真 Anthropic key 创建 coworker -> web 发消息（bootstrap as alice）-> token stream -> run.completed；run.{status, completed_at, usage} 写入；多 user `BOOTSTRAP_USERS` 切换 token 看到不同身份 |
| 2 | 配真 credential -> MCP server (`auth_mode=service`) attach -> coworker 使用 MCP tool；mcp_servers hot-reload；`auth_mode=user` MCP **不在 e2e smoke 范围**，单测覆盖 |
| 3 | approval require -> web Approve -> coworker 继续；**alice 发起 / bob 审批**（方案 A）；audit FK actor_user_id 落真 user UUID；skill 投影到容器 tmpfs；SKILL.md 受保护 |
| 4 | safety rule 触发 block -> decision 落表 -> UI 显示 |

**Tier 2 OIDC e2e smoke** 在 Keycloak + mock-tropos-mcp 分支合入后补一份独立 smoke 文档；本设计不依赖它存在。

---

## 11. 不变量清单（INV）+ pinned tests

```
INV-1  所有 tenant-scoped SQL = RLS policy + 显式 tenant_id 谓词
       test: test_tenant_isolation_belt_and_braces
       CI lint: grep tenant-scoped 表的 SQL 字符串，缺谓词则 fail

INV-2  IPC dataclass deserialize = filter unknown keys
       test: test_ipc_forward_compat_ignores_unknown_fields

INV-3  Container orphan cleanup = image whitelist
       test: test_cleanup_excludes_foreign_images
       手动 smoke: 起 kindest/node 容器跑 cleanup 验证不被删

INV-4  所有 audit FK actor_user_id 写入 = 真 user 或 503
       test: test_audit_write_with_bootstrap_no_owner_returns_503
       方案 A 多 user 下：test_audit_write_with_bootstrap_users_uses_real_uuid

INV-5  SKILL_MANIFEST_NAME 在 DB / Python / TS 三处共享
       test: test_skill_manifest_constant_consistency

INV-6  runs.{status, completed_at, usage} 在每条终止路径都被 UPDATE
       test: test_run_state_machine_all_paths（枚举 WS / cancel / schedule /
             approval_reject / container_crash / reauth_required）

INV-7  Wire enum 与 engine enum 在 handler 边界翻译
       test: TestResolvedDecisionMap（HTTP action / WS decision / engine outcome）
```

---

## 12. 命名陷阱清单（L5）

```
- OpenAPI $ref 名 != wire 字段名 -> 读 contract 时展开 $ref 看真实 name: / in:
- HTTP /decide.action != WS request.approval.decision != event.approval.resolved.decision
  (three closed enums evolve independently; handler translates at the wire edge; engine uses one enum)
- coworker.tools JSONB is not droppable in one shot; grep all readers first
- skills.coworker_id same story (per-coworker -> per-tenant dual-write window)
- "builtin" has two meanings: BuiltinProvider (stub) != bootstrap fast-path (live).
  They are separate at the code level; live smoke uses the latter.
- ADMIN_BOOTSTRAP_TOKEN single-token mode: user_id = "bootstrap" literal; all FKs must be NULLABLE or use helper.
  Plan A multi-user map: users land in users table; FKs use real UUIDs.
```

---

## 13. 错误码统一格式

```json
{
  "code": "RESOURCE_IN_USE",
  "message": "human-readable",
  "details": { ... }
}
```

| Code | HTTP | 含义 |
|---|---|---|
| `RESOURCE_IN_USE` | 409 | DELETE 被引用 |
| `BACKEND_INCOMPAT` | 422 | model.provider/family 与 backend 不兼容 |
| `MISSING_CREDENTIAL` | 422 | 创建 coworker 时 tenant 没配对应 provider 凭证 |
| `BOOTSTRAP_NEEDS_TENANT_OWNER` | 503 | bootstrap 路径写 audit 但 tenant 无 owner |
| `NEEDS_USER_PRESENCE` | 422 | scheduled run 不能调 `auth_mode=user` MCP |
| `WS_TICKET_EXPIRED` | 401 | ws 握手 ticket 过期 |
| `SKILL_MANIFEST_PROTECTED` | 409 | 试图单独删除 SKILL.md |
| `REAUTH_REQUIRED` | 401 | user-mode MCP 调用时 token vault refresh 失败（架构保留，bootstrap 下不触发）|

---

## 14. 明确不做 / 推迟

| 模块 | 状态 | 理由 |
|---|---|---|
| Schedules CRUD（写）| NO | coworker 通过 tools 自管理 |
| Tools list 端点 | NO | 无用户场景 |
| Markdown 9 类型 | NO | 用 Skill + system_prompt 替代 |
| MCP Generator | NO | 反模式 |
| `/schemas` 端点 | NO | OpenAPI 已是 schema 唯一源 |
| `/usage` `/replace` | NO | DELETE 409 + details |
| `AUTH_MODE=builtin` (BuiltinProvider) | LATER | 当前 stub；dev 用 bootstrap fast-path，prod 用 OIDC / external |
| Tier 2 OIDC e2e smoke | LATER | 等待 Keycloak + mock-tropos-mcp 分支合入；本文档所有 OIDC / token_vault / user-mode MCP 路径单测覆盖即合入，不依赖 IdP 跑起来 |
| 凭证健康检查 | LATER | 后续加 status / last_validated_at |
| Audit / Rollback | LATER | NATS 已就位 |
| Memories | LATER | harness 容器 home dir 天然支持 |
| Observability / Trace UI | LATER | 与 audit 一起做 |
| Coworker 混合可见性 | LATER | 预留 `created_by_user_id`，v2 加 visibility |
| Per-tenant 自定义模型 | LATER | `models.is_platform` 已预留 |
| 多 key per provider | LATER | UNIQUE(tenant_id, provider, label) |
| Token exchange (RFC 8693) | LATER | dev D1 -> 生产 D3，vault 加 `exchange_for(audience)` 接口 + stub 先合入 |
| Offline access for scheduled runs | LATER | Phase 1 拒绝；后续视需要开 |

---

## 15. 决策清单（开工前确认）

1. **整体方向**：hash router + `/api/v1` 与 `/api/admin` 并存 6 个月 + skill/MCP 三阶段渐进迁移 -> CONFIRMED
2. **从 Phase 0 起步**：先建表 + 不变量基建 + `GET /api/v1/backends` + L9 container cleanup 审计（**P0**） + bootstrap multi-user 扩展 -> CONFIRMED
3. **OpenAPI 先行**：先写 `web/openapi.yaml`，FastAPI 用 `response_model` 校验匹配；codegen 出 TS client -> CONFIRMED
4. **Dark mode**：现有 CSS 有 `--color-d-*` token 但无 toggle。Phase 1 跟系统 `prefers-color-scheme`（toggle 推迟）-> PROPOSED
5. **抽 `<rm-app-shell>`**：chat 也接入新 shell，统一布局收敛 -> CONFIRMED
6. **多 user smoke 方案**：方案 A（`BOOTSTRAP_USERS` env 多 user map，alice/bob 真落 users 表）-> CONFIRMED

---

## 附：Phase 0 启动 punch list（不变量 + 防雷基建）

按依赖顺序，应该是第一批 PR：

1. `core/backend_capabilities.py` + `GET /api/v1/backends`
2. `core/skills.py` 抽 `SKILL_MANIFEST_NAME` / `SKILL_FILE_PATH_RE` 常量
3. IPC dataclass deserialize 加 `_filter_unknown` mixin + pinned test
4. Container orphan cleanup 审计（grep `src/rolemesh/container/`）+ image 白名单 + pinned test
5. `_bootstrap_actor_user_id()` helper + `BOOTSTRAP_NEEDS_TENANT_OWNER` 错误码 + pinned test
6. **`BOOTSTRAP_USERS` env 解析 + `authenticate_ws()` 多 user 分支 + 首次见到时 upsert users 表 + pinned test**
7. Migration：新表（models / tenant_model_credentials / mcp_servers / coworker_mcp_servers / coworker_skills / runs） + RLS policy
8. Migration：`coworkers.model_id` / `coworkers.created_by_user_id`（NULLABLE）/ `skills.created_by_user_id`（NULLABLE）/ `messages.run_id`
9. `web/openapi.yaml` 初稿 + codegen pipeline
10. `<rm-app-shell>` 抽出 + chat 接入 + sidebar 占位入口（其它页跳 "coming soon"）
11. Bootstrap smoke 脚本（仅 fast-path 路径），含多 user 扩展验证

---

**文档结束。**
