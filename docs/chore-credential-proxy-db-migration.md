# Chore Session 2 — credential_proxy DB migration (D1 + D4) — Option A

| field | value |
|---|---|
| Branch | `chore/config-db-truth`（off main，session 1 已先在此分支跑过 D2+D3） |
| Prerequisites | Session 1 done（D2 simplify + D3 fix 在同分支前两个 commit） |
| Estimated PRs | 4 commits |
| Estimated LOC | ~400-500（Option A：container labels；不做 header injection 省 ~300 LOC vs 原 Option C 设计）|
| Status | not started |

> **Trigger**：`docs/config-drift-fix-plan.md` §3 D1（CRITICAL）+ D4。当前 multi-tenant LLM credential 隔离**完全不工作**——UI 上 user 配的 anthropic key 加密入 DB 后从未被 runtime 读取；agent 实际用 host process 的 `ANTHROPIC_API_KEY` env var，所有 tenant 共用一份。dev 单 tenant 凑巧能跑，prod 立刻爆。
>
> **2026-05-26 架构 pivot**：从 Option C（agent 出站塞 `X-RoleMesh-Conversation-Id` header + proxy 反查 + 验证）改成 **Option A（container labels at spawn + proxy 通过 source IP 读 labels）**。理由：
> - 身份是**结构性**的（container 物理上不能改自己的 labels / IP），不是**传输性**的（header 可伪造）
> - **agent 端 0 改动** —— Pi / Claude SDK 都不用加 header 注入（省 ~300 LOC）
> - **审计天然**——proxy 每请求自带 container_id
> - **复合未来 OIDC**：user_id 加进现有 labels，user-mode 是 `+1 label + 1 if 分支`，不是重新设计
> - 复用 chore A 已有的 Docker inspect 基础设施

## Goal

把 `src/rolemesh/egress/` 子系统从 **v0 single-tenant host-env-based** 改造成 **multi-tenant DB-based** credential 注入，通过 container labels 解析 tenant 身份：

1. **spawn 时给 container 加 3 个 labels**（`rolemesh.tenant_id` / `rolemesh.coworker_id` / `rolemesh.run_id`）
2. **`IdentityResolver`**：proxy 收请求 → `request.client.host` → docker inspect → labels → `ContainerIdentity(tenant_id, coworker_id, run_id)`
3. **`CredentialResolver`**：`(tenant_id, provider)` → DB → `CredentialVault.decrypt_json()` → 真 key；1 分钟 in-memory cache
4. **`reverse_proxy.py` 改造**：所有 `os.environ.get("ANTHROPIC_API_KEY")` 等替换为 `resolver.resolve_llm(identity.tenant_id, provider)`
5. **`egress/launcher.py` `_FORWARDABLE` 删 LLM 行**：保留 `EGRESS_UPSTREAM_DNS` 等 infra
6. **MCP service-mode 同款路径**：按 `(tenant_id, mcp_server_name)` 查 `mcp_servers.credential_ref` → 注入
7. **INV-CRED-1/2/3/4 pinned tests + lint script** 防回退

**不在范围**：user-mode MCP（OIDC vault）—— 02c retired 工作；Option A 让那条路径变成 future "+1 label + 1 if"，但仍是独立 chore。

## Required reading

1. [`docs/config-drift-fix-plan.md`](./config-drift-fix-plan.md) §3 D1/D4 + §5.B（修复设计）
2. [`docs/webui-backend-v1.1-design.md`](./webui-backend-v1.1-design.md) §8.1 envelope encryption
3. **v1.1 02a Findings** § "CredentialVault" —— vault primitive 实际接口
4. **2026-05-26 conversation 关于 Option A vs C** —— 架构选择理由（保留在 git blame 的 commit message）
5. **chore A** (`src/rolemesh/orchestration/run_cancel_subscriber.py` + `src/rolemesh/container/scheduler.py:get_active_container_name`) —— Docker inspect 基础设施 + 容器映射 pattern
6. 现状代码：
   - `src/rolemesh/egress/reverse_proxy.py` —— 当前直接 `os.environ.get(...)` 的 LLM key 注入点（grep `ANTHROPIC_API_KEY`）
   - `src/rolemesh/egress/launcher.py` —— `_FORWARDABLE` host env forward 名单
   - `src/rolemesh/container/runner.py` —— `build_container_spec` (spawn 时加 labels 处)
   - `src/rolemesh/container/docker_runtime.py` —— Docker label 透传
   - `src/rolemesh/auth/credential_vault.py` —— `decrypt_json()` 方法
   - `src/rolemesh/db/model.py` —— `get_credential_data(tenant_id, provider)` 已存在
   - `src/rolemesh/db/mcp_server.py` —— `credential_ref` 字段查询
   - 内部 bridge 网络配置（grep `agent-network` / `Internal=true`）—— 验证 per-container unique IP

## 概念定位：身份结构性而非传输性

| 方案 | 身份来源 | agent 是否需改 | 伪造可能 |
|---|---|---|---|
| **Option A**（本 chore）| container labels（orchestrator 烙）| 0 改动 | 物理上不可——container 无法改自己 labels |
| Option C（已 cut）| HTTP header（agent 出站塞）| Pi + Claude SDK 各加注入点 | 可伪造，需 proxy 验证 |

**核心 invariant**：proxy 永远不信任 application-level 数据来决定 tenant 身份；只信任 infrastructure-level metadata（labels via docker inspect）。

## 显式禁令（防 v2-A / D2 bloat 重演）

- ❌ **不加 `X-RoleMesh-Conversation-Id` header** —— Option C 路径已 cut；agent 端 0 改动
- ❌ **不抽 `CredentialPolicy` / `IdentityProvider` 抽象类** —— 单一来源直接 dict + function
- ❌ **不写 spec-confirming tests** —— "anthropic 用 x-api-key header" 这类是 spec 验证不是 invariant 守护
- ❌ **不引入 LRU / cache library** —— plain dict + TTL 够 dev 阶段
- ❌ **不重构 `reverse_proxy.py` 的非 credential 部分**（routing / logging / response handling 等不动）
- ❌ **不做 evaluation CLI 兼容** —— CLI 不走 proxy（已确认），无需特殊路径
- ❌ **不做 user-mode OIDC vault** —— 02c retired 工作；Option A 让 future user-mode 是 trivial extension 不是本 session
- ❌ **不做 channel_bindings 迁 vault** —— 独立技术债 chore
- ❌ **不做 cache invalidation 复杂逻辑** —— 1 分钟 TTL 是简单粗暴的 invalidation；不订阅 NATS event。credential PUT 后用户体感"1 分钟内生效"可接受
- ❌ **不允许 silent fallback host env** —— missing credential 必 401 `MISSING_CREDENTIAL`，不许 fallback
- ❌ **不删 `ANTHROPIC_API_KEY` 等在 dev `.env`** —— dev 开发者本机可能仍 export（与 evaluation CLI 兼容）；只是 credential_proxy 不再消费
- ❌ **不修 docker_runtime 接口** —— Docker labels 透传应该是 1 行参数（spec 已支持 labels）
- ❌ **不加 dynamic label update**（mid-run 改 labels）—— container 一生不可变

## Scope — PR breakdown

### PR 1 (commit 1) — spawn 时给 container 加 labels (~30 LOC)

**Goal**：orchestrator 在 spawn 时给每个 agent container 烙 3 个 labels。

子任务：

1. **`src/rolemesh/container/runner.py` `build_container_spec`**:
   ```python
   labels = {
       "rolemesh.tenant_id": str(coworker.tenant_id),
       "rolemesh.coworker_id": str(coworker.id),
       "rolemesh.run_id": run_id,
   }
   ```
   加进 `ContainerSpec` 的 labels 字段（如果 spec 已有 labels 字段就 append；如果没有就加）

2. **`docker_runtime.py` create_container**：传 `Labels` 参数给 Docker API
   - aiodocker 的 ContainerConfig 接 `Labels: dict[str, str]`
   - 验证一行透传即可（如果已有透传机制就不动）

3. **1 test**（mutation-resistant）：
   - `test_spawn_writes_rolemesh_labels` —— spawn 后 mock docker inspect 看到 3 个 labels 都存在 + 值正确

**Commit message 模板**：

```
feat(spawn): label agent containers with tenant_id / coworker_id / run_id

Prerequisite for Option A credential routing (next 3 commits):
the egress proxy will read identity from container labels rather
than from agent-injected headers. Labels are immutable from inside
the container — physically impossible to spoof.

build_container_spec emits three labels at spawn:
- rolemesh.tenant_id
- rolemesh.coworker_id
- rolemesh.run_id

Pinned by test_spawn_writes_rolemesh_labels (mutation-resistant:
remove any label and the test fails). No behavior change in this
commit — labels are dormant until PR 2 reads them.

Part of docs/config-drift-fix-plan.md §3 D1/D4 fix via Option A.
```

### PR 2 (commit 2) — `IdentityResolver` from source IP (~80 LOC)

**Goal**：proxy 从 source IP 读 container labels，返 `ContainerIdentity`。

子任务：

1. **新建 `src/rolemesh/egress/identity_resolver.py`**:
   ```python
   @dataclass(frozen=True)
   class ContainerIdentity:
       tenant_id: str
       coworker_id: str
       run_id: str

   class UnknownSourceError(Exception):
       """Source IP doesn't map to any rolemesh-labeled container."""

   class IdentityResolver:
       def __init__(self, docker_client, *, cache_ttl_seconds: int = 30):
           self._docker = docker_client
           self._cache: dict[str, _CacheEntry] = {}  # source_ip → identity
           self._ttl = cache_ttl_seconds

       async def resolve(self, source_ip: str) -> ContainerIdentity:
           if cached := self._cache.get(source_ip):
               if cached.expires_at > now():
                   return cached.identity
           container = await self._docker.containers.find_by_ip(source_ip)
           # 或更具体：list all containers + match by NetworkSettings.IPAddress
           if container is None:
               raise UnknownSourceError(source_ip)
           labels = container.labels
           identity = ContainerIdentity(
               tenant_id=labels["rolemesh.tenant_id"],
               coworker_id=labels["rolemesh.coworker_id"],
               run_id=labels["rolemesh.run_id"],
           )
           self._cache[source_ip] = _CacheEntry(identity, now() + self._ttl)
           return identity
   ```

2. **`find_by_ip` 实现**（如果 aiodocker 没原生 helper）：
   - List all containers + match `NetworkSettings.Networks[<bridge>].IPAddress`
   - 缓存 list 30 秒（avoid per-request listing 整 Docker daemon）
   - Container 重启 IP 变 → cache miss → 重 list 即可

3. **3 mutation-resistant tests**：
   - `test_resolve_returns_identity_from_labels` —— mock docker，验返 3 个 label 值
   - `test_resolve_unknown_ip_raises` —— IP 不对应任何 container
   - `test_resolve_cache_hit` —— 同 IP 第二次调用不打 docker（mock call_count == 1）

**Commit message 模板**：

```
feat(egress): IdentityResolver reads tenant from container labels

Translates source IP → ContainerIdentity by docker inspect. Cache
30s; container lifetime is short, IPs don't reuse rapidly.

Three failure modes are explicit:
- UnknownSourceError: source IP isn't a rolemesh-labeled container
  (operator script, leaked test container, network spoofing).
- Missing label key: KeyError surfaces as 500 to fail loudly —
  labels are spawned by build_container_spec and must be present.
- Docker daemon unreachable: bubbles up as 503 from proxy handler.

This is the identity layer for Option A. Next commits use it.
```

### PR 3 (commit 3) — `CredentialResolver` + `reverse_proxy.py` 改造 (~250 LOC)

**Goal**：proxy 所有 LLM key 注入从 host env 换到 DB-via-vault。

子任务：

1. **新建 `src/rolemesh/egress/credential_resolver.py`** (~80 LOC):
   ```python
   class MissingCredentialError(Exception):
       def __init__(self, tenant_id: str, provider: str):
           self.tenant_id = tenant_id
           self.provider = provider

   class CredentialResolver:
       def __init__(self, vault: CredentialVault, *, cache_ttl_seconds: int = 60):
           self._vault = vault
           self._cache: dict[tuple[str, str], _CacheEntry] = {}
           self._ttl = cache_ttl_seconds

       async def resolve_llm(self, tenant_id: str, provider: str) -> dict:
           key = (tenant_id, f"llm:{provider}")
           if cached := self._cache.get(key):
               if cached.expires_at > now():
                   return cached.value
           blob = await get_credential_data(tenant_id, provider)
           if blob is None:
               raise MissingCredentialError(tenant_id, provider)
           value = self._vault.decrypt_json(blob)
           self._cache[key] = _CacheEntry(value, now() + self._ttl)
           return value

       async def resolve_mcp(self, tenant_id: str, mcp_server_name: str) -> dict | None:
           # 同 pattern；mcp_servers.credential_ref 解 vault
           ...
   ```

2. **`reverse_proxy.py` 改 ~6-10 个注入点**：
   - 每个 handler 入口：
     ```python
     identity = await identity_resolver.resolve(request.client.host)
     # provider 从 path / Host header 推断
     try:
         cred = await credential_resolver.resolve_llm(identity.tenant_id, provider)
     except MissingCredentialError:
         return error_response(401, "MISSING_CREDENTIAL", ...)
     # 用 cred dict 注入 header
     headers["x-api-key"] = cred["api_key"]
     ```
   - **删除**所有 `os.environ.get("ANTHROPIC_API_KEY")` / `secrets.get("ANTHROPIC_API_KEY")` 等读 host env LLM key 的位置
   - Bedrock region 从 `cred["extras"]["region"]` 拿（v2-B 已存）；不再从 host AWS_REGION

3. **`egress/launcher.py` `_FORWARDABLE` 删 LLM 行**：
   - 删：`ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` / `PI_OPENAI_API_KEY` / `OPENAI_BASE_URL` / `PI_GOOGLE_API_KEY` / `GOOGLE_BASE_URL` / `AWS_BEARER_TOKEN_BEDROCK` / `AWS_REGION`
   - 保留：`EGRESS_UPSTREAM_DNS` 等真 infra

4. **不订阅 NATS event 做 cache invalidate** —— 1 分钟 TTL 简单粗暴；用户 PUT 后体感"1 分钟内生效"可接受。**Findings 段记录这是有意决定**

5. **5 mutation-resistant tests** (`tests/egress/test_reverse_proxy_db_credentials.py`):
   - `test_tenant_a_request_injects_tenant_a_key` —— mock vault + DB，验跨 tenant 隔离
   - `test_missing_credential_returns_401_not_fallback` —— mutation test：临时改回 fallback host env → 测试红
   - `test_cache_hit_skips_vault_decrypt` —— 同 (tenant, provider) 2 次请求，vault.decrypt_json 被调 1 次
   - `test_bedrock_region_from_db_not_host_env` —— Bedrock 请求 region 来自 cred["extras"]["region"]
   - `test_unknown_source_ip_returns_401` —— spoofed / non-rolemesh container

**Commit message 模板**：

```
feat(egress): reverse_proxy reads per-tenant credentials from DB

Replaces all os.environ.get("*_API_KEY") in reverse_proxy.py with
CredentialResolver lookups keyed on (tenant_id, provider). Identity
comes from the source IP via IdentityResolver (previous commit).

Removes 10 LLM-key forwards from launcher.py _FORWARDABLE —
gateway container no longer needs host env LLM keys.

Cache: plain dict + 1-minute TTL. No NATS invalidation event;
"new credential takes up to 1 minute to apply" is acceptable for
dev stage. Production tightening (event-driven invalidation) is
a separate chore if needed.

Missing credential → 401 MISSING_CREDENTIAL. No silent fallback
to host env. Mutation test pinned: changing the handler to
fallback fails test_missing_credential_returns_401_not_fallback.

Bedrock region now comes from credential extras (v2-B credential
dialog already stores it). The AWS_REGION env forward is removed.

This is the core D1 fix. D4 (MCP service-mode credentials) is
the next commit; both flow through the same resolver pattern.
```

### PR 4 (commit 4) — MCP service-mode credential + INV-CRED lint (~80 LOC)

**Goal**：service-mode MCP credentials 同款路径 + 防回退 lint。

子任务：

1. **`reverse_proxy.py` MCP routing 加 credential resolve**：
   - 路径前缀 `/proxy/mcp/<server-name>` 触发
   - `identity = await identity_resolver.resolve(request.client.host)`
   - `cred = await credential_resolver.resolve_mcp(identity.tenant_id, server_name)`
   - cred is None（auth_mode != service 或 credential_ref unset）→ pass-through，不注入
   - cred 有 → 按 server.auth_mode 注入对应 header（看 02a Findings）

2. **`scripts/lint-no-host-env-llm-keys.py`** (~40 LOC):
   - grep `src/rolemesh/egress/` 找 `os.environ.get` 读名字含 `*_API_KEY` / `*_OAUTH_TOKEN` / `*_AUTH_TOKEN` / `BEARER` 的
   - 例外白名单走 `# inv-cred-1-ok: <reason>` 注释豁免
   - 命中输出 file:line + 总数；exit 1
   - 加进 `web/package.json` scripts 或 pyproject 的 test runner

3. **2 tests** (`tests/egress/test_mcp_credentials.py` + `tests/test_lint_no_host_env.py`)：
   - `test_service_mode_mcp_injects_credential` —— mock vault，验 mcp 请求注入正确 header
   - `test_lint_catches_host_env_llm_key_read` —— 临时加一个 `os.environ.get("ANTHROPIC_API_KEY")` 在测试文件外的 egress 模块 → lint 红（变异测试）

**Commit message 模板**：

```
feat(egress): service-mode MCP credentials via DB + INV-CRED lint

Mirrors the LLM credential path for MCP service-mode. Same
CredentialResolver primitive, keyed on (tenant_id, mcp_server_name)
rather than (tenant_id, provider).

INV-CRED-1: scripts/lint-no-host-env-llm-keys.py blocks any new
os.environ.get("*_API_KEY") in src/rolemesh/egress/. Existing
violations (none expected after PR 3) get `# inv-cred-1-ok` allow
comment.

Closes D4 from config-drift-fix-plan §3. mcp_servers.credential_ref
column finally has a runtime consumer.

Updates docs/config-drift-fix-plan.md to mark D1 + D4 shipped.
```

## Acceptance criteria

- [ ] spawn 后 docker inspect 看到 3 个 rolemesh.* labels（PR 1）
- [ ] `IdentityResolver` 解析 source IP → identity（PR 2，单测 + 手动 smoke）
- [ ] tenant A 配 anthropic key K_A、tenant B 配 K_B → 各请求注入各的 key（PR 3 手动 smoke：在 Anthropic console / dashboard 看到两个不同的 API key 使用）
- [ ] Missing credential → 401 `MISSING_CREDENTIAL` + 结构化 error，不 silent fallback host env
- [ ] Vault decrypt cache hit 时**不调用**（性能 invariant）
- [ ] `_FORWARDABLE` 在 `egress/launcher.py` 不再含 LLM provider key names
- [ ] Bedrock region 从 DB extras 拿，不从 host AWS_REGION
- [ ] MCP service-mode credential 注入工作（PR 4）
- [ ] `lint-no-host-env-llm-keys.py` 跑通 + clean
- [ ] 全部现有 pytest 不退化（特别 INV-VAULT-*, INV-6 等）
- [ ] 4 commits 用 `git commit -s` 累在 `chore/config-db-truth`
- [ ] `git push origin chore/config-db-truth`
- [ ] 更新 `docs/config-drift-fix-plan.md` 标 D1 + D4 fixed + 日期 + Option A 设计 note

## Open questions（session 内自决）

1. **`find_by_ip` 实现**：aiodocker 没原生 helper，需要 list all containers + match `NetworkSettings.Networks.<bridge>.IPAddress`。30 秒 cache 整 list 还是 per-ip lookup？推荐前者（list 一次便宜，per-request docker inspect 慢）
2. **Cache TTL 选择**：IdentityResolver 30s（IP 短期不复用）；CredentialResolver 60s（credential PUT 后用户等 1 分钟可接受）。session 内可微调
3. **provider 从 request 推断**：path-based (`/proxy/anthropic/...`) vs Host header 解析？grep 现有 reverse_proxy.py 的 routing 看
4. **Bedrock extras schema**：v2-B credential-dialog 存了 region 进 extras——验证 schema 字段名（`region` vs `aws_region`）+ 必填还是可选
5. **lint 集成位置**：pytest run vs 独立 npm/script？参考 v1.1/v2 lint script pattern（`web/scripts/lint-no-admin-chat.mjs`）
6. **`get_credential_data(tenant_id, provider) -> bytes | None`** 返 None 还是抛？现有 `db/model.py` 已实现，grep 看签名

## Pitfalls

- **不要假设 docker inspect 返完整 labels**——`Labels` 字段可能是 `None` 如果 container 启动时没 labels；本 chore PR 1 是 prerequisite，但要兼容旧 container（无 label）→ raise `UnknownSourceError` 而不是 KeyError
- **source IP 唯一性**：rolemesh-agents 内部 bridge 应该 per-container unique IP（验证：`docker network inspect rolemesh-agents`）；如果用 host network 或 NAT，整个 Option A 不成立——session 第一件事 grep 验证
- **cache invalidation 不订阅 NATS event** 是 dev 决定；prod 真上要补
- **`_FORWARDABLE` 删 LLM keys** 后必须确认 evaluation CLI / 其它 host 工具不依赖 gateway 容器有这些 env（已确认 CLI 不走 proxy）
- **conversation_id header 完全废弃** —— 即使 Option C 推过这个 header，Option A 下不需要；如果 grep 发现 agent SDK / Pi 已经在塞这个 header（02c retired 没真做但万一以后做了），删掉
- **`MissingCredentialError` 返 401 不返 500** —— user-actionable error
- **不要 silent fallback** —— 这是当前 bug 根源；strict 是 invariant
- **vault.decrypt_json 不便宜** —— cache 必要；mutation test 验 cache hit 不打 vault

## 执行前刷新清单

- [ ] Session 1 完成？（D2 simplify + D3 fix 已在 chore/config-db-truth 前 2 个 commit）
- [ ] `docker network inspect rolemesh-agents` 验证 per-container unique IP（Option A 前提）
- [ ] grep `Labels` 在 docker_runtime.py 看 Docker API label 透传现状
- [ ] grep `X-RoleMesh-Conversation-Id` 全仓——如非 0 hits，确认其它用途 + 决定是否删
- [ ] `tests/egress/` 目录是否存在 + 现有 fixture 风格

## Findings (after execution)

_(empty — 重点记录：docker network 是否真支持 per-container IP / find_by_ip 实现路径 / Bedrock extras 字段名验证结果 / cache hit ratio 实测 / 对未来 OIDC user-mode chore 复用的 ContainerIdentity 接口形状 / LOC 实际 vs 500 估算 / 删除的 _FORWARDABLE 行清单)_
