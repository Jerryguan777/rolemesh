# WebUI / Backend v1.1 — 实施计划

> 设计文档来源：[`docs/webui-backend-v1.1-design.md`](./webui-backend-v1.1-design.md)
> Session prompt 目录：[`docs/webui-backend-v1.1-sessions/`](./webui-backend-v1.1-sessions/)
> 本文档跟踪执行进度与 session 依赖关系。

## 规模估计

| Phase | session 数 | PR 数 | LOC |
|---|---|---|---|
| 0（防雷基建 + migration + 脚手架）| 3 | 8-11 | ~2500-3500 |
| 1（chat 主路径）| 3 | 6-8 | ~3000-4000 |
| 2（配置生态 + tools 下线 stage 1+2）| 3 | 8-10 | ~3500-4500 |
| 3（approvals + skills per-tenant）| 2 | 5-7 | ~2000-2500 |
| 3+（drop tools 列 — 独立 session）| 1 | 1 | ~100 |
| 4（safety UI 搬迁）| 1 | 2-3 | ~800-1200 |
| **合计** | **13** | **30-40** | **12k-16k** |

## 执行顺序与依赖

```
Phase 0   00a (INV foundations) ----+
              v                     |
          00b (migrations + RLS) ---+--> 00c (openapi + shell)
                                              |
Phase 1   01a (coworkers + runs writer) <-----+
              v
          01b (WS protocol + state machine)
              v
          01c (frontend chat new WS)
              |
Phase 2   02a (models + credentials + MCP) <--+
              v
          02b (tools dual-write + reader switch)
              v
          02c (credential_proxy user-mode + fake-vault e2e)
              |
Phase 3   03a (approvals to v1 + multi-user smoke) <--+
              v
          03b (skills per-tenant + UI)
              |
          (Phase 2 实跑一段时间后)
              v
          03+ (drop coworkers.tools column)
              |
Phase 4   04  (safety UI to v1)
```

## Session 状态跟踪

| Session | 标题 | 状态 | 完成日期 | 备注 |
|---|---|---|---|---|
| 00a | INV foundations | done | 2026-05-20 | 7 个 punch list 项；无 migration |
| 00b | Migrations + RLS | done | 2026-05-20 | 6 张新表 + 4 处 ALTER；2 个 commit；greenfield rename of skills.created_by |
| 00c | OpenAPI 脚手架 + shell 抽离 | done | 2026-05-20 | 3 个 commit；codegen + freshness + contract pytest；shell 不重构 chat-panel |
| 01a | Coworkers CRUD + runs 写入责任人 | done | 2026-05-20 | 4 个 commit；coworkers CRUD + runs lifecycle helper + auth/ws-ticket；39 个新测试全绿 |
| 01b | WS 新协议 + run state machine | done | 2026-05-20 | 4 个 commit；REST conv/messages/runs + WS v1 stream + 7-path INV-6 + INV-7 enum 翻译；70 个新测试全绿 |
| 01c | 前端 chat 接入新 WS | done | 2026-05-20 | 3 commits; v1_client + chat-panel rewrite + Coworkers list; 13 vitest cases; lint:no-admin-chat enforces v1 cutover |
| 02a | Models + Credentials + MCP CRUD | not started | — | 不含 `auth_mode=user` 路径 |
| 02b | `coworker.tools` 双写 + reader 切换 | not started | — | **不 drop 列**（推到 03+）|
| 02c | credential_proxy user-mode + fake-vault e2e | not started | — | OIDC wiring 兜底 |
| 03a | Approvals 迁 v1 + 多 user smoke | not started | — | 方案 A 多 bootstrap user 实跑 |
| 03b | Skills per-tenant 迁移 + UI | not started | — | 双写期保留 `coworker_id` |
| 03+ | drop `coworkers.tools` 列 | not started | — | 必须等 02b 实跑足够时间 |
| 04 | Safety UI 迁 v1 | not started | — | 基本搬迁 |

更新方法：手动维护此表。每个 session 完成后填 ✅ + 日期。

## 如何执行一个 session

1. 开一个**新的 Claude Code 会话**（不要复用前一个 session 的 context）
2. 输入：

```
请读取 docs/webui-backend-v1.1-sessions/<session-id>.md，按照里面的描述完成所有任务。
完成后运行验收清单中所有检查项，确保通过。
更新 docs/webui-backend-v1.1-plan.md 的状态表。
```

3. 该 session 结束后：
   - 把 plan 文件的状态改为 `done` + 日期
   - 如果有跨 session 的发现（比如某个 PR 实际拆成了两个，或者发现新的不变量），在 session 文件末尾加 "Findings" 段
   - **下游 session 的 prompt 可能需要刷新**——Phase 2-4 session prompt 标了 `[DRAFT]`，到执行前再 review 一遍

## 跨 session 工作约定

- **必须同 session 内完成的耦合**（拆 session 会导致 INV 形同虚设）：
  - IPC mixin + 全部 dataclass apply + INV-2 pinned test
  - Migration + RLS policy + 第一个写入路径
  - Wire enum 翻译层 + INV-7 pinned test
  - Run state machine UPDATE + INV-6 pinned test 枚举所有终止路径
  - 容器 cleanup image whitelist + INV-3 pinned test + 手动外来容器 smoke

- **必须跨 session 的拆分**（同 session 做完会掩盖 bug）：
  - Backend API 与前端集成（前端在 backend smoke 通过后另开 session）
  - `coworker.tools` 双写+reader 切（02b）与 drop 列（03+）
  - OIDC fake-vault e2e（02c）与真 IdP 接入（推迟到 OIDC 分支）

## 下游 session prompt 刷新规则

Phase 2-4 prompt 都是基于"上游 session 结果未知"的草案。每次执行 Phase X session 前：

1. 把已完成 session 的 "Findings" 段读一遍
2. 检查刷新清单（每个下游 prompt 末尾都有）
3. 必要时在执行前由用户和 Claude 一起更新 prompt

## 参考文档（按重要性）

1. [`docs/webui-backend-v1.1-design.md`](./webui-backend-v1.1-design.md) — 设计源
2. [`docs/5-webui-architecture.md`](./5-webui-architecture.md) — 现有 webui 架构
3. [`docs/18-rls-architecture.md`](./18-rls-architecture.md) — RLS 现状
4. [`docs/19-skills-architecture.md`](./19-skills-architecture.md) — skills 现状
5. [`STEPS.md`](../STEPS.md) — 已完成 step 的历史
6. [`CLAUDE.md`](../CLAUDE.md) — 用户偏好（如有）
