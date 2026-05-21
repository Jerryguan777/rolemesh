# Session 02a — Models + Credentials + MCP CRUD  `[DRAFT]`

| field | value |
|---|---|
| Phase | 2 |
| Prerequisites | Phase 1 全 done（含 e2e smoke 通过） |
| Estimated PRs | 4-5 |
| Estimated LOC | ~1500（含 vault primitive + rotation 支持） |
| Status | not started — DRAFT |

> **DRAFT**：本 prompt 在 Phase 1 完工前可能需要刷新。**执行前必读**：
> - 末尾"刷新清单"
> - Phase 1 三个 session 的 Findings 段
> - 检查 plan critique §5 / §8 的开放问题是否已解决

## Goal

落地 `/api/v1/models`（只读）、`/api/v1/tenant/credentials`、`/api/v1/mcp-servers` 全套 CRUD。**不**实现 user-mode credential 注入（02c 做）。本 session 后 coworker 能配真 credential + service-mode MCP 跑起来。

**新增重点**（与原 DRAFT 不同）：引入 `CredentialVault` envelope-encryption primitive + MultiFernet rotation 能力。`tenant_model_credentials.credential_data` BYTEA 存 Fernet 密文；现有 OIDC `TokenVault` 也升级到 MultiFernet 共享 rotation 机制。设计 §8.1 是权威依据。

## Required reading

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §3 Phase 2 / §7 hot-load 矩阵 / **§8 + §8.1（envelope encryption + rotation）**
2. Phase 1 Findings（特别是 01a 的 ErrorResponse helper + ApiClient 模式；01b 的 hot-reload pattern）
3. `src/rolemesh/auth/token_vault.py` + `src/rolemesh/db/schema.py` 中 `oidc_user_tokens` 表 —— **这是 vault primitive 的模板，CredentialVault 抄它**
4. `src/rolemesh/egress/` —— 现有 credential_proxy / mcp_cache 实现
5. `src/rolemesh/db/` —— `mcp_servers` 表（00b 已建）的现有 CRUD（如有）

## Scope — PR sketch

### PR 1 — Vault primitive + schema 微调

**Background**：设计 §8.1 envelope encryption。把 OIDC vault 与新 LLM credential vault 共享一层 fernet helper + MultiFernet rotation。

- 新建 `src/rolemesh/auth/encryption.py`（抽 OIDC vault 现有的 `derive_key()` 出来）：
  - `derive_fernet_key(secret: str) -> bytes`
  - `build_multi_fernet(primary: str, prev: str | None = None) -> MultiFernet`
  - env 读取 helper：`load_vault_keys_from_env("CREDENTIAL_VAULT_KEY", "CREDENTIAL_VAULT_KEY_PREV") -> tuple[bytes, bytes | None]`
- 新建 `src/rolemesh/auth/credential_vault.py`：
  - `class CredentialVault` 包装 MultiFernet：
    - `encrypt_json(data: dict) -> bytes`
    - `decrypt_json(blob: bytes) -> dict`
    - `rotate(blob: bytes) -> bytes`（调 `MultiFernet.rotate`）
  - app 启动时构造单例（缺 key 直接 raise，fail-loud）
- 升级现有 `src/rolemesh/auth/token_vault.py` 到 MultiFernet（OIDC 也吃同套 rotation 能力；接口不变，内部 `Fernet` → `MultiFernet`）
- Schema 微调：`tenant_model_credentials.credential_ref TEXT` → `credential_data BYTEA NOT NULL`。greenfield 姿态 → drop column + add column 即可，不写 migration ceremony
- pinned tests（设计 §8.1.3 INV-VAULT-1/2/3）：
  - `INV-VAULT-1`：未设 env 时 `CredentialVault` 构造抛 + app 启动 fail-loud
  - `INV-VAULT-2`：encrypt 后 DB BYTEA 不含明文 substring（写 sentinel string `SENTINEL_LEAK_<uuid>`，从 DB 读 BYTEA 转 utf8 + grep 不命中）
  - `INV-VAULT-3`：MultiFernet rotation —— 两条 row 分别用 prev / primary 加密，跑 `rotate()` 后两条都用 primary，old key 移除后仍能解
- `scripts/smoke_bootstrap.sh` 加 env 校验：未设 `CREDENTIAL_VAULT_KEY` 时 smoke 立即 red

### PR 2 — `/api/v1/models` + `/api/v1/tenant/credentials`

- `GET /api/v1/models?provider=&family=` —— 列表，公开（已 seed）
- `GET /api/v1/models/{id}`
- `GET /api/v1/tenant/credentials` —— **永不返真 key**，只返 `provider / created_at / updated_at`（不再有 credential_ref；BYTEA 数据列也不投影）
- `PUT /api/v1/tenant/credentials/{provider}` —— body 含真 key (`{"api_key": "sk-ant-..."}`)，walks through `CredentialVault.encrypt_json()` → INSERT/UPDATE `credential_data` BYTEA
- `DELETE /api/v1/tenant/credentials/{provider}` —— 如果有 coworker 在用返 409 `RESOURCE_IN_USE`
- 修改 credential 后 publish NATS event `web.coworker.restart` → orchestrator 重启该 tenant 下用该 provider 的所有 coworker（**已锁定 Open Q：全部重启，不挑 idle**）
- pinned test：`INV-VAULT-4` —— API response 永不含明文（用 sentinel key 写入后 GET 返回 JSON 字符串化后 grep sentinel 不命中）

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
- [ ] Vault INV-VAULT-1/2/3/4 pinned test 全绿
- [ ] OIDC `TokenVault` 已升 MultiFernet；现有 oidc 测试不退化
- [ ] Phase 2 smoke 部分（service-mode MCP 路径）：配真 credential（密文落 DB）→ 绑 MCP → coworker 用 MCP tool 成功
- [ ] Rotation 手动 smoke：设置 `CREDENTIAL_VAULT_KEY` + `_PREV`，跑一个 batch rotate script，验所有 row 都用新 key 加密
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
- **OIDC vault 升 MultiFernet 时**：现有 env var 可能叫 `OIDC_VAULT_KEY` / `TOKEN_VAULT_KEY` / 别的——grep `src/rolemesh/auth/token_vault.py` + `src/webui/main.py` 确认现有 env 名，**不要破坏现有行为**。建议保留旧名 + 用 `derive_fernet_key` helper 兼容；如果想统一，留独立 chore 不在 02a 范围
- **`PUT /credentials/{provider}` 入参不要 log full body**——日志 sanitize：把 `"api_key"` 字段值替换成 `<redacted>`，否则 dev console / log file 泄漏面积大
- **GET 响应 schema 严格禁列**：在 Pydantic `CredentialResponse` 里**不存在** `credential_data` 字段（不是设 `exclude={...}`——根本不在 schema 里）。这样即使将来有人写错 endpoint 也漏不出来
- `mcp_servers.auth_mode` 默认值要给（设计 §2.1 没默认，决定 `service` 还是必填）
- **Rotation script** 不是 endpoint——是一个 `scripts/rotate_vault_keys.py` 命令行工具，运维人员手动跑。endpoint 化反而危险（rotation 是 ops-privileged 动作不应有 web 入口）
- `MultiFernet([new, old]).decrypt` 顺序很重要：先试 new 再试 old；如果反过来，rotation 完成的行也走 old key fallback 路径，性能差

## 执行前刷新清单（DRAFT 状态）

- [ ] Phase 1 三个 session 已完成？
- [ ] secret store 方案确认了？→ 已锁定 §8.1 envelope encryption
- [ ] credential 重启策略确认了？→ 已锁定 tenant 全部重启
- [ ] OIDC vault 当前 env var 名 + key 派生方式确认了？（影响 PR 1 的"既不破现有又能复用"）
- [ ] 现有 `mcp_servers` 表是否已有部分 CRUD？避免重复
- [ ] CredentialVault 是否需要 audit log（PUT/DELETE 时落 audit）？v1.1 范围没明定——session 内可以决定加 + 在 Findings 记录
- [ ] Rotation script 形态：CLI 命令 vs management endpoint vs 后台 task？推荐 CLI（脱离 web），但 session 可结合现有 admin tooling 决定

## Findings (after execution)

_(empty)_
