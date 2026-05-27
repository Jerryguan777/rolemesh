# Chore Session 1 — D2 simplify + D3 fix

| field | value |
|---|---|
| Branch | `chore/config-db-truth`（off main，已起 + push） |
| Prerequisites | none（D2 shipped 在 main：commits `6eafd33` + `25834a5`）|
| Estimated PRs | 2（每 drift 一个 commit）|
| Estimated LOC | ~150-200（D2 净 -130 + D3 +80-100） |
| Status | not started |

> **Trigger**：`docs/config-drift-fix-plan.md` §3 D2 / D3。两个同主题"DB 列有值，runtime 应该读它"的 small fix。同 session 同分支，2 commits。
>
> **核心约束**：**反 over-engineering 严格姿态**。D2 当前 shipped 是 293 LOC（实际仅需 ~80）—— 本 session 同 spirit 不要重蹈覆辙。

## Goal

1. **简化 D2**：把已 shipped 的 Pi model_id wiring 从 293 LOC 缩到 ~150 LOC，删过度抽象 + 删 spec-confirming tests，保留 mutation-resistant 测试
2. **修 D3**：`tenants.max_concurrent_containers` DB 列有值 → orchestrator 实际读它做 per-tenant 限制；保留 `GLOBAL_MAX_CONTAINERS` 兜底

**两个 drift 同根因**：DB 列存在但 runtime 不读。修法都是"加一处 lookup + 兜底"。

## Required reading

1. [`docs/config-drift-fix-plan.md`](./config-drift-fix-plan.md) §3 D2 / D3 + §5.A / §5.C
2. **2026-05-25 conversation 关于 D2 over-engineering 反思**：D2 现状 293 LOC，最小所需 ~80 LOC；2-3x bloat 来自：
   - 重构诱惑（拆 `pi_extra_env` 成两个 pure 函数）
   - 过度测试（8 个，含 4 个 spec-confirming pass-through）
   - 3 条 fallback log（1 条 try/except 即可）
   - Docstring/注释膨胀
3. D2 shipped 代码：
   - `src/rolemesh/agent/executor.py` (含 `pi_format_model_id` / `pi_env_for_model_id`)
   - `src/rolemesh/agent/container_executor.py` (spawn lookup + 3-branch fallback)
   - `src/rolemesh/container/runner.py` (`pi_model_id_override` 参数)
   - `tests/container/test_runner.py` (8 tests)
4. D3 修改面：
   - `src/rolemesh/main.py` (`OrchestratorState(global_limit=GLOBAL_MAX_CONTAINERS)`)
   - `src/rolemesh/orchestration/` 或类似（spawn 路径 check limit）
   - `src/rolemesh/db/tenant.py` (`list_tenants` / `tenants.max_concurrent_containers` 读取)

## 显式禁令（防 v2-A / D2 bloat 教训重演）

**不允许做的事**：

- ❌ **不抽 helper / 不提 pure function** —— 除非真有第二个 caller。`pi_format_model_id` 当前只在 1 处用，inline。
- ❌ **不写 spec-confirming tests** —— "openai pass-through" / "anthropic pass-through" 这类测试是"验 spec 就是这样"不是"invariant 不会破"。删掉。**每个 invariant 1 个 mutation-resistant test 即可**。
- ❌ **不做 quota policy 抽象层** —— 直接 `dict[tenant_id, int]` lookup，不抽 `LimitPolicy` 类
- ❌ **不加 admin endpoint** inspect current usage（"看一下能不能 spawn" 之类 debug endpoint）
- ❌ **不写 logger.info debug 路径** —— 异常路径 logger.warning 一行
- ❌ **不重构 `OrchestratorState` 已有字段** —— 加 dict，不改其它
- ❌ **不加 tenant created/updated 时 hot-reload limits** —— YAGNI，dev 阶段 tenant 不常变；启动时 load 一次即可，新加 tenant 用 `GLOBAL_MAX_CONTAINERS` fallback 直到下次 restart
- ❌ **不加 Docstring 段** 给每个函数解释架构理由 —— 类型注释 + 名字清晰即可；架构注释放 module 顶部一段
- ❌ **不改 D2 shipped 的 public API contract** —— `pi_model_id_override` 参数名 / `coworker.model_id` 读取语义保持

## Scope — PR breakdown

### PR 1 (commit 1) — D2 simplify

**Goal**：把 D2 实现压到 ~150 LOC（删 ~140 LOC + 加 ~0 LOC）。

子任务（按顺序）：

1. **`executor.py` 折叠两个 pure 函数回 `pi_extra_env`**：
   - 删 `pi_format_model_id()` 独立函数
   - 删 `pi_env_for_model_id()` 独立函数
   - 把核心逻辑（provider rename + PI_MODEL_ID 设置 + Bedrock env 加成）inline 回 `pi_extra_env` 原函数
   - 保留 `_DB_TO_PI_PROVIDER` 常量（小 dict 是合理 helper）
   - 注释收窄到 1-2 行说"why"，不是"what"

2. **`container_executor.py` 3-branch fallback → 1 try/except**：
   - 删 3 个独立的 `if` 分支 + 3 条 warning
   - 改成：
     ```python
     pi_override = None
     if self._config.name == "pi" and coworker.model_id:
         try:
             m = await get_model_by_id(coworker.model_id)
             if m:
                 pi_override = _format_pi_model_id(m.provider, m.model_id)
         except Exception:
             logger.warning(
                 "Pi model_id resolution failed; falling back to env",
                 coworker_id=coworker.id, model_id=coworker.model_id,
             )
     ```
   - `_format_pi_model_id` 可以是 `executor.py` 内的 module-level 函数（最小公共）；只要 1 个 caller，inline 也行。本 session 内决定（看哪个更简洁）

3. **`runner.py` 保留**：`pi_model_id_override` 参数 + override path。0 改动（这个本来就 minimal）

4. **`test_runner.py` 删 5 个 spec-confirming tests，保留 3 个 mutation-resistant**：
   - 保留：`test_pi_model_id_override_replaces_default`（核心 invariant）
   - 保留：`test_pi_model_id_override_none_keeps_default`（fallback invariant）
   - 保留：`test_bedrock_renames_to_amazon_bedrock`（provider rename invariant，有 bug 时立即红）
   - **删**：`test_pi_model_id_override_recomputes_bedrock_env`（重复了 Bedrock rename test 的 invariant，区别只是 env 字段名）
   - **删**：`test_pi_model_id_override_ignored_for_claude_backend`（spec：Claude backend 不读 PI_MODEL_ID 显而易见）
   - **删**：`test_openai_passes_through_unchanged`（spec：unknown provider passthrough）
   - **删**：`test_anthropic_passes_through_unchanged`（同上）
   - **删**：`test_unknown_provider_passes_through`（同上）

5. **`container_executor.py` 添加测试**：如果 spawn lookup path 还没单测覆盖，加 1 个：
   - `test_spawn_resolves_coworker_model_id_from_db` ——mock get_model_by_id + 验 build_container_spec 收到 override
   - 如果 DB lookup fail 路径也想测：1 个 `test_spawn_lookup_failure_falls_back_to_env`

**目标 LOC delta**：净 -140 LOC（删 ~150，加 ~10）

**Commit message 模板**：

```
chore(d2): simplify Pi model_id wiring to minimal form

The shipped D2 (6eafd33 + 25834a5) was 293 LOC; honest minimum
is ~150. This commit trims the bloat:

- Fold pi_format_model_id / pi_env_for_model_id back into
  pi_extra_env (no second caller, no testability benefit)
- Replace 3-branch fallback logging with 1 try/except + 1 warning
- Drop 5 spec-confirming tests; keep 3 mutation-resistant ones
  (override / no-override / bedrock-rename)

Behavior unchanged. The public Pi container env contract and
build_container_spec(pi_model_id_override) signature are stable.

Net: -140 LOC. Companion to D3 (next commit) covering the
config-drift-fix-plan §3 D2/D3 simplification cycle.
```

### PR 2 (commit 2) — D3 per-tenant container limit

**Goal**：`tenants.max_concurrent_containers` 真生效，~80 LOC。

子任务（按顺序）：

1. **`OrchestratorState` 加两个 dict**（~10 LOC）：
   ```python
   @dataclass
   class OrchestratorState:
       # existing fields...
       global_limit: int = GLOBAL_MAX_CONTAINERS  # fallback for unknown tenant
       tenant_limits: dict[str, int] = field(default_factory=dict)
       running_per_tenant: dict[str, int] = field(default_factory=dict)
   ```

2. **启动时 load 一次**（~15 LOC）：
   ```python
   async def _load_tenant_limits(state: OrchestratorState) -> None:
       """Load per-tenant max_concurrent_containers at orchestrator boot.
       New tenants created post-boot fall back to global_limit until
       orchestrator restart — acceptable for dev stage."""
       async for tenant in iter_tenants():  # 或 list_tenants()
           state.tenant_limits[str(tenant.id)] = tenant.max_concurrent_containers
   ```
   - `main.py` 启动序列调用一次
   - 不订阅 NATS event 做 hot-reload

3. **spawn 前 check + counter 维护**（~25 LOC）：
   - 找当前 spawn 路径的 limit check（grep `global_limit` / `_can_spawn` 等）
   - 改成：
     ```python
     def can_spawn(state, tenant_id) -> bool:
         limit = state.tenant_limits.get(tenant_id, state.global_limit)
         return state.running_per_tenant.get(tenant_id, 0) < limit

     def on_spawn(state, tenant_id) -> None:
         state.running_per_tenant[tenant_id] = state.running_per_tenant.get(tenant_id, 0) + 1

     def on_terminate(state, tenant_id) -> None:
         state.running_per_tenant[tenant_id] = max(0, state.running_per_tenant.get(tenant_id, 0) - 1)
     ```
   - 现有 global counter（如果有）保留作为 process-wide 兜底（也可删；看哪个简洁）

4. **3 个 mutation-resistant tests**（~50 LOC）：
   - `test_per_tenant_limit_blocks_excess_spawn` —— tenant A limit=2，spawn 3 个第 3 个被拒
   - `test_unknown_tenant_uses_global_default` —— tenant_id 不在 limits dict 中走 global_limit
   - `test_tenant_quotas_are_independent` —— A 满了不影响 B（验隔离）

5. **手动 smoke 说明**（commit message 内）：
   - 起 dev，BOOTSTRAP_USERS=alice/bob 不同 tenant slug
   - alice tenant 配 max_concurrent_containers=1（admin endpoint 或 SQL）
   - alice spawn 2 个 coworker chat → 第 2 个被拒
   - bob 同时 spawn 不受影响

**Commit message 模板**：

```
chore(d3): per-tenant container limit reads tenants.max_concurrent_containers

DB column has existed since v1.1 04 but OrchestratorState used
process-wide GLOBAL_MAX_CONTAINERS instead. Multi-tenant quota
isolation was non-functional — one tenant could saturate the
process and lock out others.

Adds:
- OrchestratorState.tenant_limits + running_per_tenant dicts
- _load_tenant_limits() at orchestrator boot reading the DB column
- can_spawn() / on_spawn() / on_terminate() helpers for per-tenant
  counter maintenance

GLOBAL_MAX_CONTAINERS env retained as fallback for tenants not in
the cached dict (e.g. created post-boot). Not hot-reloaded —
restart picks up new tenants. Acceptable for dev stage.

3 mutation-resistant tests:
- per-tenant limit blocks excess spawn
- unknown tenant falls back to global
- tenant quotas are independent

Companion to D2 simplify (previous commit). Companion to
config-drift-fix-plan.md §5.C.
```

### 完成后更新 plan

在 `docs/config-drift-fix-plan.md` §3 D2 标 "shipped + simplified 2026-05-26"；§3 D3 标 "shipped 2026-05-26"。§5.A / §5.C 标完成。

## Acceptance criteria

- [ ] D2 净 LOC delta < -100（删多于加；不强求 -140，但不能 > -100）
- [ ] D2 现有 8 tests 剩 3 个 mutation-resistant
- [ ] D2 行为不变（手动 smoke：Pi coworker 用 wizard 选的 model，不是 host PI_MODEL_ID env）
- [ ] D3 `OrchestratorState.tenant_limits` 启动后非空（load 自 DB）
- [ ] D3 3 mutation-resistant tests 全绿
- [ ] D3 手动 smoke：alice tenant limit=1 时 spawn 第 2 个被拒，bob 同时 spawn 不受影响
- [ ] 全套现有测试不退化
- [ ] OpenAPI / contract test 不动（本 session 不碰 API surface）
- [ ] 2 commits 都用 `git commit -s` 累在 `chore/config-db-truth`
- [ ] `git push origin chore/config-db-truth`
- [ ] 更新 `docs/config-drift-fix-plan.md` 标 D2/D3 完成

## Out of scope

- ❌ **D1 (credential_proxy DB lookup)** —— session 2 工作
- ❌ **D4 (mcp_servers.credential_ref 串到容器)** —— session 2 工作
- ❌ **改 `OrchestratorState` 其它字段** —— 只加 2 个 dict
- ❌ **hot-reload tenant.max_concurrent_containers** —— YAGNI
- ❌ **admin endpoint inspect quota usage** —— ops tool 是独立 chore
- ❌ **per-coworker limit override**（如 coworker 自带 limit 字段覆盖 tenant 的）—— YAGNI
- ❌ **quota policy 抽象层** —— 直接 dict
- ❌ **改 D2 的 public contract**（`pi_model_id_override` 参数名等）—— 只 trim 内部
- ❌ **删 evaluation CLI 兼容路径**（如果 CLI 调 `pi_extra_env`，保持工作）

## Open questions（session 内自决）

1. **`_format_pi_model_id` inline vs module-level**：1 个 caller 时 inline 更简洁；如果格式逻辑 > 5 行，module-level 更可读。session 内看具体行数决定
2. **`OrchestratorState.tenant_limits` 是否预填 default tenant**：dev 通常只 1 个 default tenant；预填 vs lazy 都行。推荐 lazy（启动时 `_load_tenant_limits` 自然填）
3. **`running_per_tenant` counter 与现有 global counter 关系**：保留 global 做 process-wide safety net，还是完全替代？推荐保留（global 是 process 资源上限，per-tenant 是策略上限，两层防御不冲突）
4. **`iter_tenants` vs `list_tenants`**：现有 db 接口名；grep 确认；不要新建

## Pitfalls

- **不要"顺便重构"** —— 任何不属于 D2 简化 / D3 修的代码碰一行都是 scope creep
- **mutation-resistant 不是 happy-path** —— 测试要保证"改坏代码会红"，不是"代码这样跑就过"。删 spec-confirming test 时如有疑虑，问：把被测代码注释一行，测试还会过吗？如果会过，删
- **D3 counter 必须配对** —— `on_spawn` 加 1 必有对应 `on_terminate` 减 1；找现有 terminate 路径（INV-6 7 终止路径之一）加 counter 维护
- **D3 启动顺序**：`_load_tenant_limits` 必须在 spawn 路径接收请求之前；如果是 async startup，await 完成
- **D2 行为兼容**：删 pi_format_model_id 时确认没有外部 caller（grep）
- **不要 logger.info dev info**——只在异常路径 logger.warning；正常路径 silent
- **lookup 失败不抛**——graceful fallback 是 design choice，强制抛会让 spawn 全失败

## 执行前刷新清单

- [ ] 当前 main 分支干净（无 D2/D3 in-flight 改动）
- [ ] `chore/config-db-truth` 分支已 checkout（git branch --show-current）
- [ ] D2 shipped 状态确认（git log main grep `feat(spawn): wire coworker.model_id` → 找到 6eafd33）
- [ ] `tests/container/test_runner.py` 当前 8 个 tests grep 确认（删 5 加 0）
- [ ] D3 现有 spawn 路径 grep（`global_limit` / `_can_spawn` / `MAX_CONCURRENT`）找到 limit check 位置

## Findings (after execution)

_(empty — 重点记录：D2 实际 LOC delta / D3 spawn 路径具体位置 / 是否发现 D2 还有其它 over-engineering 残留 / 对 session 2 (D1+D4) 的影响)_
