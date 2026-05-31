# Session 00a — INV foundations

| field | value |
|---|---|
| Phase | 0 |
| Prerequisites | none |
| Estimated PRs | 6-7 |
| Estimated LOC | ~1000 (含测试) |
| Status | not started |

## Goal

落地 7 个**无 migration、无破坏性**的防雷基建项，把 INV-2 / INV-3 / INV-4 / INV-5 的 pinned test 立起来。这一步打完后，后续 session 才能在干净的不变量地基上做 migration 与 API 工作。

## Required reading

进 session 前必须看：

1. [`docs/webui-backend-v1.1-design.md`](../webui-backend-v1.1-design.md) §0（不变量定义）/ §11（INV 清单 + pinned tests）/ 附录（Phase 0 punch list）
2. `src/webui/auth.py`（理解 bootstrap fast-path 与 `BOOTSTRAP_USER_ID` 字面量现状）
3. `src/rolemesh/container/docker_runtime.py:250-269`（当前 `cleanup_orphans` 实现，确认它走 name substring）
4. `src/rolemesh/ipc/web_protocol.py` 与 `src/rolemesh/ipc/protocol.py`（理解当前 dataclass deserialize 模式）
5. `src/rolemesh/core/skills.py:82` 附近的 `SKILL_MD_FILENAME` 常量
6. 全局 [`CLAUDE.md`](~/.claude/CLAUDE.md) 的"测试理念"章节——这一步的 pinned test 必须遵循那里的反 mirror-test / 边界优先原则

## Scope — PR breakdown

**Workflow（已锁定）**：本 session 不开子 PR，所有 commit 直接累在 `feat/ui` 分支上。下面每一个"PR N"对应一个独立 commit（或几个小 commit 一组）。session 结束时一次性 `git push` 到 origin/feat/ui。Phase 0 完工后再把 feat/ui 整体合 main。每个 commit 用 `git commit -s`（用户全局规则）。

### PR 1 — `core/skills.py` 常量抽取

**Why first**：最小、零依赖；其它 PR 改动可能 import 它。

- 在 `src/rolemesh/core/skills.py` 顶部抽出常量：
  ```python
  SKILL_MANIFEST_NAME = "SKILL.md"          # already exists as SKILL_MD_FILENAME — rename + re-export
  SKILL_FILE_PATH_RE = re.compile(r"^[a-zA-Z0-9_\-./]+$")  # tighten if needed
  ```
- 把现有 `SKILL_MD_FILENAME` 改成 `SKILL_MANIFEST_NAME`，保留旧名作为 deprecated alias 一个 PR cycle
- grep 全仓 `"SKILL.md"` 字面量，把 hardcode 字符串换成常量引用（**只换 Python，TS 端 PR3 处理**）
- 添加一个 `core/skills_consts_pin.py` 小模块仅 re-export 这两个常量，方便其它模块 import 最小依赖
- 单测：`tests/test_skill_manifest_constant.py`，断言 `SKILL_MANIFEST_NAME == "SKILL.md"` 且常量在 hardcoded 位置出现且字符串一致（INV-5 Python 半边）

**Acceptance**：
- `grep -rn '"SKILL.md"' src/rolemesh/` 只剩常量定义处和必要的字面量出现位置（如 docstring 中的描述）
- 新单测通过
- 全套现有测试通过

### PR 2 — IPC dataclass unknown-keys filter mixin + INV-2 pinned test

**Background**：当前 `ipc/web_protocol.py` 用 `d["x"]` 手挑字段意外满足 INV-2；只要有人改成 `cls(**d)` 就破。设计文档要求统一加 mixin。

- 新建 `src/rolemesh/ipc/_unknown_filter.py`：
  ```python
  from dataclasses import fields
  from typing import TypeVar, Type, Any

  T = TypeVar("T")

  def from_dict_filter_unknown(cls: Type[T], data: dict[str, Any]) -> T:
      """Build a dataclass instance, silently dropping unknown keys.

      Forward-compat across orchestrator/container version skew.
      """
      known = {f.name for f in fields(cls)}
      return cls(**{k: v for k, v in data.items() if k in known})
  ```
- 把 `ipc/web_protocol.py` 与 `ipc/protocol.py` 中**所有** `from_bytes` classmethod 改为先 `json.loads(data)`，再调 `from_dict_filter_unknown(cls, d)`（保留必填字段缺失时的 KeyError 行为——用一个 `required` 参数或在 mixin 内显式校验，不要静默给 default）
- 关键 dataclass 列表（不要漏）：
  - `WebInboundMessage` / `WebStreamChunk` / `WebTypingMessage` / `WebOutboundMessage`
  - `AgentInitData` / `McpServerSpec`（在 `protocol.py`）
  - 任何其它带 `from_bytes` / `from_dict` 的 dataclass —— 先 `grep -rn "from_bytes\|json.loads" src/rolemesh/ipc/`
- pinned test：`tests/test_ipc_forward_compat_ignores_unknown_fields.py`
  - 用每个 dataclass：构造一个带"未来字段" `{"future_field": "xxx", ...known_fields}` 的 JSON，断言 `from_bytes` 不抛 + unknown 字段被丢弃
  - 反向：缺必填字段时必须抛（防止 mixin 写错把缺失字段也吞了）
  - 不要 mock；用真 JSON bytes round-trip

**Acceptance**：
- 所有 `from_bytes` 走统一 mixin
- pinned test 覆盖每个 dataclass 的 forward-compat + missing-required 两个分支
- 现有 ipc 测试不退化

### PR 3 — Container orphan cleanup image whitelist + INV-3 pinned test

**Background**：当前 `cleanup_orphans` 走 name prefix + suffix 黑名单（`-postgres-`/`-nats-`/`-redis-`），不能防止"用户起的 kindest/node 或其它合规 cluster 被误删"。

- 改 `src/rolemesh/container/docker_runtime.py:250` 的 `cleanup_orphans`：
  - 接受 `allowed_images: frozenset[str]` 参数（由调用方传入）
  - 列出容器后，**只删 image 在白名单内**且 name 匹配 prefix 的容器
  - 删除路径：先 inspect `c._container["Image"]` 拿 image ref，比对 `allowed_images`
  - 兼容 image tag 带或不带 registry：normalize 一下（strip `docker.io/library/`）
- 找到所有调用方 (`grep -rn "cleanup_orphans" src/`)，把 RoleMesh 自家 image 列表传进去（典型：agent-runner image + ipc-bridge image）
- 删除原 `_infra_suffixes` 黑名单逻辑（黑名单已被白名单替代，不要并存）
- pinned test：`tests/test_container_cleanup_image_whitelist.py`
  - 不 mock docker SDK；用 `aiodocker` mock library（已用过的） OR `unittest.mock` mock 一层
  - 测三个场景：
    1. 容器 image 在白名单 + name 匹配 prefix → 删除
    2. 容器 image **不在**白名单 + name 匹配 prefix（模拟用户的 kindest/node）→ **不删**
    3. 容器 image 在白名单 + name 不匹配 prefix → 不删
  - 用变异思维：把白名单匹配条件取反，测试应该红
- 文档：在 `docs/14-container-hardening-architecture.md` 补一段说明 image whitelist 策略（短，1-2 段）

**Acceptance**：
- `cleanup_orphans` 签名变更，所有调用方更新
- pinned test 通过
- 手动 smoke（在 session 末尾跑一次，记 Findings）：
  ```bash
  docker run --rm --name foreign-not-rolemesh-test -d alpine sleep 600
  # 跑 cleanup_orphans("not-rolemesh-test", allowed_images={"agent-runner:latest"})
  docker ps | grep foreign-not-rolemesh-test  # should still exist
  docker stop foreign-not-rolemesh-test
  ```

### PR 4 — `_bootstrap_actor_user_id()` helper + INV-4 pinned test

**Background**：audit 表（`safety_rules_audit.actor_user_id`）写入时 user 可能是 bootstrap 字面量，不能直接做 FK。

- 新建 `src/rolemesh/auth/bootstrap_actor.py`：
  ```python
  class BootstrapActorError(Exception):
      code = "BOOTSTRAP_NEEDS_TENANT_OWNER"
      status = 503

  async def resolve_actor_user_id(
      tenant_id: str, current_user_id: str
  ) -> str:
      """Resolve a real user UUID for audit FK writes.

      If current_user_id is already a real UUID (not the bootstrap literal),
      return it. If it's the bootstrap literal, look up the tenant's first
      owner and return that UUID. If the tenant has no owner, raise
      BootstrapActorError -> 503.
      """
      ...
  ```
- 找出所有 audit 写入路径（`grep -rn "actor_user_id" src/`），改为统一过这个 helper
- 在 FastAPI 全局 exception handler 里把 `BootstrapActorError` 转 503 + 标准错误体：
  ```json
  {"code": "BOOTSTRAP_NEEDS_TENANT_OWNER", "message": "...", "details": {"tenant_id": "..."}}
  ```
- pinned test：`tests/test_audit_actor_resolution.py`
  - 真实数据库 fixture（用现有的 testcontainer 模式）
  - 测：
    1. 当 current_user_id 是真实 UUID → 返回原 UUID
    2. 当 current_user_id 是 `"bootstrap"` 且 tenant 有 owner → 返回 owner UUID
    3. 当 current_user_id 是 `"bootstrap"` 且 tenant 无 owner → 抛 `BootstrapActorError` + status=503
  - 反 mirror：不要先读 helper 实现再写测试；先列预期行为，确保第一个测试是失败的，再实现

**Acceptance**：
- 所有 audit 写入路径过 helper
- pinned test 三场景覆盖
- 触发场景 3 时的 HTTP 响应是 503 + 正确 error code

### PR 5 — `BOOTSTRAP_USERS` env multi-user map + upsert users

**Background**：设计 §5.2.1 方案 A。bootstrap fast-path 当前只产 `user_id="bootstrap"`，Phase 3 多 user 场景不够用。

- 改 `src/webui/auth.py:authenticate_ws()`：
  - 读 env `BOOTSTRAP_USERS`（JSON 数组），形如：
    ```json
    [{"token":"tok-alice","user_id":"alice","tenant":"default","role":"owner"},
     {"token":"tok-bob","user_id":"bob","tenant":"default","role":"member"}]
    ```
  - 单 token 兼容路径不动（向后兼容）
  - 多 user map 命中时：
    - 调用 helper `_ensure_bootstrap_user(spec)` — 首次见到时 INSERT users (id=spec.user_id, tenant_id=resolved, role=spec.role) ON CONFLICT DO NOTHING
    - 返回 AuthenticatedUser（user_id 用真实 UUID，不是字面量）
  - 校验 spec 合法性（每个 spec 必须有 token / user_id / tenant / role；role 在合法 enum 内）；非法则 startup 时 fail loud
- 启动时若 `AUTH_MODE` 不是 `external` 且 `BOOTSTRAP_USERS` 存在，warn-log（设计 §5.2.1 约束）
- **`user_id` 字段类型考虑**：当前 `users.id` 是 UUID。要么 spec 提供 UUID 字符串，要么按 `user_id` 字符串生成稳定 UUID（用 `uuid5(NAMESPACE_URL, "bootstrap:" + spec.user_id)`）。推荐后者——避免用户手写 UUID。
- pinned test：`tests/test_bootstrap_multi_user.py`
  - 真实数据库
  - 测：
    1. 单 token (`ADMIN_BOOTSTRAP_TOKEN`) 路径不受影响（旧行为保持）
    2. 多 user map：tok-alice 命中 → user 落表 → 返回的 AuthenticatedUser.user_id 是 stable UUID
    3. 多 user map：tok-bob 命中 → 第二个 user 落表 → tok-alice 重复请求时不重复 INSERT（ON CONFLICT 验证）
    4. 非法 spec（缺字段）→ startup raise
    5. 不匹配的 token → fall through to provider（不 short-circuit）
- 更新 `src/rolemesh/auth/factory.py` 或 startup hook 把 `BOOTSTRAP_USERS` 解析做在 init 时（避免每次 auth 调用重新 parse）

**Acceptance**：
- 单 + 多 user 两路径并存
- 所有 spec 在启动时校验
- pinned test 5 个场景覆盖
- 手动测：`BOOTSTRAP_USERS='[{"token":"tok-a","user_id":"alice","tenant":"default","role":"owner"}]' python -m webui` → curl 带 `Authorization: Bearer tok-a` → 返回的 user 信息显示 alice + 真 UUID

### PR 6 — `core/backend_capabilities.py` + `GET /api/v1/backends`

**Background**：设计 §2.3。引入 backend × provider × family 兼容矩阵。

- 新建 `src/rolemesh/core/backend_capabilities.py`，按设计 §2.3 实现 `BackendCapability` / `CLAUDE_BACKEND` / `PI_BACKEND` / `ALL_BACKENDS` / `validate_combo()`
- 新建 `BackendCompatError`，含 `code="BACKEND_INCOMPAT"`、status=**400**（设计 §13 标 422，但我建议 400 — see plan critique，可在 session 内由 reviewer 决定。如果选 422 请在 Findings 注明）
- 新建 `/api/v1` router 骨架：
  - 在 `src/webui/main.py` 注册一个新的 APIRouter `prefix="/api/v1"`
  - 鉴权 dependency 复用 `webui/auth.py` 的现有机制
  - 第一个 endpoint：`GET /api/v1/backends`
    - 不需要鉴权（公开元数据）OR 用最低 tier（看 reviewer 偏好）
    - 返回所有 backends 描述 + 兼容矩阵
    - 加 `Cache-Control: max-age=3600` header
- pinned test：`tests/test_backend_capabilities.py`
  - 单测：`validate_combo("claude", "openai", "gpt")` → raise（不兼容）
  - 单测：`validate_combo("claude", "bedrock", "claude")` → OK（Bedrock 跑 Claude）
  - 单测：`validate_combo("pi", "openai", "gpt")` → OK
  - API 测：`GET /api/v1/backends` 返 200 + JSON schema 校验

**Acceptance**：
- backend 兼容矩阵是代码常量（不是 DB 表）
- API endpoint 工作
- 单测覆盖典型组合 + 错误组合

### PR 7 — Bootstrap smoke 脚本

**Background**：每 Phase 末尾 smoke 需要可重复跑的脚本。Phase 0 的 smoke 验证 INV-2/3/4/5 + multi-user bootstrap。

- 新建 `scripts/smoke_bootstrap.sh`（或 `.py` 视项目惯例）：
  - 起 docker compose（postgres + nats）
  - 用 `BOOTSTRAP_USERS='[...]'` 启动 webui
  - curl `GET /api/v1/backends` → 验返回 schema
  - curl 带 tok-alice → 验返回 alice 身份
  - curl 带 tok-bob → 验返回 bob 身份
  - 起一个 foreign 容器 → 跑 cleanup_orphans → 验 foreign 还在
  - 触发一次 audit write（用 bootstrap user 在没有 tenant owner 的场景）→ 验返 503 + 正确 code
- 输出 ✅ / ❌ 表格，最后 exit code 反映通过与否
- 脚本可在本机直接跑，无外网 LLM 依赖

**Acceptance**：
- 在干净 checkout 上跑通
- 列出所有 INV 验证步骤
- 失败时 exit 非零

## Acceptance criteria（session 级）

跑完全部 PR 后：

- [ ] `pytest tests/test_skill_manifest_constant.py tests/test_ipc_forward_compat_ignores_unknown_fields.py tests/test_container_cleanup_image_whitelist.py tests/test_audit_actor_resolution.py tests/test_bootstrap_multi_user.py tests/test_backend_capabilities.py` 全绿
- [ ] `bash scripts/smoke_bootstrap.sh` 全绿
- [ ] 全套现有测试不退化（`pytest`）
- [ ] `grep -rn '"SKILL.md"' src/rolemesh/ src/webui/ | grep -v "skills.py:" | grep -v docstring` 输出为空（PR1）
- [ ] `git diff main..feat/ui` 不含 `coworkers.tools`、`models`、`mcp_servers` 等表的 schema 变更（migration 留给 00b；本 session commit 都在 feat/ui 上累积）
- [ ] 更新 `docs/webui-backend-v1.1-plan.md` 状态表为 `done` + 日期

## Out of scope（明确不做）

- ❌ 任何 DB migration（新表、ALTER）—— 留 00b
- ❌ OpenAPI 文件 / TS codegen —— 留 00c
- ❌ `<rm-app-shell>` 前端抽离 —— 留 00c
- ❌ `coworker.tools` 双写 —— 留 02b
- ❌ Coworker CRUD / runs 表 —— 留 01a

## Open questions

已锁定（执行前 prompt 阶段已确认，不要再问用户）：

1. **`BACKEND_INCOMPAT` HTTP status** = **400 Bad Request**（理由：422 留给 schema validation；"组合不存在"语义上更像 invalid_request）
2. **`BOOTSTRAP_USERS` 中 user_id 字段** = **slug + uuid5**（spec 里写 `"alice"`，代码内部 `uuid5(NAMESPACE_URL, "bootstrap:" + spec.user_id)` 生成稳定 UUID）
3. **`SKILL_MD_FILENAME` 旧名** = **保留一个 PR cycle**（PR1 改名 + 旧名 alias 同 commit；后续 commit 内删 alias）

## Pitfalls

- **`from_dict_filter_unknown` 不能给缺失必填字段 silently 填 default**——否则 INV-2 测试的"缺必填抛错"分支会废
- `cleanup_orphans` image whitelist 不要写死成 hardcode 集合——签名传入，避免下游 session 增加 image 时改不到
- `BOOTSTRAP_USERS` 解析放在 startup hook 而不是 `authenticate_ws` 内——后者每个请求都跑 一次，浪费 + 不一致风险
- `_bootstrap_actor_user_id` helper 不要回退到 hardcoded fallback user——L6 强约束就是宁可 503 也不要假写

## Findings (after execution)

执行日期：2026-05-20。所有 7 个 PR 均以单 commit 形式累在 `feat/ui` 上。

### 与原 prompt 的偏差

1. **文档段未补**：PR3 prompt 要求在 `docs/14-container-hardening-architecture.md`
   追加一段 image whitelist 策略说明，但该文件不存在（docs/ 目录仅有
   `webui-backend-v1.1-*` 与 `egress/`；`docs_bk/safety/container-hardening.md`
   是历史备份）。和用户确认后选择**跳过文档段**，待 hardening 架构文档重新
   引入 docs/ 时一并补回。不影响 PR3 的代码 + 测试落地。

### 实际范围调整

* **PR1 SKILL.md 常量**：`src/pi/` 下的 `coding_agent/core/skills.py:304` 与
  `core/package_manager.py:221` 仍有 `"SKILL.md"` 字面量。这是 Pi 的内部
  skill loader，**保留不动**——acceptance criteria 限制范围在
  `src/rolemesh/ src/webui/`；且与 memory `[[pi-in-tree]]` 中"Pi 内部规约自治"
  原则一致。
* **PR3 cleanup_orphans 签名变更**：原 `exclude_infra` 黑名单参数与
  `_infra_suffixes` 一起删除（whitelist 已覆盖该意图，不与黑名单并存）。
  调用方仅一处 (`main._ensure_container_system_running`)，已同步更新；
  现有单测 `tests/container/test_docker_runtime.py::test_cleanup_orphans`
  也跟着改了。`tests/container/test_startup_order.py` 用 `AsyncMock`
  打 stub，未做 args 断言，不需要改。
* **PR4 范围**：文档列出的 safety create/update/delete 三处都在 audit
  写入路径上，`actor_user_id` 列已统一改成在 REST handler 边界 resolve 后
  再传入下游。如果将来下游也从 channel 路径直接被调用，那条路径的
  `user_id` 已是真实 UUID（无 bootstrap），无需再过 helper。
* **PR5 多 user fast-path**：在 `webui.authenticate_ws` 中把多 user 路径
  **排在** legacy `ADMIN_BOOTSTRAP_TOKEN` 之前；同时配置两者时多 user 拿到的
  身份更丰富，更符合 §5.2.1 的意图。Spec 引用不存在的 tenant slug 选择
  **fail-closed**（返回 None → 401），不伪造 tenant_id。

### 新发现的不变量 / 潜在风险

* **INV-2 mixin 与 `from dataclasses import MISSING` 的语义**：
  `from_dict_filter_unknown` 用 `spec.default is MISSING` + `spec.default_factory is MISSING`
  来判定 required；某些后续 dataclass 若引入 `field(default=None)`（None 是
  合法 default）需要小心——他们就是 optional，filter 会接受缺失字段。这与
  pre-refactor 的 `raw.get(...)` 行为一致，但和 `d["x"]` 严格写法不同。下游
  写新的 IPC dataclass 时要意识到：**有 default 就是 optional，没 default 就
  抛 KeyError**——不再有"我用 d.get 但语义上是 required"的灰色地带。
* **INV-3 image normalization**：只 strip 了 docker.io 系列前缀。如果未来
  agent runner 自带的 image 来自其它 registry（如 ghcr.io / 私有 harbor），
  whitelist 里要写完整的 ref。同样的 normalization 对其它 registry **不生效**，
  这是有意的——避免把 `ghcr.io/owner/x` 与 `gcr.io/owner/x` 误判为同一个。
* **INV-4 bootstrap actor**：helper 选 "oldest owner by created_at"。如果
  租户中多个 owner 同毫秒插入（unlikely 但理论上可能），返回结果 deterministic
  程度依赖 PG 内部排序。下游 audit 分析可能出现 owner 名漂移；可考虑改成
  `ORDER BY created_at, id` 兜底——本 session 没改，记录在此。
* **PR5 in-process upsert cache**：`_upserted` set 是 process-local。多
  worker 部署时每个 worker 第一次见到 token 都会跑一次 `ON CONFLICT DO NOTHING`，
  不会写脏数据但会多一次 DB round-trip——可接受。

### 对下游 session 的影响

* **00b migration**：
  * INV-4 helper 已依赖 `users.created_at` 上的索引（`ORDER BY created_at ASC LIMIT 1`）。
    现有 schema 没有这个索引，目前数据量小不会成本爆炸；00b 可考虑追加
    `CREATE INDEX users_tenant_role_created ON users (tenant_id, role, created_at)`，
    作为 bootstrap fast-path 的优化（**非阻塞**）。
  * INV-4 helper 也假设 `users(id)` 在 `INSERT ... ON CONFLICT (id) DO NOTHING`
    下行为正确——schema 里已经是 PRIMARY KEY，但 00b 触碰 users 表时要
    保持这点不变。
  * PR5 用 `INSERT users(id, tenant_id, name, role)` 显式传 UUID。schema
    里 `id UUID PRIMARY KEY DEFAULT gen_random_uuid()` 允许显式赋值；00b
    如果迁移这个表（重命名列、加 NOT NULL 约束等），需要保留这个能力。
* **01a Coworkers CRUD**：`/api/v1` 路由已经骨架化（PR6），新 endpoint
  挂到 `webui.api_v1.router` 上即可，不要再去 `webui/main.py` 加。
* **01b WS 协议**：INV-2 mixin 是 ipc 层的强约束——新加 ws/protocol dataclass 时
  请走 `from_dict_filter_unknown` 而不是直接 `cls(**d)`，否则 pinned test 不会
  抓到（test 只覆盖现有 dataclass）。考虑把这一行 lint 化（grep `cls\(\*\*` 在
  `ipc/`）。

### 全套测试回归状态

* 总计 ~23,866 passing + 21 skipped + 3 xfailed across the tree（分三组并行跑）。
* **一个 pre-existing failure，不是本 session 引入**：
  `tests/test_agent_runner/test_amazon_bedrock_tool_limit.py::test_realistic_long_mcp_name_raises`
  在 stash 后切回 `feat/ui` 起始状态依然 fail（断言 `len(bad) > 64`，
  而字面量长度刚好是 64）——属于 b0043f5 (`fix(bedrock)`...) 引入的失误，
  应在独立的 chore PR 中修，不影响 Phase 0 acceptance。

### 后续 cleanup（不在本 session 范围）

* `SKILL_MD_FILENAME` 别名按约定保留一个 PR cycle，**下次触碰 core/skills.py
  时移除**。
* `BootstrapUserSpec.user_id_slug` 这个字段名是为了避免和 ``user_id`` (UUID)
  混淆，但 spec JSON 里仍写 `"user_id"`——长期一致性上略别扭，等 frontend 接入
  时统一改成 `"slug"` 更显式。
