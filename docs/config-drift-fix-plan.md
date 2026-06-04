# Config Drift — Analysis + Fix Plan

> **Scope**: 配置项 DB vs .env 的整体分类 + 4 个真实 drift 案例（设计说 DB、代码读 .env）+ 修复路线图。
> **Trigger**: 2026-05-25 用户审视 v2 cycle 完工后的配置面，要求 greenfield 姿态规划修复。
> **状态**: 分析归档；3 stage 修复待 chore session 执行。

## 0. TL;DR

v1.1 02a 给 multi-tenant credential 加了 schema + Fernet vault primitive；v2 cycle 加了 UI；**但 `src/rolemesh/egress/credential_proxy` 是 v0 single-tenant 设计，从未改造**。结果：UI 让用户配 credentials → 加密入 DB → **运行时从来不读**——agent 实际用的是 host process env var。多租户隔离 promise 当前**全部失效**。dev 阶段单 tenant 凑巧能跑（一把 ANTHROPIC_API_KEY 覆盖所有），prod 多租户立刻爆。

同根因下还有 3 个小 drift（model_id / max_concurrent_containers / mcp credential_ref）—— 累计 4 个 case 在本文档 §3 列详。

修复分 3 stage（§5）：
- **A** (small)：Pi `model_id` 已诊断 + 已写修 + 已写 8 tests，本文档外单独 commit/push
- **B** (CRITICAL)：credential_proxy 改 per-request DB lookup + 顺带修 mcp credential_ref（§5.B 详细规划）
- **D** (low priority)：`tenants.max_concurrent_containers` per-tenant limit（dev 阶段可推迟）

## 1. 调研方法

实测代码状态，不靠记忆：

- grep `os.environ.get` / `os.getenv` 全 src/ → **176 reads / 100+ 唯一 env vars**
- grep `CredentialVault` 在 egress/ → **0 callers**（关键发现）
- 对照 `src/rolemesh/db/schema.py` 23 张表
- 对照 design docs (`webui-backend-v1.1-design.md` §2 / §8.1) 写过的 DB 意图

调研工具数据存放：本文档 §3 引用具体行号，未来 chore session 直接 grep 验证。

## 2. 配置分类原则

### 2.1 该 `.env` 的（基础设施 + 进程级）

| 类别 | 例子 | 理由 |
|---|---|---|
| 数据库连接 | `DATABASE_URL` / `ADMIN_DATABASE_URL` | 引导基础（bootstrap chicken-and-egg） |
| 消息总线 | `NATS_URL` | 同上 |
| App master secret | `CREDENTIAL_VAULT_KEY` / `ROLEMESH_TOKEN_SECRET` / `WS_TICKET_SECRET` | 用于解 DB 中 BYTEA 的密钥本身——必须先于 DB 存在；k8s Secret 注入 |
| Dev bootstrap | `ADMIN_BOOTSTRAP_TOKEN` / `BOOTSTRAP_USERS` | dev/CI only；per-process identity |
| Feature mode | `AUTH_MODE` / `SAFETY_FAIL_MODE` | per-deployment 行为开关 |
| Container infra | `CONTAINER_BACKEND` / `CONTAINER_IMAGE` / `CONTAINER_MAX_*` / `CONTAINER_NETWORK_NAME` / `IDLE_TIMEOUT` | 单 deployment 容器 runtime 配置 |
| Web infra | `WEB_UI_HOST` / `WEB_UI_PORT` / `WEB_UI_DIST` / `WEBUI_BASE_URL` / `CORS_ORIGINS` | 单 deployment networking |
| Egress infra | `EGRESS_GATEWAY_*` / `EGRESS_UPSTREAM_DNS` / `CREDENTIAL_PROXY_HOST` / `CREDENTIAL_PROXY_PORT` | 单 deployment egress |
| OIDC IdP config | `OIDC_DISCOVERY_URL` / `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` / `OIDC_*` (15 个) | per-deployment IdP，不是 per-tenant |
| External JWT | `EXTERNAL_JWT_*` (8 个) | 同上 AUTH_MODE=external |
| Pi internal | `PI_AI_*` / `PI_CACHE_*` / `PI_PACKAGE_*` / `PI_*` (8 个) | Pi runtime 内部约定 |
| 日志 / 时区 | `LOG_LEVEL` / `TZ` | infra |

### 2.2 该 DB 的（per-tenant / per-coworker / per-user）

| 实体 | DB 表 | 内容 | 状态 |
|---|---|---|---|
| Tenant | `tenants` | name / plan / max_concurrent_containers | ✅ 表 ⚠️ 部分字段未消费（见 §3 D3） |
| Coworker | `coworkers` | name / folder / agent_backend / model_id / system_prompt / status / permissions | ✅ 表 ⚠️ model_id 对 Pi 未消费（见 §3 D2） |
| User | `users` | per-user identity | ✅ |
| Models 目录 | `models` | 平台级 provider × family × model_id | ✅ |
| LLM credentials | `tenant_model_credentials` | per-tenant Fernet-encrypted credential_data BYTEA | ✅ 表 + UI ❌ **运行时 0 callers**（D1） |
| MCP servers 注册 | `mcp_servers` | tenant 级 name / type / url / auth_mode / extra_headers / tool_reversibility / credential_ref | ✅ 表 ⚠️ credential_ref 未串到容器（D4） |
| Skills 目录 | `skills` + `skill_files` | per-tenant skill 包 | ✅ |
| Coworker ↔ MCP 绑定 | `coworker_mcp_servers` | enabled_tools 三态 | ✅ |
| Coworker ↔ Skill 绑定 | `coworker_skills` | enabled | ✅ |
| Safety rules | `safety_rules` | 护栏 | ✅ |
| Channel bindings | `channel_bindings` | per-coworker bot_token（**明文 JSONB**，已知技术债，独立 chore）| ⚠️ tech debt |
| OIDC tokens | `oidc_user_tokens` | Fernet 加密 refresh/access | ✅ |
| Runs | `runs` | run lifecycle | ✅ |

### 2.3 关键判据

**任何"per-tenant 隔离 promise"的数据 → DB**。因为 .env 是单 process 单值，无法表达 N 个 tenant 各自的配置。

**任何"用户运行时通过 UI 改"的数据 → DB**。`.env` 改了要重启进程。

**任何"启动 DB 之前就需要"的数据 → .env**。鸡生蛋问题：DB connection / master encryption key 不能存 DB 自己。

## 3. 真实 drift 4 条

按严重度排。

### 3.1 D1 — `tenant_model_credentials` 从未被消费（CRITICAL）

**现状**：
- 表 schema：`tenant_model_credentials.credential_data BYTEA NOT NULL`（v1.1 02a `src/rolemesh/db/schema.py:84`）
- Vault primitive：`src/rolemesh/auth/credential_vault.py` `encrypt_json()` / `decrypt_json()` 实现完整
- UI 写入：`src/webui/v1/credentials.py` PUT endpoint 正确 encrypt + INSERT
- pinned tests INV-VAULT-1/2/3 全绿
- **但 `src/rolemesh/egress/reverse_proxy.py` 直接 `os.environ.get("ANTHROPIC_API_KEY")`** —— 0 引用 CredentialVault

**证据**（grep 输出）：

```
$ grep -rn "CredentialVault\|from.*credential_vault" src/rolemesh/egress/
(empty — 0 lines)

$ grep -rn "ANTHROPIC_API_KEY\|OPENAI_API_KEY" src/rolemesh/egress/reverse_proxy.py
118:        ak = secrets.get("ANTHROPIC_API_KEY", "")
291:            (k, _os.environ.get(k, ""))
293:                "ANTHROPIC_API_KEY",
312:    auth_mode: AuthMode = "api-key" if secrets.get("ANTHROPIC_API_KEY") else "oauth"
478:            headers["x-api-key"] = secrets.get("ANTHROPIC_API_KEY", "")
534:    return "api-key" if _os.environ.get("ANTHROPIC_API_KEY") else "oauth"
```

**且**`src/rolemesh/egress/launcher.py` 启动时**把 host env LLM keys forward 到 gateway 容器**：

```
_FORWARDABLE: tuple[_ForwardSpec, ...] = (
    ...
    _ForwardSpec("ANTHROPIC_API_KEY"),
    _ForwardSpec("CLAUDE_CODE_OAUTH_TOKEN"),
    _ForwardSpec("ANTHROPIC_AUTH_TOKEN"),
    _ForwardSpec("PI_OPENAI_API_KEY"),
    _ForwardSpec("PI_GOOGLE_API_KEY"),
    _ForwardSpec("AWS_BEARER_TOKEN_BEDROCK"),
    _ForwardSpec("AWS_REGION", requires="AWS_BEARER_TOKEN_BEDROCK"),
)
```

**影响**：
- 多租户 LLM key 隔离**完全不工作** —— alice 配的 anthropic key 和 bob 配的都被忽略
- v1.1 02a 投入（schema + vault primitive + INV-VAULT-1/2/3 + UI）全部建在未连通的目的地
- UI 上"Set credential" 是 lie

**为何 dev 阶段未暴露**：单 tenant + 单 ANTHROPIC_API_KEY 覆盖所有需求，凑巧能跑。

### 3.2 D2 — `coworker.model_id` 对 Pi backend 不生效  ✅ shipped + simplified 2026-05-26

**现状**：

- DB 列存在（v1.1 02a + 00b）
- Wizard 让用户选 model_id 入 DB（v2-B）
- **修复已 shipped**：spawn 时查 DB，map 到 Pi-format string，注入到容器 env

**Shipped 历史**：
- `6eafd33` + `25834a5`（feat/ui-v2 上 PR30；原始 293 LOC，含 over-engineering bloat）
- `chore/config-db-truth` 简化 commit（净 -100 LOC；折叠 helper、3-branch → try/except、删 5 spec-confirming tests）

**最终态文件**：
- `src/rolemesh/agent/container_executor.py` — spawn 时 lookup coworker.model_id，try/except fallback
- `src/rolemesh/agent/executor.py` — `_pi_extra_env` inline 了模型 env 逻辑；`_DB_TO_PI_PROVIDER` 常量留存
- `src/rolemesh/container/runner.py` — `build_container_spec(pi_model_id_override=...)` 含 inline override 逻辑
- `tests/container/test_runner.py` — 4 个 mutation-resistant tests pin 公共 contract

### 3.3 D3 — `tenants.max_concurrent_containers`  ✅ already shipped pre-session (no work needed 2026-05-26)

**修订**：本节最初按"DB 列未被读取"立项；2026-05-26 chore session 1 准备执行时审计 `main`，发现 wiring 完整：

- `core/types.py:112` — `Tenant.max_concurrent_containers` 字段
- `db/tenant.py:67` — `get_all_tenants()` 从 DB 投影该列
- `main.py:417-418` — `_load_state()` 启动时 `for t in await get_all_tenants(): _state.tenants[t.id] = t`
- `core/orchestrator_state.py:103` — `can_start_container` 真的读 `tenant.max_concurrent_containers` 检查
- `core/orchestrator_state.py:108-118` — `increment_active`/`decrement_active` 维护 `tenant_active` dict
- `container/scheduler.py:94-95` — `GroupQueue._can_start` 调 `_orch_state.can_start_container`
- `main.py:1420` — `_queue = GroupQueue(transport=..., runtime=..., orchestrator_state=_state)`
- `container/scheduler.py:382, 408, 417, 438` — spawn / terminate 路径 inc/dec
- 已有 tests：`tests/test_multi_tenant_e2e.py` (test_global_limit_blocks_all_tenants 等), `tests/container/test_scheduler.py:136` (三级 concurrency)

**为何最初误判**：grep `GLOBAL_MAX_CONTAINERS` 看见 `OrchestratorState(global_limit=...)` 后误以为 `tenant.max_concurrent_containers` 没人读；实际两条限制并存（global + per-tenant），都生效。

**无须代码修改**。如未来发现真实 gap（如 hot-reload tenant 限额、admin endpoint 改限额后不生效），独立 chore 再处理。

**原始证据（保留供 archaeology）**：

```
$ grep -rn "max_concurrent_containers\|GLOBAL_MAX_CONTAINERS" src/rolemesh/
src/rolemesh/main.py:66:    GLOBAL_MAX_CONTAINERS,
src/rolemesh/main.py:132:_state: OrchestratorState = OrchestratorState(global_limit=GLOBAL_MAX_CONTAINERS)
src/rolemesh/db/schema.py:42:            max_concurrent_containers INT DEFAULT 5,
src/rolemesh/db/tenant.py: (CRUD only)
```

**影响**：tenant 隔离 limit 失效。dev 阶段无感（单 tenant）。生产多 tenant 一个 tenant 跑爆挤掉别人。

### 3.4 D4 — `mcp_servers.credential_ref` 不串到容器

**现状**：

- DB 有 `credential_ref TEXT`（v1.1 02a）
- v1 API write/read 字段（`src/webui/v1/mcp_servers.py`）
- **但 `McpServerSpec` IPC dataclass 不含此字段**——容器只收到 `name / type / url / tool_reversibility`

**证据**：

```
$ grep "credential_ref" src/rolemesh/ipc/protocol.py
(empty)

$ grep -B1 -A8 "class McpServerSpec" src/rolemesh/ipc/protocol.py
@dataclass(frozen=True)
class McpServerSpec:
    name: str
    type: str
    url: str
    tool_reversibility: dict[str, bool] = field(default_factory=dict)
```

**影响**：service-mode MCP 的 credential 完全没生效。v2-C retired 把 user-mode 路径推到 OIDC 分支，**但 service-mode 这条也 broken 没人注意**。

## 4. 根因

**v1.1 02a 加了 multi-tenant credential schema + vault primitive；但 `src/rolemesh/egress/credential_proxy + reverse_proxy + launcher` 是 v0 时代 single-tenant 设计，从未改造**。

整个 egress 子系统假设："credentials 在 host env，启动时 forward 进 gateway 容器，runtime 共用一份"。引入 DB credentials 后两层未串：

```
DB (per-tenant)      CredentialVault                   credential_proxy
  credential_data    encrypt_json/decrypt_json         os.environ.get(...)
        ✅ 02a              ✅ 02a              ???           ❌ v0
        |                    |                                |
        +----- 写入路径 ------+                                |
                                                              |
                                  ← (空缺) → 读取路径从未连通 →
```

**v2 cycle 完全没碰这层**——v2 是纯前端。所以 v1.1 vault 投资 + v2 UI credential dialog + per-provider extras 全都 wire 到一个未连通的目的地。

## 5. 修复规划（greenfield，无 compat）

### 5.A — D2 收尾（Pi model_id wiring）

**状态**：已诊断 + 已写修 + 已写 8 tests，working tree 未 commit。

**动作**：

```bash
git add src/rolemesh/agent/container_executor.py src/rolemesh/agent/executor.py \
        src/rolemesh/container/runner.py tests/container/test_runner.py
git commit -s -m "fix(runtime): Pi backend now reads coworker.model_id from DB

Previously the Pi container always used PI_MODEL_ID from host
process env, ignoring the model_id that the v2-B wizard wrote
to coworkers.model_id. The wizard UI was lying.

This commit:
- Extracts pi_format_model_id() / pi_env_for_model_id() as pure
  functions so per-spawn resolution is possible (was only callable
  at module load).
- ContainerAgentExecutor.spawn() looks up coworker.model_id against
  the models table, formats with pi_format_model_id(), and passes
  to build_container_spec() as pi_model_id_override.
- build_container_spec() honors the override when set, falling
  back to host .env PI_MODEL_ID otherwise (preserves evaluation
  CLI behavior which has no coworker context).
- 8 pinned tests cover override scenarios + Bedrock provider
  rename + pass-through for openai/anthropic/google.

Three graceful fallback paths (no model_id / orphan / DB error)
log + degrade, never block spawn. The bedrock provider in DB
becomes amazon-bedrock in Pi's PI_MODEL_ID format at the boundary.

Tracked in feat/ui-v2 because the v2-B wizard surface made the
gap visible. This is one of four config drift cases inventoried
in docs/config-drift-fix-plan.md — the other three (credential
proxy DB migration, mcp credential_ref propagation, per-tenant
container limit) are scoped as separate chore sessions."

git push origin feat/ui-v2
```

**估算**：~280 LOC（已写完）+ 5 分钟 commit/push。

### 5.B — D1 + D4 合并（credential_proxy DB migration）

**Critical**。一个 chore session 跑完两个 drift（同根因 + 同代码区域）。

#### 5.B.1 目标

`credential_proxy` 改成 **per-request DB lookup**：

```python
# 新设计（伪代码）
async def get_credential_for_request(request) -> dict:
    tenant_id = await resolve_tenant_id_from_request(request)
    provider  = infer_provider_from_request_path(request)
    return await credential_cache.get_or_load(tenant_id, provider)

class CredentialCache:
    async def get_or_load(self, tenant_id, provider) -> dict:
        # 1 分钟 TTL（Fernet decrypt 不便宜，per-request 调 vault 浪费）
        key = (tenant_id, provider)
        if cached := self._cache.get(key):
            if cached.expires_at > now():
                return cached.value
        blob = await get_credential_data(tenant_id, provider)  # DB
        value = self._vault.decrypt_json(blob)
        self._cache[key] = CacheEntry(value, expires_at=now()+60s)
        return value
```

#### 5.B.2 关键问题：`tenant_id` 从哪儿来

3 种解法（按推荐度）：

1. **从 conversation_id header 反查**（推荐）
   - agent 容器走 credential_proxy 时带 `X-RoleMesh-Conversation-Id` header
   - proxy 查 `conversations` → `coworker_id` → `coworker.tenant_id`
   - 与 02c retired 设计的 conversation_id header 思路一致；service-mode 也用得上
2. **从 job_id 反查**
   - 容器启动时已注入 `JOB_ID` env，agent 出站可以带 header
   - proxy 查 `runs / jobs` → `tenant_id`
3. **从容器本身 metadata** (Docker labels)
   - 启动容器时 `Labels.tenant_id = ...`
   - proxy 通过 source IP / container name → docker inspect 拿
   - 复杂、慢

选 1。比 02c retired 用户级 header 更简单（service-mode 只需 conversation 反查，无 OIDC user vault 复杂性）。

#### 5.B.3 同步修 D4（mcp credential_ref）

D1 路径打通后，service-mode MCP 一样走：

```
agent → credential_proxy (X-RoleMesh-Conversation-Id) → DB lookup →
   if request to /proxy/mcp/<server-name>:
       lookup mcp_servers.credential_ref + tenant_id → decrypt → inject
   else (LLM provider):
       lookup tenant_model_credentials → decrypt → inject
```

`McpServerSpec` IPC dataclass **不需要** `credential_ref`——容器只需调 proxy URL，proxy 端按 server name 查 DB。这是更干净的设计：容器内代码完全不知道 credential 形态。

#### 5.B.4 Stage B 工作清单

- credential_proxy 改造：`reverse_proxy.py` 内的 `secrets.get("ANTHROPIC_API_KEY")` 等全部换成 cache-backed DB lookup
- `egress/launcher.py` 停止 forward LLM keys 到 gateway 容器（_FORWARDABLE 删 LLM-related 行；保留 EGRESS_UPSTREAM_DNS 这种 infra forward）
- credential_proxy 加 conversation_id header 反查 helper
- credential_proxy 加 in-memory cache 层（1 分钟 TTL，per-tenant per-provider key）
- MCP server credential 路径：`POST /proxy/mcp/<server>/...` 时按 server name + tenant_id 查 `mcp_servers.credential_ref` → 注入对应 header
- Pinned tests:
  - tenant A 配 anthropic key → tenant A 请求注入 A 的 key
  - tenant B 配 anthropic key → tenant B 请求注入 B 的 key
  - 同 tenant 两次请求 → cache hit（vault decrypt 只调 1 次）
  - 1 分钟后 cache 过期 → 再 decrypt
  - tenant 没配 credential → 401 + 结构化 error (`MISSING_CREDENTIAL`)
  - Mutation: 改 reverse_proxy 让它 fallback host env → tenant 隔离测试应该红

**估算**：~600-900 LOC（含 tests）。2x 系数 → ~1500-1800 实际。**比 v2-B 一个 session 略大；可能需要拆 2 个 session**。

#### 5.B.5 风险

- **Single-tenant fallback 不要建**——这就是当前 bug 的根源。strict: 无 tenant_id 直接 401，不允许"凑巧能跑"
- **现有 host env LLM keys 还要不要留**——不要，但 evaluation CLI / 旧 ops script 可能依赖；session 内 grep 确认
- **conversation_id header 信任问题**——02c retired prompt 当时讨论过，容器内 agent 可伪造任意 header。需要 credential_proxy **验证 header 中的 conversation_id 真对应当前 active container**（chore A `get_active_container_name` 比对）
- **缓存 TTL race**——超过 TTL 但请求 in-flight 时不要 panic，刷一次重试

### 5.C — D3（per-tenant container limit）  ✅ already shipped pre-session

见 §3.3 修订段。`OrchestratorState.can_start_container` 已经按 `tenant.max_concurrent_containers` 检查；`_load_state()` 启动时 load 所有 tenant；`tenant_active` dict + `increment_active`/`decrement_active` 都 in 位；`GroupQueue` 已绑 `_orch_state`。无须代码。

### 5.D — 不建议改

不要冲动迁这些到 DB：

- `CONTAINER_*` env vars——dev 阶段一致；prod per-tenant 容器配置是 v3+ 工作
- `OIDC_*` env vars——per-deployment IdP，本来不该 per-tenant
- `PI_AI_*` env vars——Pi runtime 内部约定
- `LOG_LEVEL` / `TZ`——infra
- `WEB_UI_*`——单 deployment
- `tenant.plan` 配额体系——用户当前没用到（YAGNI）
- per-tenant feature flag table——YAGNI

也**不建议**做 channel binding credentials 迁 vault——已知技术债但与本文档 4 个 drift 不同根因，独立 chore。

## 6. 不变量（防回归）

修完后加 lint / 测试钉死：

- **INV-CRED-1**: `grep "ANTHROPIC_API_KEY\|OPENAI_API_KEY\|PI_OPENAI_API_KEY" src/rolemesh/egress/` 必须只剩 docstring / 注释；任何 `os.environ.get` 读 LLM key 在 egress/ 下都禁止
- **INV-CRED-2**: 跨 tenant test —— tenant A 真请求注入 A 的 key（不是 B 的，不是 host env 的）
- **INV-CRED-3**: 未配 credential 时 `MISSING_CREDENTIAL` 401 不 silent fallback（pinned test 验）
- **INV-CRED-4**: `_FORWARDABLE` 在 `egress/launcher.py` 不含 LLM-provider key（lint 测试）
- **INV-CONFIG-1**: 任何 v2+ 加进 `coworkers` / `tenants` / `mcp_servers` / `tenant_model_credentials` 的字段必须有 runtime consumer（lint 警告）；DB 列存在但 0 reader 是当前 4 个 drift 的共同迹象

## 7. 修复时间线建议

| Stage | Scope | LOC | 优先级 | 何时做 |
|---|---|---|---|---|
| A | D2 commit + 后续 simplify | 280 ship + -100 simplify | high | ✅ done 2026-05-26 |
| B | D1+D4 credential_proxy 改造 | 1500-1800 | **CRITICAL** | 下个独立 chore session（与 v2 / v1.1 平级）|
| C | D3 per-tenant limit | 0 | low | ✅ already shipped pre-session（见 §3.3 修订）|

**不要等 v3 整 cycle 来做 B**——B 是当前真 bug，每多等一天 prod 部署多一份风险。

## 8. 与 v1.1 / v2 cycle 的关系

- v1.1 02a 落了 schema + vault primitive **但没落 consumer**——这是个隐藏的"半成品" pattern，本文档 §3 4 个 drift 全是它的实例
- v2 cycle 完全没碰 backend，但 UI 让 drift **从"潜在 bug"变成"用户感知 lie"**（点 Save Credential 后 UI 显示"Saved"但运行时无效）
- **v3 cycle 的 backend 工作核心**就应该是 Stage B + retired 02c user-mode 链路 + 02c retire 时记的 3 个 backend chore（user-scoped WS subject 等）

## 9. 参考

- [`webui-backend-v1.1-design.md`](./webui-backend-v1.1-design.md) §2.1 (DB schema) / §8.1 (envelope encryption)
- [`webui-backend-v1.1-sessions/02a-models-credentials-mcp.md`](./webui-backend-v1.1-sessions/02a-models-credentials-mcp.md) Findings（Vault primitive 实际签名）
- [`webui-backend-v1.1-sessions/02c-credential-proxy-user-mode.md`](./webui-backend-v1.1-sessions/02c-credential-proxy-user-mode.md) Retired session（user-mode 推迟理由）
- [`webui-backend-v1.1-sessions/04-safety-ui.md`](./webui-backend-v1.1-sessions/04-safety-ui.md) Findings v1.1 retro（"半成品 pattern" 教训）
