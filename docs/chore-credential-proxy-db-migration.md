# Chore — credential_proxy DB migration (D1 + D4)

| field | value |
|---|---|
| Cycle | independent chore (与 v1.1 / v2 平级) |
| Branch | 新分支 `chore/credential-proxy-db`（off main） |
| Prerequisites | v2 cycle 合 main（如未合，可在 feat/ui-v2 上做但 PR 边界乱）|
| Estimated PRs | 3-4 |
| Estimated LOC | ~1500-1800（2x 系数；含 cache + tests） |
| Status | not started |

> **Trigger**：`docs/config-drift-fix-plan.md` §3 D1（CRITICAL）+ D4。当前 multi-tenant LLM credential 隔离**完全不工作**——UI 上 user 配的 anthropic key 加密入 DB 后从未被 runtime 读取；agent 实际用 host process 的 `ANTHROPIC_API_KEY` env var，所有 tenant 共用一份。dev 单 tenant 凑巧能跑，prod 立刻爆。

## Goal

把 `src/rolemesh/egress/` 子系统从 **v0 single-tenant host-env-based** 改造成 **multi-tenant DB-based** credential 注入：

1. **D1**：`credential_proxy` per-request 查 `tenant_model_credentials.credential_data` BYTEA → `CredentialVault.decrypt_json()` → 注入真 key
2. **D4**：service-mode MCP credentials 走同一路径——按 mcp server name 查 `mcp_servers.credential_ref` → 注入
3. 加 in-memory cache（1 分钟 TTL，per (tenant_id, provider/mcp_name) key），避免每请求 vault decrypt
4. 停止 `egress/launcher.py` 把 host env LLM keys forward 到 gateway 容器
5. INV-CRED-1/2/3/4 pinned tests 防止退回

**不在范围**：user-mode MCP（OIDC vault）—— 02c retired 的工作；本 chore 只 service-mode。当 OIDC 真接入时复用本 chore 的 cache + conversation_id header 设计模式。

## Required reading

1. [`docs/config-drift-fix-plan.md`](./config-drift-fix-plan.md) §3-§6（drift evidence + 根因 + 修复设计）
2. [`docs/webui-backend-v1.1-design.md`](./webui-backend-v1.1-design.md) §8.1 envelope encryption
3. **v1.1 02a Findings** § "CredentialVault" —— vault primitive 实际接口
4. **v2-C Findings § "Backend WS forward — partial gap discovered"** —— forward-design 反应模式（chore-D1+D4 不复用，但参考"假设接口"的诚实姿态）
5. **02c retired session** (`docs/webui-backend-v1.1-sessions/02c-credential-proxy-user-mode.md`) —— 历史推迟的 user-mode 工作；其中"conversation_id header 信任验证"安全要点本 chore **必须照搬**到 service-mode
6. 现状代码：
   - `src/rolemesh/egress/reverse_proxy.py` —— 当前直接 `os.environ.get(...)` 的 LLM key 注入点（grep `ANTHROPIC_API_KEY`）
   - `src/rolemesh/egress/launcher.py` —— `_FORWARDABLE` host env forward 名单
   - `src/rolemesh/egress/gateway.py` —— gateway init
   - `src/rolemesh/egress/credential_proxy.py` / `forward_proxy.py` / `mcp_cache.py` —— proxy 实现
   - `src/rolemesh/auth/credential_vault.py` —— `decrypt_json()` 方法
   - `src/rolemesh/db/model.py` —— `get_credential_data(tenant_id, provider)` 已存在
   - `src/rolemesh/db/mcp_server.py` —— `credential_ref` 字段查询
   - `src/rolemesh/db/chat.py` —— `get_conversation_for_user` 用于反查 tenant_id
   - chore A `src/rolemesh/orchestration/run_cancel_subscriber.py` —— `get_active_container_name` helper（信任验证用）

## 概念定位：service-mode 复用 02c 的设计原语

02c retired 推了**整条 user-mode 链路**（OIDC vault）；本 chore 只做 **service-mode**：

| 维度 | service mode（本 chore）| user mode（02c retired，未来 OIDC chore）|
|---|---|---|
| credential 来源 | `tenant_model_credentials`（platform-managed）/ `mcp_servers.credential_ref` | `oidc_user_tokens`（user-managed via OIDC）|
| 反查路径 | conversation → coworker → tenant → DB | conversation → user → vault → decrypt |
| 触发条件 | 任何 LLM 调用 / `auth_mode=service` MCP | 仅 `auth_mode=user` MCP |
| 阻塞性 | **CRITICAL 当前真 bug** | OIDC 分支合入前无 caller |

两条路径共用：
- `X-RoleMesh-Conversation-Id` header（02c retired 设计）
- conversation_id header 信任验证（防容器伪造，比对 active container map）
- in-memory cache + TTL pattern

## Scope — PR breakdown

### PR 1 — `CredentialResolver` 新模块 + cache + reverse-lookup helpers

**Goal**：把"拿 tenant_id + provider → 返真 key"逻辑封装成可测试的服务，与 reverse_proxy / forward_proxy 解耦。

子任务：

1. **新建 `src/rolemesh/egress/credential_resolver.py`**：
   ```python
   class CredentialResolver:
       def __init__(self, vault: CredentialVault, *, cache_ttl_seconds: int = 60):
           self._vault = vault
           self._cache: dict[tuple[str, str], _CacheEntry] = {}
           self._ttl = cache_ttl_seconds
           self._lock = asyncio.Lock()  # per-tenant lock 太细；module-level 够用 dev 阶段

       async def resolve_llm_credential(
           self, tenant_id: str, provider: str,
       ) -> dict[str, str]:
           """Return {api_key, ...extras} for (tenant, provider).
           Raises MissingCredentialError if no row in DB."""
           ...

       async def resolve_mcp_credential(
           self, tenant_id: str, mcp_server_name: str,
       ) -> dict[str, str] | None:
           """Return decrypted credential for service-mode MCP.
           None if auth_mode != service or credential_ref unset."""
           ...

       def invalidate(self, tenant_id: str, key: str) -> None:
           """Called when credential changes via PUT endpoint (NATS event subscribe)."""
           ...
   ```
   - cache key: `(tenant_id, "llm:" + provider)` / `(tenant_id, "mcp:" + name)`
   - cache value: `_CacheEntry(value: dict, expires_at: datetime)`
   - 过期 → 重 decrypt，单 process 内一把 asyncio.Lock 防 thundering herd
2. **新建 `src/rolemesh/egress/tenant_lookup.py`**：
   ```python
   async def resolve_tenant_for_request(
       conversation_id: str,
       *,
       get_active_container_name: ContainerLookup,  # chore A 落地
   ) -> str:
       """Reverse-lookup tenant_id from conversation_id header.
       Verifies the conv is owned by the current active container
       (anti-spoofing)."""
       ...

   class ConversationIdMissingError(Exception): ...
   class ConversationIdSpoofingError(Exception): ...  # header 与 active container 不匹配
   ```
3. **NATS subscribe credential change event** (`webui/v1/credentials.py` 已 publish `web.coworker.restart`；本 chore 加 `web.credential.changed.{tenant_id}.{provider}` event + CredentialResolver subscribe → invalidate cache)
4. **pinned tests** (`tests/egress/test_credential_resolver.py`)：
   - resolve LLM cache hit / miss / expire
   - resolve MCP credential 三态 (service has ref / service no ref / user-mode)
   - missing credential 抛 `MissingCredentialError`
   - invalidate event triggers re-decrypt next call
   - tenant_lookup happy path + missing header + spoofed header

**估算**：~500 LOC（含 tests）

### PR 2 — `reverse_proxy.py` 改造：所有 LLM key 注入走 resolver

**Goal**：把 `secrets.get("ANTHROPIC_API_KEY", "")` 等所有 `os.environ.get(...)` 读 LLM key 的位置换成 `await resolver.resolve_llm_credential(tenant_id, provider)`。

子任务：

1. **grep + 列清单**（session 开头跑）：
   ```bash
   grep -rn "secrets\.get\|os\.environ\.get" src/rolemesh/egress/reverse_proxy.py \
     | grep -iE "API_KEY|OAUTH_TOKEN|AUTH_TOKEN|BEARER" 
   ```
   贴到 session 开头作为工作清单
2. 改造每个注入点（应该 ~6-10 处）：
   - Anthropic native (api-key + oauth 两条)
   - OpenAI
   - Google
   - Bedrock（AWS bearer + region）
3. 改造每个 handler 入口：
   - 从 request header 拿 `X-RoleMesh-Conversation-Id`
   - 调 `tenant_lookup.resolve_tenant_for_request(conv_id, ...)` → tenant_id
   - 推断 provider（看 request path / Host header）
   - 调 `resolver.resolve_llm_credential(tenant_id, provider)` → credential dict
   - 用 dict 注入 header
4. **失败处理**：
   - missing conv_id header → 400 `MISSING_CONVERSATION_ID`
   - spoofed conv_id → 403 `CONVERSATION_NOT_OWNED`
   - missing credential → **不要 fallback host env**（这是当前 bug 根源）→ 401 `MISSING_CREDENTIAL` + 结构化 error
5. **删 `egress/launcher.py` 内 `_FORWARDABLE` 的 LLM-related 行**：
   - `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` / `PI_OPENAI_API_KEY` / `OPENAI_BASE_URL` / `PI_GOOGLE_API_KEY` / `GOOGLE_BASE_URL` / `AWS_BEARER_TOKEN_BEDROCK` / `AWS_REGION`
   - 保留 infra forward: `EGRESS_UPSTREAM_DNS`
6. **pinned tests** (`tests/egress/test_reverse_proxy_db_credentials.py`)：
   - tenant A 配 anthropic key → tenant A 请求 → 注入 A 的 key（mock vault + DB）
   - tenant B 配 anthropic key → tenant B 请求 → 注入 B 的 key
   - 同 tenant 两次连续请求 → vault decrypt 调 1 次（cache hit）
   - tenant 未配 credential → 401 + 结构化 error，**不 fallback host env**
   - missing conv_id header → 400
   - spoofed conv_id → 403
   - **Mutation test**：把 reverse_proxy 改回 fallback host env → tenant isolation test 应该红

**估算**：~700 LOC（含 tests）

### PR 3 — service-mode MCP credential 同款路径

**Goal**：`POST /proxy/mcp/<server-name>/...` 时按 (tenant_id, server name) 查 `mcp_servers.credential_ref` → 注入对应 header。

子任务：

1. **`reverse_proxy.py` MCP routing 加 credential resolve**：
   - 路径前缀 `/proxy/mcp/<server-name>` 触发
   - 调 `resolver.resolve_mcp_credential(tenant_id, server_name)`
   - 返 None（auth_mode != service）→ 不注入，pass-through
   - 返 dict → 按 `mcp_servers.auth_mode` 决定注入哪个 header（看 02a Findings）
2. **`mcp_cache.py` 更新**：移除任何"启动时 load 一份 credentials 缓存"逻辑（应该已经没有，但 grep 验证）
3. **pinned tests**：
   - service-mode MCP request 走 resolver
   - `auth_mode=user` 路径不调 resolver（user-mode 留 OIDC chore）
   - mcp_servers.credential_ref 改了 → next request 拿新值（cache invalidate）

**估算**：~300 LOC

### PR 4 — INV-CRED-* lint + 文档

**Goal**：防止后续 PR 退回 host env pattern。

子任务：

1. **`scripts/lint-no-host-env-llm-keys.py`**（或加进 pytest）：
   - grep `src/rolemesh/egress/` 找 `os.environ.get` 读 LLM-key-shaped 名字（`*_API_KEY` / `*_OAUTH_TOKEN` / `*_AUTH_TOKEN` / `BEARER`）
   - 命中输出 file:line，exit 1
   - 例外白名单走 `# inv-cred-1-ok: <reason>` 注释
2. **`scripts/lint-forwardable-no-llm-keys.py`**：
   - 检查 `egress/launcher.py` `_FORWARDABLE` 不含 LLM provider key names
3. INV-CRED-2/3 已在 PR 2 tests 覆盖（tenant 隔离 + missing credential 不 fallback）
4. 更新 `config-drift-fix-plan.md` §3 D1 + D4 标 fixed + 日期
5. 在 `egress/credential_resolver.py` 顶部 docstring 引 `config-drift-fix-plan.md` 作历史

**估算**：~200 LOC

## Acceptance criteria

- [ ] `<rm-credential-dialog>` PUT 后 next agent request 真用新 key（端到端 manual smoke：两 tenant 配不同 key 看 anthropic 日志 / API usage）
- [ ] `grep -rn "os\.environ\.get.*API_KEY\|os\.environ\.get.*OAUTH_TOKEN" src/rolemesh/egress/` → 应只剩 lint script 自己 + inv-cred-ok 豁免
- [ ] `egress/launcher.py _FORWARDABLE` 不含 LLM provider key
- [ ] INV-CRED-1/2/3/4 pinned test 全绿
- [ ] tenant A / tenant B 配不同 anthropic key → 各自请求注入各自的 key（手动 smoke + 单测）
- [ ] vault decrypt 在 cache hit 时**不调用**（性能 invariant）
- [ ] missing credential **不 silent fallback** host env → 401 `MISSING_CREDENTIAL` + 结构化 error
- [ ] 跨 tenant 攻击（伪造 conversation_id header）被 403 拦
- [ ] credential PUT 后通过 NATS event invalidate cache，next request 拿新值（live test）
- [ ] 现有 evaluation CLI 不退化（如果 CLI 不带 conv_id header，需要单独路径或显式 tenant_id 参数；session 内决定）
- [ ] OpenAPI / contract test / 现有 pytest 全绿
- [ ] `config-drift-fix-plan.md` 标 D1 + D4 fixed
- [ ] 加 `INV-CRED-*` 进文档

## Out of scope

- ❌ **user-mode MCP credential 注入**（OIDC vault 路径，02c retired 工作）
- ❌ **D3 per-tenant max_concurrent_containers**（独立 chore）
- ❌ **`channel_bindings.credentials` 明文迁 vault**（独立技术债 chore；同根因但不同 vault primitive）
- ❌ **重构 evaluation CLI / ops script**（如果它们依赖 host env，session 内决定保留分支还是改 CLI 用 tenant 参数）
- ❌ **删除现有 `ANTHROPIC_API_KEY` etc 在 `.env` 的存在**——dev 阶段开发者本机可能仍 export 这些（与 evaluation CLI 兼容）；只是不再被 credential_proxy 消费
- ❌ **NATS event 的真 backend publisher**——`webui/v1/credentials.py` 已经 publish coworker restart event；本 chore 加一个 `web.credential.changed.*` 是 additive

## Open questions

需 session 内决策：

1. **`X-RoleMesh-Conversation-Id` header 真生成路径**：当前 agent 容器出站 MCP 调用是否带这个 header？grep `src/rolemesh/agent/` + `src/pi/` 验证。如果没带，本 chore 需要在 Pi / Claude SDK backend 各加注入点（与 02c retired prompt 同款工作）
2. **Cache size limit**：1 分钟 TTL 但没行数 limit，dev OK；prod 大 tenant 数会爆内存——加 LRU 还是简单 dict？推荐简单 dict（dev 阶段）
3. **CredentialResolver 是 singleton 还是 per-process**：推荐 module-level singleton（与 `CredentialVault` 同模式）
4. **evaluation CLI 怎么用**：它没 conv_id 上下文。两选项：(a) CLI 显式带 `--tenant-id` 参数 + 走特殊 path bypass header 检查；(b) CLI 直接调 `CredentialVault.decrypt_json` 不经 proxy。推荐 (b)——CLI 是 ops tool 不该走用户 path
5. **Bedrock region**：当前 host env `AWS_REGION` forward。本 chore 后 region 从 `tenant_model_credentials.credential_data` decrypted JSON 拿（02a 落地 bedrock extras 含 region）。验证 v2-B credential dialog 确实存了 region

## Pitfalls

- **绝不允许 silent fallback host env** —— 这是当前 bug 的本质。strict: 找不到 credential 就 401，不允许"凑巧能跑"
- **conversation_id header 信任验证必须做** —— 容器内 agent 可伪造任意 header；proxy 必须验 conv_id 真对应当前 active container（chore A `get_active_container_name`）。02c retired 已诊断，本 chore 落地
- **cache invalidation 通过 NATS event**——不要靠 `webui/v1/credentials.py` 直接调 resolver.invalidate（跨进程不行）；通过 NATS publish/subscribe
- **现有 host env LLM keys 在 dev .env 不要删**——evaluation CLI / 旧 ops script 可能仍 export 它们（用 `(b)` 方案后 CLI 直接 decrypt 不走 proxy；它仍可能 export host env 给别的工具用）
- **`MissingCredentialError` 返 401 不返 500**——这是 user-actionable error（去配 credential），不是系统 bug
- **Vault decrypt 调用**：`CredentialVault.decrypt_json(blob)` 不便宜（Fernet HMAC verify）；cache 必要
- **Pi backend env-injection 已有的 placeholder**：`src/rolemesh/container/runner.py` 当前给 Pi 容器注入 `ANTHROPIC_API_KEY="placeholder"` 等（真 key 由 proxy 注入到 HTTP header）。这个 placeholder 路径**保留不动**——容器内 SDK 看到 env 有值就不抛 NoCredentialsError；HTTP 出站才被 proxy 拦截换真值
- **conversation_id 反查走 admin_conn** —— credential_proxy 是系统级 caller，应该 admin pool（不受 RLS）；但仍带显式 `WHERE` 谓词（INV-1 双层防御）
- **不要扩 `_FORWARDABLE` 加新 LLM key forward**——任何新 LLM provider 接入都走 DB credential 不走 host env

## 执行前刷新清单

- [ ] v2 cycle 合 main？决定本 chore 起新分支 off main 还是 off feat/ui-v2
- [ ] `docs/config-drift-fix-plan.md` 是否要先 review（特别 §5.B 设计细节）
- [ ] grep `X-RoleMesh-Conversation-Id` 在 src/ 看 header 注入路径是否已存在
- [ ] 现有 evaluation CLI 是否真使用 LLM credentials 走 credential_proxy？（决定 PR 4 是否需要给 CLI 加 bypass 或显式 vault decrypt 路径）

## Findings (after execution)

_(empty — 重点记录：reverse_proxy.py 实际改的注入点数量 + 每个 provider 的字段映射 + cache hit ratio 实测 + evaluation CLI 兼容方案 + 对未来 OIDC user-mode chore 复用的接口形状 + 与 D2 fix（已 6eafd33）的接力情况)_
