# HITL Tool Approval — Session Kickoff Prompts

Paste each block, in order, into a fresh Claude Code session running in
`/home/jerry/ai/rolemesh`. Full context lives in
[`21-hitl-approval-plan.md`](./21-hitl-approval-plan.md) — every prompt tells the
session to read it first.

**Branch:** all sessions commit to `feat/hitl-approval-B`. No per-session PR;
merge to `main` only after S5 is complete.

**Order / gates:**
- S1 must finish (RLS + pure-function tests green) before S2/S3.
- S3's race tests must be green before S4. If S3 doesn't finish, re-paste the S3
  prompt into a new session — it picks up from `git log` (this is "S3-cont").
- Run sequentially on the single branch (parallel S2/S3 needs separate worktrees).

---

## S1 — Foundation + freeze contract

```
我们在 RoleMesh 上实现 HITL 工具审批(greenfield 重写,不要捞已删除的旧 approval 代码)。
先完整阅读 docs/21-hitl-approval-plan.md —— 它是所有 session 的共读契约。

git checkout feat/hitl-approval-B，先 git log --oneline 看清现状。

本次执行 S1(地基 + 冻结契约),严格按文档 §4/§5/§7:
- 新建 approval_policies / approval_requests 两张表 + RLS。用真实的单 predicate
  tenant_id = current_tenant_id() 范式(db/schema.py:1399 模板),不要用文档里点名
  否定的 "dual-pool" 叫法。
- db/approval.py CRUD,经 tenant_conn / admin_conn。
- agent_runner/approval/policy.py:纯函数 evaluate_condition + find_matching_policy,
  算子 == != > >= < <= in not_in contains 与 and/or/always,fail-closed(缺字段/类型不符/
  异常 → 要求审批)。
- core/config.py:APPROVAL_TIMEOUT=300_000,加启动断言 APPROVAL_TIMEOUT < IDLE_TIMEOUT + 30_000。
- 确认 §3 的 IPC 三主题字段 schema,作为 S2/S3 的硬契约(若需微调,先改文档再实现)。

测试按项目对抗式理念(发现真实 bug、非镜像、最小 mock):condition 边界(空 params、
缺字段、类型不符、嵌套 and/or、always)、fail-closed 必须要求审批、priority→updated_at
平局选取、跨租户 RLS 读写隔离。

Exit:纯函数 + RLS 测试全绿;契约确认。git commit -s 增量提交,不要开 PR、不要合并 main。
```

---

## S2 — Container blocking hook + IPC

```
继续 RoleMesh HITL 工具审批。先完整阅读 docs/21-hitl-approval-plan.md。
git checkout feat/hitl-approval-B,git log --oneline 看 S1 已落地什么,基于 §3–§5 冻结契约开工。

本次执行 S2(容器侧阻塞 hook,§6 锚点 + §9 R1):
- Approval hook handler:非 mcp__* 直接 return None 放行;命中策略 → publish
  agent.{job_id}.approval_request → await decision Future(APPROVAL_TIMEOUT 兜底)→
  approve 返回 None / reject·timeout 返回 ToolCallVerdict(block=True, reason=...)。
- request_id → asyncio.Future 字典;backend start() 订阅 approval_decision,把回执接回各
  await 点;支持同一轮并发多审批(§6 已确认多 ToolUseBlock 并发派发 → 用 set 语义,不要布尔)。
- finally 确定性 publish approval_cancel(幂等),覆盖 reject/timeout/Stop(CancelledError)/异常。
- 容器 init 加载 policy 快照。
- 必须解决 R1:实测 MCP stdio/http 连接与凭据代理 token 在阻塞数分钟后是否仍有效;
  明确定义"放行后工具执行失败"如何回报用户,并把结论写进交接记录/文档。

测试(对抗式):按策略放行/拦截;timeout→block;Future 路由;同一轮并发双审批各自独立接回;
finally 在 reject/timeout/Stop/异常四条路径都发 cancel;非 mcp__ 前缀直接放行。

Exit:容器侧审批闭环可对桩 orchestrator 跑通;R1 结论记录。git commit -s 增量提交,不合并 main。
```

---

## S3 — Orchestrator suspend/resume + restart recovery (highest risk)

```
继续 RoleMesh HITL 工具审批。先完整阅读 docs/21-hitl-approval-plan.md,重点吃透 §8(全方案最难)。
git checkout feat/hitl-approval-B,git log --oneline 看 S1/S2 已落地什么。

本次执行 S3(编排侧 idle 挂起/恢复 + 过期 sweep + 重启恢复,§8 + §9 R2):
- 挂起(收 approval_request):落库 pending+expires_at;idle_handle.cancel()(封路径 A);
  强制 state.idle_waiting=False + 断言(封 scheduler.py:202/266 路径 B/C);
  awaiting_approval[key].add(request_id) —— 用 set 不用布尔;发一次"⏳等待审批中"。
- 恢复(收 approval_decision/cancel):discard;当且仅当 set 空 → re-arm 完整 IDLE_TIMEOUT;
  回发 decision 给容器。
- awaiting_approval 是按 §5 队列键(conversation_id→coworker_id)的共享 dict;不重构 GroupQueue 键模型。
- 过期 watcher(容器 SIGKILL 兜底);决策竞态:容器 Future 先到先赢 + 行级 status 幂等。
- R2 重启恢复要写全:_groups 是内存,重启即空但审批中容器仍存活。扫 pending 行,逐行:
  未过期 → 重建 _GroupState + 重放挂起(cancel idle_handle + 强制 idle_waiting=False +
  awaiting_approval.add)+ 重建 decision 路由(由 job_id 推主题)+ 重建过期 watcher;
  已过期 → 标 expired + 发硬通道通知。只重载行不重建 _GroupState/挂起 → 恢复后容器立即被收割。

测试(计时器生命周期专项):挂起→re-arm→正常 teardown;挂起→容器超时→teardown;
并发双审批→先后清空→仅末次 re-arm;重启恢复→存活容器被重新接管而非被收割;防 double-cancel/误清。

Exit:三条计时器生命周期 + 重启恢复测试全绿。若末尾竞态测试未全绿,本分支续接做完(S3-cont),
不要推进到 S4。git commit -s 增量提交,不合并 main。
```

---

## S4 — Delivery + dual-channel notify + E2E (MVP)

```
继续 RoleMesh HITL 工具审批。先完整阅读 docs/21-hitl-approval-plan.md。
git checkout feat/hitl-approval-B,git log --oneline 确认 S1–S3 已全部落地且测试绿(尤其 S3 竞态)。

本次执行 S4(投递 + 双通道通知 + 端到端,§10 S4 + §9 R4):
- 目标解析:conversation_id → channel_bindings → channel_chat_id;定时任务无活跃会话→回落最近会话。
- Telegram:内联 ✅/❌ 卡片 + CallbackQueryHandler,callback_data="apr:{request_id}"/"rej:{request_id}"。
  main 上现在没有 inline keyboard/callback,全新写(不要捞旧代码)。IDOR 防护:审批人身份从认证
  握手(ticket+DB)解析,绝不信客户端 payload。
- Web:v1 WS 新增一个 client frame(schemas_v1.py:835 范式:pydantic 成员 + WsClientFrameModel union +
  ws_stream 接收分支 + publish NATS + OpenAPI 重新生成 + ts client)+ 推审批事件;定时任务 Web 通知需持久化。
- 结果双通道:软(block reason 进 agent 上下文)+ 硬(orchestrator 确定性把卡片编辑成"❌已拒绝"/
  "⏰已超时",不经 LLM)。
- R4:在代码/文档一句话钉死 safety require_approval 仍是硬 block、不进 HITL。

验证:端到端 amount>100 自审,Telegram 与 Web 各跑一遍(批准→agent 拿结果继续;拒绝→agent 收 block
reason + 用户收硬通道卡片);resume("继续"→重试工具→再次命中→新审批)。

Exit:MVP 端到端在两个通道都可用。git commit -s 增量提交,不合并 main。
```

---

## S5 — Policy CRUD + isolation hardening + docs

```
继续 RoleMesh HITL 工具审批,收尾。先完整阅读 docs/21-hitl-approval-plan.md。
git checkout feat/hitl-approval-B,git log --oneline 确认 S1–S4 已落地、MVP 端到端可用。

本次执行 S5(§10 S5):
- 策略 CRUD REST + Web UI 条件构建表单。
- attack-sim 跨租户隔离:租户 A 看不到/批不了租户 B 的审批(RLS + IDOR)。
- 完善 docs/21-hitl-approval-plan.md 并补一份 -cn.md 翻译(按 repo 惯例保留 agent/hook/skill 等英文术语);
  把 block-and-await vs 旧 block-and-replay 的差异、R1 结论、R2 恢复语义都写进去。

测试(对抗式):跨租户隔离必须真能挡住越权读/决策。

Exit:跨租户隔离测试全绿;文档完整。

全部完成后告诉我,我来 review 并把 feat/hitl-approval-B 合并 main(本特性约定:整个完成才合并)。
```
