# Session v2-B — Coworker wizard + Models provider grouping + Credential per-provider extras  `[REFRESHED 2026-05-23]`

| field | value |
|---|---|
| Phase | v2 cycle |
| Prerequisites | v2-A done（tokens / dialog / wizard primitive / chat shell / settings shell / icons.ts 全就位）+ 用户 smoke 验证 chat 不退化 |
| Estimated PRs | 3 |
| Estimated LOC | ~1400（v2-A 实证 LOC 是估算 2.85x；本 session 含 wizard 6 step + dialog + helper + 大量 tests）|
| Status | done 2026-05-23 (3 commits on feat/ui-v2) |

> **Refresh 起源**：v2-A 落地后 prompt 大改：
> 1. 把 `<rm-wizard>` / `<rm-dialog>` 实际 API 写进 prompt（v2-A 实现的 prop / event 名）—— 不再是 "primitive ready" 抽象描述
> 2. icons.ts 已有 8 个 SVG，本 session **不再造新 icon**
> 3. Models grouping helper v2-A **未抽**（plan locked 0 触碰业务）—— 本 session fresh write
> 4. v2-A 发现 `@keyframes` 不跨 shadow boundary —— 本 session 如用 animation 注意
> 5. 估算从 700 → 1400 LOC（v2-A 实证含 tests 后 2.85x，本 session 取保守 2x）
> 6. v2-A 用 `location.href` reload 切 coworker —— wizard Create 完成后 redirect to new coworker chat 同款（接受）

## Goal

v2 唯一有真新业务逻辑的 session：

1. **6 步 Coworker wizard** (`<rm-coworker-wizard>` 用 v2-A `<rm-wizard>` primitive)
   - `folder` **自动派生**（locked decision #4：name → kebab-slug，advanced 区可改）
2. **Models page provider grouping**：按 provider 分组 + 交叉 `GET /tenant/credentials` 算 ready/locked
3. **Credential per-provider extras** (`<rm-credential-dialog>` 用 v2-A `<rm-dialog>` primitive)：按 provider 动态字段（解 blocking #2，Bedrock region 等）
4. **Wizard 内联补 credential**：选中模型 provider 缺凭据时弹 credential dialog 就地补，成功后当场解锁该行

**v1.1 coworkers-page 不替换**——保留 list / edit / delete 路径；wizard 只接管 "+ New coworker" 按钮（设计 §3 "向导浮在 Coworkers 页之上"）。

## Required reading

1. [`docs/webui-ui-redesign-v2-design.md`](../webui-ui-redesign-v2-design.md) §3 wizard 6 步表 / §10.1 blocking #1 #2 / §10.3 provider 切换是 list filter / §4 dialog 写入路径
2. **v2-A Findings** —— wizard / dialog / icons 实际接口；@keyframes shadow boundary 注意；Models helper 没抽的理由
3. [`docs/webui-ui-redesign-v2-prototype.html`](../webui-ui-redesign-v2-prototype.html) —— wizard 6 步视觉（搜 `class="wiz"` / `.wsteps`）+ credential dialog（搜 `<dialog>` 第 650 行附近）
4. `web/src/components/wizard.ts` —— 实际 API（见下）
5. `web/src/components/dialog.ts` —— 实际 API（见下）
6. `web/src/components/icons.ts` —— 8 个可复用 SVG
7. `web/src/styles/tokens.css` —— `--rm-*` 变量清单（accent / warn / good / bad / subtle / ink-* / surface-* / border / font-body / font-display）
8. `src/webui/schemas_v1.py` —— `CoworkerCreate.folder` 必填 + 正则 `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`；`CredentialUpsert.extras: dict`（已支持任意 keys）
9. v1.1 现有 `<rm-coworkers-page>` (03b) / `<rm-models-page>` (02a) / `<rm-credentials-page>` (02a) —— 保留运行；wizard 只追加"+ New coworker" 按钮路径

## 概念定位

- Wizard 是浮在 Coworkers 页上的对话框形态，不是新路由 —— `#/manage/coworkers` 仍是 list；wizard 是 modal overlay
- Wizard draft state **完全在 `<rm-coworker-wizard>` 父组件内**——`<rm-wizard>` primitive 无内部 state（v2-A 落地，与 `<rm-inline-approval>` 同模式）
- Credential dialog 与 Wizard 是兄弟而非父子——wizard 触发 dialog 时**保持 wizard mounted**，dialog 关闭后 wizard 收到 refresh 信号重 fetch credentials
- Models page provider grouping helper 同时给 wizard step 3 + Models page 用——单一来源

## v2-A primitive 实际 API（写代码直接照抄）

### `<rm-wizard>` (`web/src/components/wizard.ts`)

```ts
export interface WizardStep {
  // 用户在 step rail 上看到的名字
  // ... 看实际 interface
}

// 用法示例
<rm-wizard
  title="New coworker"
  .steps=${[{label: 'Identity'}, {label: 'Engine'}, ...]}
  current-step=${this.currentStep}
  ?can-advance=${this.stepValid}
  submit-label="Create"
  ?busy=${this.creating}
  @step-change=${(e: CustomEvent) => this.currentStep = e.detail.step}
  @submit=${this.handleCreate}
  @close=${this.handleClose}
>
  ${this.renderCurrentStepBody()}
</rm-wizard>
```

### `<rm-dialog>` (`web/src/components/dialog.ts`)

```ts
export type DialogCloseReason = 'x' | 'backdrop' | 'esc' | 'programmatic';

<rm-dialog
  title="Add credential"
  ?open=${this.dialogOpen}
  ?close-on-backdrop=${true}
  ?close-on-esc=${true}
  width="480px"
  @close=${(e: CustomEvent<{reason: DialogCloseReason}>) => this.handleDialogClose(e)}
>
  <!-- slotted body -->
  ${this.renderCredentialForm()}
  <div slot="footer">
    <button @click=${this.save}>Save</button>
  </div>
</rm-dialog>
```

### `icons.ts` 8 个可用

`iconActivity / iconApprovals / iconSettings / iconChevronDown / iconPlus / iconSearch / iconClose / iconLogout`——**本 session 不要造新 icon**。如果 wizard 内某 step 真需要新 icon（如 model 卡片角标 backend logo），加进 icons.ts 让 v2-C 也能复用。

### tokens.css 关键 vars

- 颜色：`--rm-accent` / `--rm-accent-subtle` / `--rm-warn` / `--rm-warn-subtle`（"needs credential" 用）/ `--rm-good` / `--rm-good-subtle`
- 文本：`--rm-ink` / `--rm-ink-2` / `--rm-ink-3`
- 背景：`--rm-bg` / `--rm-surface` / `--rm-surface-2` / `--rm-surface-3`
- border：`--rm-border` / `--rm-border-2`
- 字体：`--rm-font-display` (Fraunces) / `--rm-font-body` (Hanken)

## Scope — PR breakdown

### PR 1 — `<rm-coworker-wizard>` 6 步框架 + Identity / Engine / Model / Review

**Goal**：wizard 主体上线；Tools / Skills 步先 placeholder（PR 2 真做）。

子任务：

1. **新建 `web/src/components/coworker-wizard.ts`**：
   - 用 `<rm-wizard>` primitive
   - `WizardStep[]`: `[Identity, Engine, Model, Tools, Skills, Review]`
   - 父组件 draft state: `{name, folderOverride?, instructions, agentBackend, modelId, mcpBindings, skillBindings}`
   - `canAdvance` 计算：每 step 各自 valid 函数
   - `handleSubmit`：先 POST `/api/v1/coworkers` → 拿 new coworker id → 然后顺序 POST `/coworkers/{id}/mcp-servers` + POST `/coworkers/{id}/skills` → 完成 close + `location.href = #/?coworker=<id>` (复用 v2-A reload pattern 切到新 coworker chat)

2. **Step 1 Identity**：
   - `<input name>` 实时计算 `derivedSlug = slugify(name)` 显示在下方灰字 "Slug: marketing-helper"
   - Advanced section（默认折叠）：`<input>` 让用户改 slug；改后 `folderOverride` 接管显示
   - `<textarea instructions>` → `system_prompt`
   - slugify 函数：lowercase + 替换非 `[a-z0-9-_]` 成 `-` + 收窄连续 `-` + 验 `^[a-z0-9][a-z0-9_-]{0,63}$`
   - `canAdvance` = name 非空 + slug 通过正则

3. **Step 2 Engine**：
   - `GET /api/v1/backends` 拿 `Backend[]`
   - 渲染 2 张卡片：Claude Agent SDK / Pi
   - 每张卡片显示 `supported_providers[]` + `supported_model_families` (null = "any family")
   - 选中 → `draft.agentBackend = backend.name`
   - `canAdvance` = backend 已选

4. **Step 3 Model**：
   - `GET /api/v1/models` + `GET /api/v1/tenant/credentials` 并发；以及 backend 兼容矩阵从 step 2 backend
   - 用 PR 3 抽的 `groupModelsByProvider(models, credentials, backend)` helper 算出 `{provider, models[], hasCredential}[]`
   - 渲染按 provider 分组的列表：
     - 每组 header 显示 provider 名 + 状态徽章（`hasCredential` true = 隐藏；false = "needs X credential" 用 `--rm-warn-subtle` 背景）
     - 状态为缺凭据的组下方加 `<button>+ Add credential</button>`
     - 每行模型 click → `draft.modelId = model.model_id`；引擎不兼容的模型禁用 + tooltip "Not supported by Claude Agent SDK" 之类
   - "+ Add credential" click → 弹 `<rm-credential-dialog provider=...>`（PR 2 落）；dialog 成功关闭 → wizard 重 fetch credentials → 当前行解锁
   - `canAdvance` = modelId 已选 + 该 model 的 provider 在 credentials 内 + 通过引擎兼容矩阵

5. **Step 4 Tools / Step 5 Skills**（PR 1 占位 + PR 2 真做）：
   - 占位 body 写 "(coming in PR 2)" + 允许 advance (canAdvance=true)

6. **Step 6 Review**：
   - 渲染 draft 摘要：name / slug / engine / model display_name / # tools / # skills
   - "Create" button → wizard primitive 的 `@submit` 触发

7. **接入 Coworkers page**：
   - `<rm-coworkers-page>` 顶部 "+ New coworker" button click → `<rm-coworker-wizard>` 显示
   - v1.1 现有 coworkers-page list / edit / delete 不动；只追加 wizard 触发路径
   - wizard close 后 list refresh（重 fetch coworkers）

**Pinned tests** (`web/src/components/coworker-wizard.test.ts`)：

- step rail 6 项渲染
- Identity slug 自动派生 + advanced override
- Engine 选择驱动 step 3 model 过滤
- Model step canAdvance 严格（必须 model + credential 双 OK）
- folder 正则违规（如 "Foo Bar"）→ slug 派生成 "foo-bar" 通过 + advanced override 输入非法 → canAdvance=false
- Submit 调用 POST /coworkers + 拿 id + 顺序绑定（mock fetch）

### PR 2 — `<rm-credential-dialog>` per-provider + Tools / Skills 两步

**Goal**：credential dialog 按 provider 动态字段；wizard step 4/5 真做。

子任务：

1. **新建 `web/src/components/credential-dialog.ts`**：
   - 用 `<rm-dialog>` primitive
   - prop: `provider: ProviderName | null` (null 时显示 provider 选择 select；非 null 时锁定该 provider)
   - 按 provider hardcode 字段 schema（前端 map；后端 `extras: dict` 已支持任意 keys）：
     - `anthropic`: api_key
     - `openai`: api_key + (optional) api_base
     - `google`: api_key
     - `bedrock`: aws_access_key_id + aws_secret_access_key + region (default `us-west-2`) + (optional) aws_session_token
   - Save → `PUT /api/v1/tenant/credentials/{provider}` body `{api_key, extras: {...}}`
   - Success → `@credential-saved` event 给父；dialog close
   - sanitize log（与 v1.1 02a credential pitfall 同款）：dev console / log 永不打印 api_key / secret 字段值

2. **Wizard Step 4 Tools**：
   - `GET /api/v1/mcp-servers` 拿 server list
   - Multi-select checkbox 列表
   - "+ Connect a new server" 按钮 → 弹 `<rm-mcp-server-dialog>`（如果 v1.1 已有 inline create dialog 用之；否则本 session 新建简化版仅 name / type / url / auth_mode）
   - draft.mcpBindings = `{server_id, enabled_tools: null}[]`（locked decision #9：整服务器绑定，`enabled_tools=null`）

3. **Wizard Step 5 Skills**：
   - `GET /api/v1/skills` 拿 skill list
   - Multi-select
   - "+ New skill" 按钮 → 弹 v1.1 `<rm-skill-dialog>`（03b 落地）；如果没有就 placeholder + 提示去 settings/skills 创建
   - draft.skillBindings = `{skill_id}[]`

4. **接入 Models page**：
   - `<rm-models-page>` 顶部加 "+ Add credential" button（per provider 缺凭据时 highlight）→ 弹 `<rm-credential-dialog>`
   - 该 page 既复用 PR 3 抽的 helper 算 ready/locked

**Pinned tests**：

- credential-dialog: anthropic 显示 api_key 字段；bedrock 显示 4 字段
- Save 调 PUT 用正确 body shape (`{api_key, extras}`)
- `@credential-saved` event 触发后 wizard model step refresh

### PR 3 — `groupModelsByProvider()` helper 提取

**Goal**：单一来源，wizard step 3 + Models page 共用。

子任务：

1. **新建 `web/src/services/models-grouping.ts`**：
   ```ts
   export interface ProviderGroup {
     provider: ProviderName;
     hasCredential: boolean;
     credentialUpdatedAt: string | null;
     models: Model[];
   }
   export function groupModelsByProvider(
     models: Model[],
     credentials: CredentialResponse[],
     backend?: Backend,  // optional 引擎兼容矩阵；不传则不过滤
   ): ProviderGroup[]
   ```

2. **`<rm-models-page>` (v1.1 02a) 改用 helper**：
   - 这是**唯一允许触碰 v1.1 业务组件**的地方（plan locked decision #2 也认这是"取代旧 grouping 逻辑"，不是顺手重构）
   - 改前 grep 确认 v1.1 没有其它消费者把 model 列表当扁平看
   - 改后跑 v1.1 models page 的所有现有测试不退化

3. **pinned tests**：
   - 3 个 provider × 不同 credentials 配置组合：全配 / 部分配 / 全没配
   - backend 过滤：Claude backend 只返 `family=claude` 的 model
   - 排序：provider 名字 alphabetical；组内按 `model_id` alphabetical

## Acceptance criteria

- [ ] `<rm-coworker-wizard>` 6 步全跑通；Create 成功后 redirect 到新 coworker chat（`location.href = #/?coworker=<new-id>`）
- [ ] `folder` slug 自动派生 + 正则校验 + advanced override（locked decision #4）
- [ ] Bedrock 凭据 dialog 显示 region 字段 + AWS keys（解 blocking #2）
- [ ] 内联补 credential：wizard step 3 缺凭据 → click "+ Add credential" → dialog → save → 当前 provider 组解锁（refresh）
- [ ] Models page 用同款 grouping helper 显示按 provider 分组 + ready/locked 状态
- [ ] v1.1 `<rm-coworkers-page>` list / edit / delete 不退化（只追加 wizard 路径）
- [ ] v1.1 现有 models / credentials page 单测不退化
- [ ] vitest + build + lint:no-admin-chat + lint:flat-route + openapi check 全绿
- [ ] 手动 smoke：起 dev → "+ New coworker" → 走完 6 步 → 新 coworker 在 list 出现 + 跳进 chat 可发消息
- [ ] 更新 plan.md 状态

## Out of scope

- ❌ **Activity shell / Approvals popover**（v2-C；v2-A 已落 activity-shell 最小占位）
- ❌ **MCP per-tool binding UI**（locked decision #9；wizard step 4 整服务器绑定即可）
- ❌ **`agent_role` / `max_concurrent` 暴露**（locked decision #10/13）
- ❌ **Wizard 编辑现有 coworker**（v3；本 session 只 create flow）
- ❌ **Coworker 详情 sub-tab**（v3；list / edit 在现有 v1.1 page 内即可）
- ❌ **重写 v1.1 chat-panel / app-shell / 其它业务组件**（v2-A locked）
- ❌ **fix coworker switch 的 location.href reload trade-off**（v2-A Findings 提的；v3 单独 chore）
- ❌ **Settings page 内卡片双层套**（v2-A Findings 提的；v2-C polish）
- ❌ **Approval policy 编辑器** / **Safety rules 完整编辑** (locked decision #7 #8)
- ❌ **新依赖引入**（form lib / animation / editor）

## Open questions

锁定（refresh 时定）：

1. **Wizard 错误反馈** = inline 红字 per field（不是顶部 banner）—— 用户能即时看到哪个字段错
2. **"needs X credential" → dialog → 解锁过渡** = 无动画；dialog 关闭后立即重 fetch credentials 并 re-render 该组；视觉切换是 instant（避免 v2-A `@keyframes` shadow boundary 的复杂性）
3. **credential dialog 关闭返 wizard** = wizard mounted 不动；dialog 是兄弟组件 by sibling event；wizard 收 `@credential-saved` 后重 fetch

仍需 session 内决策：

1. **slugify 算法的 edge case**：连字符开头（如 name "-foo"）、纯数字（"123"）、纯空格 → 用户友好 fallback vs 强制让用户改？推荐前者：name "-foo" → slug "foo"；纯数字 "123" → 不变（regex 允许）；纯空格 → empty → canAdvance false 提示"name required"
2. **MCP server inline dialog 用 v1.1 现有 vs 新建简化版**：先 grep `<rm-mcp-server-dialog>` 看 v1.1 是否有 modal 形式；如果只有 inline form 没 dialog 包装，新建简化版（仅 name / type / url / auth_mode 必要字段）
3. **Models grouping helper 是否同时挪 backend 兼容矩阵过滤逻辑**：推荐是——一处算完 ready/locked **加** 引擎兼容，避免 wizard step 3 还要自己过滤一次

## Pitfalls

- **`<rm-wizard>` primitive 无内部 state** —— draft / canAdvance / busy 全由父 `<rm-coworker-wizard>` 控；不要试图给 primitive 加内部 step state
- **Wizard 与 Dialog 同时打开**（step 3 内联补 credential）—— 这是 nested modal，但用原生 `<dialog>` 浏览器自动处理 stacking；不要自己实现 z-index 管理
- **Submit 顺序很重要**：POST /coworkers 先；拿到 new id 后再 POST /coworkers/{id}/mcp-servers + /coworkers/{id}/skills。任一绑定失败 wizard 不要回滚 coworker（用户能在 list 看到部分配好的 coworker，比 silently 失败友好）；失败时显示 banner + 给"Try bindings again later" 路径
- **`folder` 自动派生在 PATCH 上不适用** —— wizard 只用于 create；edit 走 v1.1 现有 coworkers-page 表单（其中 folder 字段不在 v1 PATCH endpoint 暴露——刻意 immutable）
- **凭据保存 sanitize log** —— `logger.info` 不打 body；与 v1.1 02a 同款做法
- **GET response 永不含明文 api_key** —— credential dialog 编辑现有凭据时，**字段必须空白**，提示"Set new value"；不要回填 last4 之类（v1.1 02a INV-VAULT-3 钉死）
- **icons.ts 不要复制 SVG inline** —— 用 `iconPlus()` 等 factory
- **不要给 wizard primitive 加新 prop** —— primitive 锁定（v2-A 测试钉了 API）；business state 在父
- **@keyframes 不跨 shadow boundary**（v2-A 发现）—— 如果 wizard step 内任何动画，要么在父 wizard 的 styles 里声明，要么在每个 dialog 内重复声明；推荐少用 animation，instant 切换更稳

## 执行前刷新清单

- [ ] v2-A 完成 + 用户手动 smoke 通过？
- [ ] 现有 v1.1 coworkers-page 的"+ New coworker" button 当前调什么（grep 一下 caller）—— 决定 wizard 接入点的具体改动
- [ ] v1.1 是否已有 `<rm-mcp-server-dialog>` 还是只有 inline form（决定 PR 2 是否新建简化版）
- [ ] v1.1 `<rm-skill-dialog>` (03b 落) 实际 API（决定 wizard step 5 怎么调用）

## Findings (after execution)

执行日期：2026-05-23（feat/ui-v2 上 3 commits：PR3 `4dec191` → PR1 `d0c320c` → PR2 `496e4fc`）。

### LOC 实际 vs 估算

| | LOC | 占比 |
|---|---:|---:|
| 业务代码 | ~1760 | 65% |
| 测试 | ~999 | 36% |
| **总计 added** | **2727** | — |
| 估算 | 1400 | — |
| 实际 ÷ 估算 | **1.95x** | — |

与 v2-A 的 2.85x 相当；session 估算系数收敛到 2x 比较稳。测试占比 36% 偏高但合理——本 session 真新逻辑（slugify / 6-step gating / partial-commit / per-provider extras）值得 anti-mirror 的契约钉子。

### Decisions taken in-session

1. **slugify 算法**：lowercase → 非 `[a-z0-9_-]` 替换成 `-` → 收 `-+` → 去掉开头非 alphanumeric → 去掉末尾 `-` → truncate 到 64。
   - `"Marketing Helper"` → `"marketing-helper"` ✓
   - `"-foo"` → `"foo"`（backend regex 拒绝首位 `-`）
   - `"123"` → `"123"`（regex 允许首位数字）
   - `"   "` → `""`（让 canAdvance 拒绝 + 提示 name required，不再报错）
   - 长度 > 64 → 直接 truncate（不报错；advanced override 给用户精细控）
2. **`groupModelsByProvider` 签名**：`(models, credentials, backend?)` → `ProviderGroup[]`。`backend` 可选；传时同时过滤 `supported_providers` + `supported_model_families`（null = any family）。这两个过滤在 helper 里一起做（locked 决策 #3），避免 wizard step 3 还要自己再过滤一次。空 group 会 drop 掉，即使该 provider 有 credential——避免 UI 出现"有 credential 但没模型可选"的空卡。
3. **credential dialog per-provider 字段最终 schema**：
   - `anthropic`: `api_key` only
   - `openai`: `api_key` + 可选 `extras.api_base`
   - `google`: `api_key` only
   - `bedrock`: `api_key` 当作 `aws_access_key_id` + 必填 `extras.aws_secret_access_key` + 必填 `extras.region`（default `us-west-2`）+ 可选 `extras.aws_session_token`
   - body shape：`{ api_key, extras: {...} | null }`（无 extras 时 explicit null，避免后端误读空对象）
4. **失败回滚 vs partial commit**：locked 决策已锁定 partial commit。submit 路径：
   - `POST /coworkers` 失败 → 整体 abort，原地报错，wizard 不关闭
   - 任一 mcp / skill 绑定失败 → 不回滚 coworker，banner 列出失败 id，wizard 不关闭，用户可选择关闭或继续操作。失败 id 在 v3 重试路径里能定位。
5. **MCP server inline dialog**：v1.1 `<rm-mcp-servers-page>` 只有 inline form panel（非 dialog），所以本 session **新建** `<rm-mcp-server-dialog>` 简化版（仅 name / type / url / auth_mode；extra_headers / tool_reversibility 留给老 page 完整编辑）。
6. **Skill inline create**：v1.1 `<rm-skills-page>` 同样是 inline form，不是 dialog；本 session **不**新建 skill dialog，Step 5 空状态文案引导去 settings/skills 创建（保持 v1.1 边界）。

### Architecture / impl notes

- **Lit 布尔属性绑定陷阱**：v2-A `<rm-wizard>` 的 `@property({type: Boolean, attribute: 'can-advance'})` **default = true**。父用 `?can-advance=${false}` 只移除属性但不触发 property 写回，primitive 仍读到 true。修复：父全部改用 property binding（`.canAdvance=${...}` / `.busy=${...}`），跳过 attribute 路径。**v2-C 写 component 时一律默认 property binding for booleans。**
- **Sibling dialog 容器位置**：credential-dialog + mcp-server-dialog 必须是 wizard 的 sibling（不是 child），原生 `<dialog>` 的 top-layer 自动 stack 在 wizard overlay 之上。由 `<rm-coworkers-page>` host 它们；wizard 通过 `request-credential` / `request-add-mcp-server` 事件冒泡通知。
- **catalogue lazy load**：wizard 在 `open=true` 第一次时并发 fetch `getBackends / listModels / listCredentials / listMCPServers / listSkills`。credentials / mcp / skills 失败 graceful degrade（空数组），不阻塞 wizard 渲染。
- **public refresh API**：wizard 暴露 `refreshCredentials()` / `refreshMCPServers()` / `refreshSkills()` 给 sibling dialog 触发的 host 调用。这是 locked #3 的实现细节——dialog 关闭后 host 显式调用 wizard.refreshX()，不靠 wizard 自己监听全局事件。
- **`getApiClient()` 上的新 client 方法**：`createCoworker` + `bindCoworkerMCPServer`。v1.1 client.ts 之前只 expose 到 v1.1 surface，没有创建 / MCP 绑定方法——v2-B wizard submit 必须的 4 个 method（create / bind mcp / enable skill / put credential）现在三个齐了；`enableCoworkerSkill` 已存在。
- **`groupModelsByProvider` 兼容 backend 缺省**：传 `null`/`undefined` 不做 provider/family 过滤——Models page 复用这个分支，wizard 总是传 backend。

### 对 v2-C 的影响

- **icons.ts 没增加新 icon**——保持 8 个不变（spec 要求 unless really cross-session 复用）。v2-C 如果需要 activity / approvals popover 的新 icon，独立追加。
- **`<rm-coworker-wizard>` 公开的 refresh API + sibling event 模式**：v2-C 的 activity / approvals popover 如果要类似 inline 补 credential 模式（unlikely），可以照抄这套 host-mediated event + refresh 方法。
- **v1.1 `<rm-coworkers-page>` 已加 wizard host**：list/edit/delete 路径 0 退化，但 v2-C 触碰 coworkers-page 时要小心 wizard / credential-dialog / mcp-server-dialog 三个 sibling 都挂着，state graph 比之前复杂。
- **partial-commit banner UX**：本 session 把 banner 留在 review step 内；v2-C 可以考虑挪到 wizard 的固定 banner 区。**当前实现已经足够工作但不优雅。**
- **`location.href` redirect**：wizard create 成功后还是用 `location.href` reload（沿用 v2-A），不动；v3 chore 真修。

### Acceptance criteria 状态

- ✅ 6-step wizard 全跑通；happy-path submit 测试钉了 POST /coworkers body 形状 + `location.href` 包含 `agent_id=<new-id>`
- ✅ folder slug 自动派生 + 正则校验 + advanced override（3 个 slugify 单元测试 + canAdvance 守门测试）
- ✅ Bedrock 凭据 dialog 显示 4 字段（access key id / secret / region / session token）+ region 默认 us-west-2
- ✅ 内联补 credential：wizard 派 `request-credential` 事件 → coworkers-page host credential-dialog → `credential-saved` 事件 → host 调 `wizard.refreshCredentials()` → 该组解锁。无 animation（locked #2）
- ✅ Models page 用 grouping helper + ready/locked badge + 每组缺凭据时 inline "+ Add" link
- ✅ v1.1 `<rm-coworkers-page>` list/edit/delete 不退化（只追加 wizard 触发路径；测试 153 → 160 → 全绿无回归）
- ✅ v1.1 models 现有测试不退化（v1.1 没有 models-page 单测；settings-shell 钉了 `rm-models-page` 还在 → 通过）
- ✅ vitest 160 全绿 / build 绿 / lint:no-admin-chat 绿 / lint:flat-route 绿 / openapi:check 绿
- ⚠️ 手动 smoke：单元测试侧 6-step 已 e2e 走过（happy + partial-commit failure modes 都钉了）；浏览器 smoke 留给用户确认（CLAUDE.md "UI / frontend changes" 要求；本 session 没起 dev server）
