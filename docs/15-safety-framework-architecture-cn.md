# Safety Framework 架构

本文档介绍 RoleMesh 的 Safety Framework——一个供管理员在不写代码的情况下，对 agent 的输入、tool 调用、输出施加运行时检测与拦截的策略框架。

它涵盖为什么这是一个**框架**而不是若干硬编码 check、考虑过哪些抽象层级并为何被否决、Stage / Context / Check / Rule 四件套的设计意图，以及 V1 故意没做的事。

目标读者：将来添加新 Check（PII 检测器、prompt injection classifier、egress 规则等）、调试某条规则为何没触发、把 Safety 移植到新 stage、或理解"为什么不用 OPA/CEL"的开发者。前置阅读：[`13-safety-overview.md`](13-safety-overview.md) §2.4 / §2.5 / §2.7 / §2.8。

---

## 背景：散落的安全检测需求

Container Hardening 关了"容器逃逸"，但 agent 在容器内**业务上**还可能做这些事：

- 把 prompt 里收到的 SSN / 信用卡号塞进 tool 参数发往外部
- 被 prompt injection 诱导调用未授权的高危 tool
- 把 LLM 输出的内容（可能含上游泄漏的 API key）直接回给用户
- 死循环烧 token、对同一个 tool 反复调用、被薅羊毛

这些问题的形态各异，但本质都是**"在某个明确的事件点（tool 调用前、prompt 进入前、模型输出后）跑一段检测逻辑，根据结果决定放行 / 拦截"**。

历史上类似需求往往各自实现：每加一种检测就写一段独立代码，加 hook、加 DB 表、加 REST API、加审计。结果是：

- 几种检测的拦截语义不一致（一个 raise exception、一个返回 None、一个写日志后放行）
- Fail-mode 各自决定（一个 fail-close、一个 fail-safe，没人记得哪个是哪个）
- 审计格式不统一（A 检测写 audit log、B 检测只写 stderr）
- 每加一种检测都要改 agent 主流程代码

Safety Framework 的存在就是为了**把这一类工作抽象到一个统一的形态下**，让"加一个新安全能力"变成"写一个 Check 类 + 注册一行配置"，不需要碰 agent 主流程、不需要新建表、不需要新 REST 端点。

---

## 设计目标

1. **唯一的扩展单元是 Check。** 一个 Check 类 = 一种检测 + 它自己的 action 语义。新增检测不改 pipeline、不改 DB schema、不改 REST 路由。
2. **未启用时零开销。** 一个租户没有配置任何规则时，行为与 Safety Framework 不存在时完全一致——hook handler 不注册、import 不发生、热路径无额外查询。
3. **统一的 fail-mode 语义。** Control stage（pre tool call、input prompt、model output）fail-close；observational stage（post tool result、pre compaction）fail-safe。所有 Check 都遵守这一约定，不允许自行决定。
4. **统一的审计形态。** 所有 Check 的决策都写入同一张 `safety_decisions` 表，含 `triggered_rule_ids` / `findings` / `context_digest`。审计员看一张表能看全。
5. **多租户原生隔离。** 所有规则按 `tenant_id` 强制过滤；`coworker_id=NULL` 表示租户全局；不允许跨租户继承。
6. **数据最小化。** 审计存 `context_digest`（SHA-256）+ 简短 summary，**不存 payload 原文**——防 PII 通过审计表二次泄漏（详见 [`13-safety-overview.md`](13-safety-overview.md) §2.7）。
7. **热更新（下一个 job 生效）。** 管理员改了规则，下一次 agent invocation 拿到新快照；进行中的 job 用启动时的快照不变——避免规则中途变更导致行为漂移难调试。

---

## 已考虑的备选方案

### 方案 A——硬编码每种检测

每加一种安全检测就直接在 agent_runner 里写 if / else：

```python
async def on_pre_tool_use(event):
    if has_pii(event.params): return block(...)
    if hits_prompt_injection(event.params): return block(...)
    if hits_rate_limit(event.tenant_id): return block(...)
    ...
```

**优点**：直观，没有抽象层。

**缺点**：散落的 fail-mode、不一致的审计、改一种检测可能影响其他检测的命中顺序、加新检测要改主流程代码。**和本框架所追求的"声明式策略"形态完全冲突**——admins 没法不写代码就配规则。

**否决**——不可扩展。

### 方案 B——引入 OPA / CEL 这种 policy DSL

每条规则写成 Rego 或 CEL 表达式：

```rego
deny[reason] {
    input.stage == "pre_tool_call"
    input.params.amount > 1000
    reason := "金额超限，已阻断"
}
```

**优点**：表达力强；admins 可写复杂条件；业界成熟（Kubernetes Admission、Envoy RBAC 都用）。

**缺点**：
- 运维门槛飙升——admins 要学一门新 DSL
- DSL 引擎本身是个外部依赖（OPA sidecar 或 celpy 库）
- 调试体验差——一条 Rego policy 不触发时很难定位原因
- 真要做 PII 检测、LLM Guard prompt injection 这种事，**仍然要写 Python 代码**——DSL 只能写 if-then，调不了 ML 模型
- 当前所有真实需求都能用"一个 Check 类 + 几个 boolean 参数"覆盖，**还没到需要 DSL 的复杂度**

**否决（V1/V2）**——明确推迟到"复合 check 类不够用"时再评估。注意是"否决 DSL 表达式"，不是"否决策略框架"——后者就是 Safety Framework 本身。

### 方案 C——用现成的 LLM safety framework（NeMo Guardrails、LangChain Guardrails 等）

直接接入某个开源 framework。

**优点**：少写代码。

**缺点**：
- 它们默认场景是"单 LLM 应用"，不是"多租户 agent orchestrator"
- 审计 / 多租户 / NATS IPC 都要深度定制——最后还是要包一层
- 锁定到该 framework 的演进节奏

**否决**——自建 1500 LoC 骨架 + 适配现成 detector 库（Presidio、LLM Guard、Lakera、OpenAI Moderation）是更稳的路。

### 方案 D——四件套抽象 + Check 是唯一扩展单元（选中）

- **Stage** 枚举所有决策点（pre tool call、input prompt、model output、post tool result、pre compaction）
- **SafetyContext** 是只读数据载体，按 stage 携带不同 payload
- **SafetyCheck** Protocol：每个 check 类声明它支持的 stage、cost class、稳定 Finding code、可选 pydantic config schema
- **Rule** 是 DB 行：选一个 check + 配它的 config + 绑 stage + 绑 scope（tenant + 可选 coworker）

**优点**：所有目标都满足；新增能力 = 写 Check 类 + 注册一行；不引入 DSL。

**选中。** 本文档其余部分描述的就是这一形态。

---

## 核心抽象

只有 4 个概念，**这是有意为之**——任何想引入第 5 个概念的扩展都要先证明不能复用既有四个。

### Stage

`StrEnum`，列出所有"安全决策可能发生的事件点"：

```
INPUT_PROMPT       — 用户 prompt 进入 agent 前
PRE_TOOL_CALL      — tool 调用执行前
POST_TOOL_RESULT   — tool 返回结果回灌 LLM 前
MODEL_OUTPUT       — 模型最终输出回用户前
PRE_COMPACTION     — 长对话压缩前（防压缩后丢失敏感上下文检测窗口）
```

**Control vs Observational 划分**：
- `{INPUT_PROMPT, PRE_TOOL_CALL, MODEL_OUTPUT}` 是 control stage——check 异常 = fail-close = 整个事件 block
- `{POST_TOOL_RESULT, PRE_COMPACTION}` 是 observational stage——check 异常 = fail-safe = 跳过该 check + log，不影响事件继续

这一划分**写在 pipeline 里强制**，不是 check 自己决定。后续如果加 `EGRESS_REQUEST`（[`16-egress-control-architecture.md`](16-egress-control-architecture.md)），必须显式声明它属于哪一档。

### SafetyContext

只读 frozen dataclass，携带一次决策的所有上下文：

```
stage              当前 stage
tenant_id / coworker_id / user_id / job_id / conversation_id
payload            按 stage 不同携带不同字段（详见 types.py 注释）
tool               ToolInfo | None (仅 PRE_TOOL_CALL / POST_TOOL_RESULT)
metadata           dict, 预留扩展
```

每个 stage 的 payload schema 约定：

- `INPUT_PROMPT` → `{prompt: str}`
- `PRE_TOOL_CALL` → `{tool_name: str, tool_input: dict}`
- `POST_TOOL_RESULT` → `{tool_name, tool_input, tool_result, is_error}`
- `MODEL_OUTPUT` → `{text: str}`
- `PRE_COMPACTION` → `{transcript_path, messages}`

新增 stage 必须扩展这份约定，而不是塞进 `metadata`。

### SafetyCheck（Protocol）

```python
class SafetyCheck(Protocol):
    id: str                          # "pii.regex", "egress.domain_rule"
    version: str                     # schema 版本，升级时 bump
    stages: frozenset[Stage]         # 该 check 支持哪些 stage
    cost_class: CostClass            # "cheap" | "slow"
    supported_codes: frozenset[str]  # 该 check 可能产生的稳定 Finding code
    config_model: type[BaseModel] | None  # pydantic schema，REST 层校验 rule.config

    async def check(ctx: SafetyContext, config: dict) -> Verdict: ...
```

**关键约束**（"adapter 纪律"，第三方库适配 check 必须遵守）：
- `supported_codes` 是**稳定枚举**，与外部库的内部分类解耦——外部库升级出新分类，映射层显式丢弃，不渗透到 Finding
- `version` 从 "1" 开始，**不向后兼容的 code 变更必须 bump**
- 每次写新 adapter check（如 `presidio.pii`）都要写"mock 外部库返回未知类型 → 应被丢弃"的单元测试

### Rule

DB 表 `safety_rules` 的一行：

```
id, tenant_id, coworker_id (None=租户全局)
stage, check_id, config (JSONB)
priority, enabled, description
created_at, updated_at
```

**Rule 是 frozen dataclass**——从 DB 加载后即不可变；通过 `Rule.to_snapshot_dict()` 序列化送进容器。

### Verdict

Check 返回值：

```python
@dataclass(frozen=True)
class Verdict:
    action: "allow" | "block" | "redact" | "warn" | "require_approval"
    reason: str | None
    modified_payload: Any | None     # action="redact" 时用
    findings: list[Finding]          # 审计用详情
    appended_context: str | None     # action="warn" 时给 agent 追加上下文
```

**V1 实际只允许 `allow / block`**——pipeline 在控制 stage 上 reject 其他 action（V2 才放开）。这是有意的渐进式投放，避免 V1 还没稳定就引入太多边界场景。

### Finding

每次命中产生的细粒度记录，写进审计：

```python
@dataclass(frozen=True)
class Finding:
    code: str          # 稳定枚举（"PII.SSN", "EGRESS.DOMAIN_DENIED"）
    severity: "info" | "low" | "medium" | "high" | "critical"
    message: str
    metadata: dict     # check 自定义
```

---

## 架构

### 进程拓扑

```
┌── Orchestrator (host process) ──────────────────────────┐
│                                                          │
│  Container Executor                                      │
│    ├─ load_safety_rules_snapshot(tid, cid)               │
│    │    → list[Rule.to_snapshot_dict()]                 │
│    └─ → AgentInitData.safety_rules                       │
│                                                          │
│  REST API (/api/admin/tenants/{tid}/safety/rules)        │
│    ├─ POST/GET/PATCH/DELETE                              │
│    └─ pydantic 校验 (check.config_model)                 │
│                                                          │
│  Safety Engine                                           │
│    ├─ NATS subscribe: agent.*.safety_events              │
│    └─ DbAuditSink.write → safety_decisions               │
│                                                          │
│  CheckRegistry (singleton)                               │
│    └─ orchestrator-side: 所有 check (cheap + slow)        │
└──────────────────────────────────────────────────────────┘
                   │
                   │ AgentInitData (含 safety_rules 快照)
                   ▼
┌── Agent Container (per job) ────────────────────────────┐
│                                                          │
│  agent_runner/main.py                                    │
│    if init.safety_rules: register SafetyHookHandler      │
│                                                          │
│  SafetyHookHandler                                       │
│    on_pre_tool_use → pipeline_run(rules, registry, ctx)  │
│                                                          │
│  pipeline_run                                            │
│    1. filter rules by stage + coworker_id                │
│    2. sort by priority desc                              │
│    3. for each rule: check.check(ctx, rule.config)       │
│       - block → publish audit + 短路返回                  │
│       - allow → publish audit + 继续                     │
│    4. fail-mode 处理 (control vs observational)           │
│                                                          │
│  CheckRegistry (container-side)                          │
│    └─ 只含 cheap check                                    │
│                                                          │
│  AuditPublisher → NATS: agent.{job_id}.safety_events     │
└──────────────────────────────────────────────────────────┘
```

### 数据流（一次 PRE_TOOL_CALL 的完整路径）

```
Claude / Pi backend 决定调用 tool
  ↓
HookRegistry.emit_pre_tool_use(event)
  ↓
SafetyHookHandler.on_pre_tool_use(event)
  ↓
构造 SafetyContext (stage=PRE_TOOL_CALL, ...)
  ↓
pipeline_run(snapshot_rules, registry, ctx, publisher):
  1. 过滤适用规则（stage + enabled + coworker scope）
  2. 按 priority 降序排序
  3. for each rule:
       check = registry.get(rule.check_id)
       verdict = await check.check(ctx, rule.config)
       publisher.publish(audit_event)   ← 异步发 NATS, 不阻塞
       if verdict.action == "block": break
  4. control stage 上 check 异常 → 抛出 (fail-close → BLOCK)
     observational stage → log + skip
  ↓
返回 ToolCallVerdict (block / allow) 给 backend
  ↓
backend 据此执行 or 取消 tool call
```

orchestrator 端独立订阅 NATS 写库：

```
Orchestrator: SafetyEngine
  ↓ NATS subscribe agent.*.safety_events
  ↓
DbAuditSink.write(AuditEvent)
  ↓
INSERT INTO safety_decisions:
  tenant_id, coworker_id, stage, verdict_action,
  triggered_rule_ids[], findings[], context_digest (SHA-256),
  context_summary (前 80 字)
```

### 配置流（rule 创建 → 生效）

```
admin: POST /api/admin/tenants/{tid}/safety/rules
  ↓
REST 校验:
  - check_id 在 orchestrator registry?
  - stage 在 check.stages?
  - config 通过 check.config_model?
  ↓
INSERT INTO safety_rules (+ trigger 写 safety_rules_audit)
  ↓
... (新 rule 此时已生效, 但不会影响进行中的 job) ...
  ↓
下次 ContainerAgentExecutor 启动新 agent:
  load_safety_rules_snapshot(tid, cid) 查最新规则
  → 塞进 AgentInitData
  ↓
容器拿到快照, 整个 job 内不可变
```

### 数据库 schema 概览

| 表 | 用途 |
|---|---|
| `safety_rules` | 规则配置 (含 audit trigger 自动写 `safety_rules_audit`) |
| `safety_rules_audit` | 规则变更时间线（不可被应用层 UPDATE/DELETE） |
| `safety_decisions` | 每次决策审计（只存 digest 不存原文） |

---

## V1 已实现 vs V2 待做

### V1（已合并到 `safety/framework` 分支）

- 5 个 Stage 枚举，wire 了 PRE_TOOL_CALL
- 唯一内置 check：`pii.regex`（SSN / 信用卡 / Email / 美式电话 / IP 正则）
- `safety_rules / safety_rules_audit / safety_decisions` 三张表
- REST CRUD 完整
- pipeline 仅允许 `allow / block` 两种 action
- 容器侧零开销保证（无规则不注册 hook）
- 审计走 fire-and-forget NATS + DB 写入
- 快照式热更新（下一个 job 生效）

### V2（设计完成、待实施）

- 多 Stage wire（INPUT_PROMPT、MODEL_OUTPUT、POST_TOOL_RESULT、PRE_COMPACTION 全部接入）
- 新 action：`redact / warn / require_approval`
- 慢检测 RPC 通道（容器侧 cheap check 同步跑，orchestrator 侧 slow check 走 NATS request-reply）
- 第三方 adapter check：`presidio.pii`、`llm_guard.prompt_injection`、`llm_guard.jailbreak`、`llm_guard.toxicity`、`openai_moderation`、`secret_scanner`（detect-secrets）
- `rate_limit` check（per-tenant / per-tool 计数）
- `domain_allowlist` check（tool input URL 白名单——与 Egress Control 互补）
- 时段调度（`active_hours` / `active_days`）
- 审计 CSV / Webhook 导出
- Admin UI

### 永远不做

- **CEL / Rego policy DSL**——见方案 B 否决理由
- **替换现有 sender_allowlist / mount_security**——它们继续独立存在
- **跨租户策略继承 / 模板市场**——多租户原生隔离的反面
- **本地 GPU 推理 check（Llama Guard 自部署）**——部署复杂度过高，外部 API 调用足够

---

## 取舍与边界

### 接受的取舍

- **快照式热更新（不是实时）**：进行中 job 不感知规则变更——避免行为漂移难调试，代价是 admin 改规则需要新 job 才生效
- **存 digest 不存原文**：审计员看不到具体内容——避免 PII 二次泄漏，代价是查 root cause 时需要去 conversation 历史回溯
- **V1 只 `allow/block`**：用户体验粗——但 V1 阶段先证明骨架，V2 再放开 action 表达力
- **不引入 DSL**：复杂条件要写 Python 类——但避免引入 OPA/Rego 的运维和调试成本

### 边界（不属于 Safety Framework）

- **凭证注入** → `credential_proxy` 独立模块
- **容器网络隔离** → Container Hardening + Egress Control
- **挂载路径白名单** → `mount_security` 独立模块（V1/V2 都不迁入）
- **频道消息发送者白名单** → `sender_allowlist` 独立模块（V1/V2 都不迁入）

把这些放在 Safety Framework 之外，是因为它们各自是**成熟的、形态独立的子系统**——把它们硬塞进框架反而是过度抽象。Safety Framework 解决的是"**未来会持续涌现的新型 LLM 安全检测**"这一类问题。

---

## 一句话总结

**Safety Framework 是一个"加一种新 LLM 安全检测能力 = 写一个 Check 类 + 在 admin 端配一行 rule"的策略框架**。它用 Stage + Context + Check + Rule 四个概念建立统一的运行时决策契约；坚持 fail-closed、零开销、数据最小化、多租户原生隔离、不引入 DSL 五条原则；V1 先用 `pii.regex` 把骨架跑通，V2 通过适配 Presidio / LLM Guard / OpenAI Moderation 等成熟库扩展能力，**自身只做框架不做检测算法**。
