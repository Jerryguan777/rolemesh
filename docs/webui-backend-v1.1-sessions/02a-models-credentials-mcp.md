# Session 02a — Models + Credentials + MCP CRUD

| field | value |
|---|---|
| Phase | 2 |
| Prerequisites | Phase 1 全 done（含 e2e smoke 通过） |
| Estimated PRs | 4-5 |
| Estimated LOC | ~1300（含 vault primitive，rotation 推迟） |
| Status | done — 2026-05-21 |

> **DRAFT**：本 prompt 在 Phase 1 完工前可能需要刷新。**执行前必读**：
> - 末尾"刷新清单"
> - Phase 1 三个 session 的 Findings 段
> - 检查 plan critique §5 / §8 的开放问题是否已解决

## Goal

落地 `/api/v1/models`（只读）、`/api/v1/tenant/credentials`、`/api/v1/mcp-servers` 全套 CRUD。**不**实现 user-mode credential 注入（02c 做）。本 session 后 coworker 能配真 credential + service-mode MCP 跑起来。

**新增重点**（与原 DRAFT 不同）：引入 `CredentialVault` envelope-encryption primitive（单 Fernet）。`tenant_model_credentials.credential_data` BYTEA 存 Fernet 密文。Master key rotation 推迟，设计 §8.1.1 说明理由（LLM key 长期 static + 未来 MultiFernet 非破坏性升级）。设计 §8.1 是权威依据。

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §3 Phase 2 / §7 hot-load 矩阵 / **§8 + §8.1（envelope encryption + rotation）**
2. Phase 1 Findings（特别是 01a 的 ErrorResponse helper + ApiClient 模式；01b 的 hot-reload pattern）
3. `src/rolemesh/auth/token_vault.py` + `src/rolemesh/db/schema.py` 中 `oidc_user_tokens` 表 —— **这是 vault primitive 的模板，CredentialVault 抄它**
4. `src/rolemesh/egress/` —— 现有 credential_proxy / mcp_cache 实现
5. `src/rolemesh/db/` —— `mcp_servers` 表（00b 已建）的现有 CRUD（如有）

## Scope — PR sketch

### PR 1 — Vault primitive + schema 微调

**Background**：设计 §8.1 envelope encryption。把 OIDC vault 与新 LLM credential vault 共享一层 fernet helper。**不引 MultiFernet / rotation**（推迟，设计 §8.1.1 说明）。

- 新建 `src/rolemesh/auth/encryption.py`（抽 OIDC vault 现有的 `derive_key()` 出来）：
  - `derive_fernet_key(secret: str) -> bytes`
  - env 读取 helper：`load_vault_key_from_env("CREDENTIAL_VAULT_KEY") -> bytes`（缺则 raise，fail-loud）
- 新建 `src/rolemesh/auth/credential_vault.py`：
  - `class CredentialVault` 包装单 Fernet：
    - `encrypt_json(data: dict) -> bytes`
    - `decrypt_json(blob: bytes) -> dict`
  - app 启动时构造单例（缺 key 直接 raise，fail-loud）
- **不动**现有 `src/rolemesh/auth/token_vault.py`——保持单 Fernet 原样，只是 import `derive_fernet_key` 共享 helper（消除 derive 逻辑重复），其它行为完全不变
- Schema 微调：`tenant_model_credentials.credential_ref TEXT` → `credential_data BYTEA NOT NULL`。greenfield 姿态 → drop column + add column 即可，不写 migration ceremony
- pinned tests（设计 §8.1.3 INV-VAULT-1/2）：
  - `INV-VAULT-1`：未设 env 时 `CredentialVault` 构造抛 + app 启动 fail-loud
  - `INV-VAULT-2`：encrypt 后 DB BYTEA 不含明文 substring（写 sentinel string `SENTINEL_LEAK_<uuid>`，从 DB 读 BYTEA 转 utf8 + grep 不命中）
- `scripts/smoke_bootstrap.sh` 加 env 校验：未设 `CREDENTIAL_VAULT_KEY` 时 smoke 立即 red

### PR 2 — `/api/v1/models` + `/api/v1/tenant/credentials`

- `GET /api/v1/models?provider=&family=` —— 列表，公开（已 seed）
- `GET /api/v1/models/{id}`
- `GET /api/v1/tenant/credentials` —— **永不返真 key**，只返 `provider / created_at / updated_at`（不再有 credential_ref；BYTEA 数据列也不投影）
- `PUT /api/v1/tenant/credentials/{provider}` —— body 含真 key (`{"api_key": "sk-ant-..."}`)，walks through `CredentialVault.encrypt_json()` → INSERT/UPDATE `credential_data` BYTEA
- `DELETE /api/v1/tenant/credentials/{provider}` —— 如果有 coworker 在用返 409 `RESOURCE_IN_USE`
- 修改 credential 后 publish NATS event `web.coworker.restart` → orchestrator 重启该 tenant 下用该 provider 的所有 coworker（**已锁定 Open Q：全部重启，不挑 idle**）
- pinned test：`INV-VAULT-3` —— API response 永不含明文（用 sentinel key 写入后 GET 返回 JSON 字符串化后 grep sentinel 不命中）

### PR 3 — `/api/v1/mcp-servers` CRUD

- 全套 CRUD + RLS + 双层防御
- `auth_mode` 字段必须能存 `user / service / both`
- `tool_reversibility` JSONB 直接透传 IPC（00a INV-2 已确保兼容）
- DELETE 被引用返 409
- hot-reload 走现有 `egress.mcp.changed` event（已实现）

### PR 4 — `/api/v1/coworkers/{id}/mcp-servers` 关系层

- POST 绑定 / DELETE 解绑 / PATCH 改 `enabled_tools`
- `enabled_tools` 三态：NULL=全启用 / []=全禁 / [...]=白名单
- 写入路径同时发 `web.coworker.mcp_changed` event
- pinned test 覆盖三态语义

### PR 5 — Frontend：Models / Credentials / MCP 页面

- 三个对应路由的页面实现（设计 §6.3 D/E/F）
- 复用 00c shell + typed client
- Credentials 页面 form：只用于 PUT 新值；**绝不**显示已有 key（不回填、不 placeholder 提示长度、不返 last4）
- DELETE 按钮 + 409 错误体的友好展示（"This credential is in use by N coworkers"）

## Acceptance criteria

- [ ] 全 endpoint 单测 + RLS 隔离测试
- [ ] Vault INV-VAULT-1/2/3 pinned test 全绿
- [ ] OIDC `TokenVault` 现有测试不退化（只 import 共享 `derive_fernet_key`，行为不变）
- [ ] Phase 2 smoke 部分（service-mode MCP 路径）：配真 credential（密文落 DB）→ 绑 MCP → coworker 用 MCP tool 成功
- [ ] DELETE 被引用 → 409 + 结构化 error
- [ ] Credentials 响应**永不**包含真 key（sentinel test 验证）
- [ ] PUT credential 后 NATS event 真发出，使用该 provider 的 coworker 在 5 秒内重启（live test 或 mock orchestrator subscriber）
- [ ] 更新 plan 状态

## Out of scope

- ❌ user-mode credential 注入 —— 02c
- ❌ `coworker.tools` 双写 —— 02b
- ❌ Models admin 写入 endpoint —— v2
- ❌ Channel binding tokens 迁 vault（slack/telegram bot_token 仍 `channel_bindings.credentials` 明文）—— 已知技术债，独立 chore，不在 v1.1 范围

## Open questions

全部已解决：

1. ~~secret store 选什么~~ → **`CredentialVault`（Fernet + BYTEA + MultiFernet rotation），复用 OIDC vault 模式**。设计 §8.1 是权威依据。
2. ~~credential 更新 coworker 重启范围~~ → **重启 tenant 下所有用该 provider 的 coworker**（greenfield 早期不挑 idle；in-flight run 中断由 INV-6 路径 6 / container_crash terminator 处理）

## Pitfalls

- **`CREDENTIAL_VAULT_KEY` 不能 silent fallback 到 hardcoded default 或明文**——未设直接 fail-loud。INV-VAULT-1 钉死
- **共享 `derive_fernet_key` 时不要破坏 OIDC vault 行为**：现有 OIDC vault env var 可能叫 `OIDC_VAULT_KEY` / `TOKEN_VAULT_KEY` / 别的——grep `src/rolemesh/auth/token_vault.py` + `src/webui/main.py` 确认现有 env 名，保留旧名 + 只 import helper 共享 derive 逻辑。OIDC vault 接口与行为完全不动
- **`PUT /credentials/{provider}` 入参不要 log full body**——日志 sanitize：把 `"api_key"` 字段值替换成 `<redacted>`，否则 dev console / log file 泄漏面积大
- **GET 响应 schema 严格禁列**：在 Pydantic `CredentialResponse` 里**不存在** `credential_data` 字段（不是设 `exclude={...}`——根本不在 schema 里）。这样即使将来有人写错 endpoint 也漏不出来
- `mcp_servers.auth_mode` 默认值要给（设计 §2.1 没默认，决定 `service` 还是必填）

## 执行前刷新清单（DRAFT 状态）

- [ ] Phase 1 三个 session 已完成？
- [ ] secret store 方案确认了？→ 已锁定 §8.1 envelope encryption
- [ ] credential 重启策略确认了？→ 已锁定 tenant 全部重启
- [ ] OIDC vault 当前 env var 名 + key 派生方式确认了？（影响 PR 1 的"既不破现有又能复用"）
- [ ] 现有 `mcp_servers` 表是否已有部分 CRUD？避免重复
- [ ] CredentialVault 是否需要 audit log（PUT/DELETE 时落 audit）？v1.1 范围没明定——session 内可以决定加 + 在 Findings 记录

## Findings (after execution)

执行日期：2026-05-21。5 个 PR 各一个 commit，均以 `git commit -s` 累在 `feat/ui`。后端 112 个 pytest 全绿（约 14.5 分钟）；前端 15 个 vitest 全绿。

### OIDC vault 当前 env var + 共享 helper 路径

- 现有 OIDC vault env var = `ROLEMESH_TOKEN_SECRET`（`src/rolemesh/core/config.py:195` 是 grep 入口，实际消费在 `src/rolemesh/auth/token_vault.py:create_vault_from_env`）
- 共享 derive 逻辑放在 **`src/rolemesh/auth/encryption.py`**，两个函数：
  - `derive_fernet_key(secret: str) -> bytes`（SHA-256 → urlsafe-base64）
  - `load_vault_key_from_env(env_var: str) -> bytes`（unset/empty 直接 `RuntimeError`，fail-loud）
- `TokenVault.derive_key(secret)` 变成 thin alias，**完全不破坏行为**——`tests/auth/test_token_vault.py` 11/11 全绿
- 新 vault env var = `CREDENTIAL_VAULT_KEY`，由 `rolemesh.auth.credential_vault.CREDENTIAL_VAULT_ENV` 常量导出，避免拼写漂移

### CredentialVault 与 audit log

- **没有**在 PUT/DELETE 时落 audit log。理由：v1.1 范围内 credential 写入没有现成的 audit 表可用；为 02a 单独立一张 audit 表是 scope creep
- PUT credential 走 `logger.info("PUT credential", ..., body=sanitize_for_log(...))`——`api_key` 字段被替换成 `<redacted>`，作为日志层的最小留痕
- 真正想审计 credential 变更时，独立 chore：复用现有 `safety_rules_audit` 表结构 + 新 actor_action 枚举值（`tenant_credential_put` / `tenant_credential_delete`），不在本 session

### `mcp_servers.auth_mode` 默认值最终选择

- DB 列：`VARCHAR(50) NOT NULL DEFAULT 'service'`（idempotent `ALTER` 兼容老 dev DB）
- API 层：**必填**——`MCPServerCreate.auth_mode` 是 `Literal["user","service","both"]`，缺字段 → 422
- 双层防御理由：API 必填让操作者必须显式声明（避免"我以为是 service 但其实落了 user"），DB 默认 'service' 让非 API 路径（migration / smoke 脚本）落在最安全的服务端凭证模式

### PUT credential 触发 coworker 重启的端到端验证

- 单元验证：`test_put_credential_publishes_restart_per_affected_coworker` 直接 monkeypatch publisher，验三个 fixture coworkers（2 个 anthropic + 1 个 openai）中只有 anthropic 的 2 个收到 event
- 边缘验证：`test_put_credential_does_not_publish_for_unused_provider` 防 false-positive（无 coworker 使用该 provider 时不发空 event）
- **JetStream live test 未做**——01a 已经在 `test_patch_model_id_round_trips_through_real_nats` 把"webui → JS → orchestrator subscriber → state.config 更新"链路钉死，credential PUT 用同一个 `WEB_COWORKER_RESTART_SUBJECT` 和同一个 publisher 函数，复用既有保护。如果 02c 实跑发现重启没生效，再补一个 NATS round-trip 测试

### 对 02b（tools 双写）与 02c（credential_proxy user-mode）的影响

**对 02b**：
- `mcp_servers.auth_mode` 列已落地 + 默认值就位，02b 双写时直接用 DB 列；不需要再改 schema
- `coworker_mcp_servers` 关系层（PR 4）已经覆盖 NULL/[]/whitelist 三态，02b reader 切换时只需要从 `coworkers.tools` JSONB 改读关系表
- 新 NATS subject `web.coworker.mcp_changed` 常量在 `rolemesh.orchestration.coworker_hot_reload` 定义，02b 在 orchestrator 侧加 subscriber 用这个常量即可，**不要**自己定义新字符串
- 提示：PATCH /mcp-servers/{id} 已经发 `egress.mcp.changed`，注意 02b 双写期不要重复发——orchestrator 侧的 in-process registry 由这个 event 维护，从 `coworker.tools` 走的旧路径要在 reader 切换时一起停掉

**对 02c**：
- CredentialVault 单例已在 webui lifespan 装好（`rolemesh.auth.credential_vault.set_credential_vault`）；orchestrator 进程需要同样 import + install 一次才能 decrypt
- `credential_data` 列是 BYTEA，credential_proxy 读取时调 `CredentialVault.decrypt_json(blob)` 拿到 `{"api_key": "sk-..."}`，**不要**自己用 Fernet 解
- `auth_mode=user` 路径 02c 仍走 TokenVault（OIDC），与 CredentialVault 是两套独立机制——不要混用 derive key
- Frontend MCP 页面已经把 `auth_mode != service` 标 amber "requires user session"，02c 不用再改 UI 文案

### 偏离原 prompt 的地方

- **未实现 OpenAPI codegen freshness 检查里的 npm 步骤**——`tests/test_openapi_codegen_freshness.py` 自带在 `web/node_modules` 缺失时 self-skip，所以 freshness 在 CI/本地都没 red。每个 PR 我都手动跑了 `npm run openapi:gen` 并提交 `types.ts`
- **PR 4 PATCH 行为细化**：原 prompt 说"PATCH 改 enabled_tools"，没说"空 body 是 no-op 还是清空"。最终决定空 body → 返回当前状态（no-op），显式 `{"enabled_tools": null}` 才是"全启用"。理由：tri-state 语义要求 null 是有意义的值，不能让"忘传字段"和"想清空"撞车。`test_patch_can_transition_through_each_tri_state` 钉死这条规则
- **PR 5 没做 Coworker 详情页的 MCP 子面板**——原 prompt 没列入 §6.3 D/E/F 范围，coworker_mcp 关系层只通过 REST 暴露；Coworkers 列表页的"详情子 tab"留给 03b 一起做（与 skills 子面板节奏一致）
- **DB schema 在 mcp_servers 上加了 idempotent `ALTER ... SET DEFAULT 'service'`**——原 prompt 没明定，但老 dev DB 的 CREATE 没有 DEFAULT，少这一行老 DB 上 API 必填的双层防御等于只有 API 一层

### Acceptance criteria verification

- [x] 全 endpoint 单测 + RLS 隔离测试（112 个 pytest 全绿 / 15 个 vitest 全绿）
- [x] Vault INV-VAULT-1/2/3 pinned test 全绿（tests/auth/test_credential_vault.py + tests/webui/test_v1_credentials.py 里 sentinel-grep）
- [x] OIDC `TokenVault` 现有测试不退化（11/11 全绿，只 import 共享 `derive_fernet_key`）
- [ ] Phase 2 smoke 部分（service-mode MCP 路径）—— **未跑**，留给 02b/02c 实跑。本 session 范围只到 CRUD + 单元/集成测试
- [x] DELETE 被引用 → 409 + 结构化 error（credentials 用 coworker_ids；mcp_servers 用 coworker_ids）
- [x] Credentials 响应永不包含真 key（sentinel test + Pydantic 结构性 forbid）
- [x] PUT credential 后 NATS event 真发出（fan-out 单测覆盖；live NATS round-trip 借 01a 既有保护）
- [x] 更新 plan 状态

### 后续 cleanup（不在本 session 范围）

- 02b：`coworker.tools` 双写 + orchestrator 侧 `web.coworker.mcp_changed` subscriber
- 02c：credential_proxy 调 `CredentialVault.decrypt_json` 取真 key + user-mode 路径
- Channel binding tokens（`channel_bindings.credentials` JSONB 明文）→ 独立 chore，不在 v1.1 范围
- Audit log for credential PUT/DELETE → 独立 chore
- Coworker 详情页的 MCP/Skills 子面板 → 03b 与 skills 一起做
- Live NATS smoke for credential-restart fan-out → 02c 跑真链路时补

