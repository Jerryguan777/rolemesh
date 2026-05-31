# Session 02c — Retired, deferred to OIDC branch  `[RETIRED]`

| field | value |
|---|---|
| Phase | 2 |
| Prerequisites | — |
| Estimated PRs | 0 |
| Estimated LOC | 0 |
| Status | retired — 2026-05-21 |

## 为什么 retire

应用与 rotation / exchange_for stub 同样的反 over-engineering 标准（zero-caller, build-on-demand）：

| 维度 | 02c 整体 |
|---|---|
| 当前 caller 数 | 0（项目内无 `auth_mode=user` MCP server 在用） |
| 当前用户感知价值 | 0（chat 全程 service mode；用户感知不到 user-mode 路径有没有） |
| 当前攻击向量 | 0（无 user-mode = 无 token 注入路径 = header 伪造无意义） |
| Reauth 触发场景 | 0（无 user-mode = 无 401） |
| 估算成本 | ~1050 LOC + 1 个 session |
| 推迟代价 | OIDC 分支合入那天调试 5 跳 wiring 没 e2e 兜底；接受这个 trade-off |

**核心判断**：02c 是 latent infrastructure——为想象中的未来 OIDC 用户建管道、为不存在的攻击向量建防御。与已经 cut 的 MultiFernet rotation、exchange_for stub 是同一种 over-engineering 模式的更大版本。Plan critique §1 原本的"wiring 兜底"论证假设 OIDC 分支临近；在 OIDC 时机不定的现实下，1050 LOC 的预投入不 earn its keep。

## 这部分工作以后怎么做

当 Keycloak / OIDC 分支真正进入排期时，单独开一个 session 把以下内容一次性做完：

1. IPC `X-RoleMesh-Conversation-Id` header（Pi + Claude SDK 两个 backend）
2. credential_proxy `auth_mode=user` 反查路径（conversation → user_id → TokenVault → Bearer）
3. Header 信任验证（防容器伪造越权）
4. `auth_mode=both` fallback (user → service)
5. Reauth 路径 wire 到 `terminate_run_via_reauth_required`（01b 已落 terminator wrapper）
6. （可选）fake-vault dev mode，如果 OIDC 真接入前还有 dev e2e 需求

届时设计接口能按 OIDC 真接口需求定型（比 v1.1 阶段凭空 stub 更准）。

## 历史档案

之前版本的 02c prompt 包含了完整的：

- 概念澄清（TokenVault vs CredentialVault 不要混用）
- conversation_id header 信任攻击向量分析
- run_id 查找的 race 条件
- fake-vault 与生产代码的隔离机制

如果未来开 OIDC session，**强烈建议**先 `git log --follow` 拿回这些设计要点：

```
git log --follow -- docs/webui-backend-v1.1-sessions/02c-credential-proxy-user-mode.md
```

特别是 conversation_id header 信任那部分——它是 02c refresh 时新发现的安全要点，不在 v1.1 范围内做实现，但**设计契约必须传到后续 session**。

## 设计文档相关 sections

不删除，作为未来实施参考：

- §5.2.2 用词需要调整（从"e2e 推迟"改成"整条链路实现推迟"）
- §5.3 User-mode MCP 链路 —— 架构图保留
- §5.4 失败模式 —— 协议设计保留

## 02a 留下的 follow-up 决策

02a 给 `mcp_servers.auth_mode` 三态 (user/service/both) 落了 API + DB。02c retire 后这变成"DB / API 可配 user，但 credential_proxy 不会处理"的状态。两个选项需要尽快决定：

- **A. v1.1 内禁用 user / both**：API 层把 auth_mode 限制为只有 `service`（暂时性），User 模式提示 "Coming with OIDC integration"
- **B. 保持 API 允许但 runtime 失败**：admin 可以配 user 模式 MCP，但用户调时 silently 失败（差体验）

推荐 A——明确告知"未支持"比 silent failure 友好。这不是 02c 的工作，是 02a 的小补 patch（30 LOC 改 Pydantic Literal + Frontend dropdown 收窄）。

## 关联 commits

- `8e0c591 docs(v1.1): revert rotation scope` —— 反 over-engineering 第一次（MultiFernet）
- `05d5d9f docs(v1.1): refresh 02c prompt with 02a/02b context + cut exchange_for stub` —— 反 over-engineering 第二次（exchange_for stub）
- 本 retirement —— 反 over-engineering 第三次（02c 整体）

## 对 plan.md 的影响

- 02c row → retired (2026-05-21)
- Phase 2 收尾节点：02a + 02b done 即视为 Phase 2 完成
- 直接进入 Phase 3（03a）
