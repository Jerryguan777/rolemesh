# RoleMesh — Web UI 设计说明

> **这是什么。** RoleMesh web UI 背后的设计思路:实体模型、创建 agent 的动线、以及信息架构的取舍。
> 目标是既能给人类设计师看,也能作为 Claude Code 实现时的参照。
>
> **两份 source of truth,各管各的。**
> - **数据以 OpenAPI 契约为准** —— 实体名、字段、枚举、端点。在 `feat/ui` 分支上,契约本体是
>   `contracts/openapi.yaml`(规范)+ `web/src/api/generated/types.ts`(生成的 TS 类型)+
>   `src/webui/schemas_v1.py`(Pydantic)。*(本分支上**没有**叫 `contract/` 的顶层目录——这三个文件
>   就是契约。若你本地路径不同,请指给我。)*
> - **样式与交互以 HTML 原型为准** —— 布局、流程、文案、那套 "studio" 视觉语言。
> - **两者冲突时,以契约为准。** §8 列出了原型里每一处做了简化或自创的地方,以及对应的更正;**§10 汇总了对照契约后浮现的待澄清项——先看 §10.1(实现前必须确认的 blocking 项)。**

---

## 1. 心智模型

RoleMesh 是一个面向 **coworker(AI agent)** 的平台。coworker 是**成品**;building blocks 组里的其它东西
都是装出它的**零件**。把"成品 vs 零件"这条线分清楚,是理解整个 IA 的钥匙。

```
                         ┌──────────────────────────┐
            credential ─▶│  PROVIDER (anthropic /    │
          (每个 provider │  bedrock / openai / google)│
             一把)        └────────────┬───────────────┘
                                      │ 拥有
                                      ▼
   engine (backend) ──门控──▶    MODEL (provider + family + model_id)
   claude · pi                        │
        │                             │ model_id(每个 coworker 一个)
        │ 门控 providers + families   ▼
        └───────────────────────▶  COWORKER  ◀──── 绑定的 skills(按 coworker、可启停)
                                      ▲   ▲
                       绑定的 MCP ────┘   └───── 受 safety rules 治理
                       servers(按 coworker、
                       enabled_tools 白名单)
```

### 实体(契约里的名字用 `code` 标注)

| UI 叫法 | 契约 | 是什么 |
|---|---|---|
| **Coworker** | `Coworker` / `CoworkerCreate` | agent 本体。有 `name`、`folder`(slug)、`agent_backend`(引擎)、`model_id?`、`system_prompt?`(即 instructions)、`status`(`active`/`paused`/`disabled`)、`agent_role`(`super_agent`/`agent`)、`max_concurrent`。 |
| **Engine(引擎)** | `agent_backend` → `BackendName` = `claude` \| `pi` | 运行该 coworker 的 runtime。`claude` = "Claude Agent SDK"(仅 Claude)。`pi` = "Pi"(任意 provider/family)。UI 里叫 **"Engine"**;**绝不要**把 `claude`/`pi` 这种原始值当作模型显示。 |
| **Backend 矩阵** | `Backend`(`GET /backends`) | 静态的 `engine × provider × family` 兼容矩阵。`supported_providers[]` + `supported_model_families[]`(`null` = 该 provider 提供的任意 family;目前只有 Pi 是 `null`)。**这是"引擎→模型"门控的数据源。** |
| **Provider** | `ModelProvider` = `anthropic` \| `bedrock` \| `openai` \| `google` | 谁来提供模型。也是**持有凭据的单位**。 |
| **Model family** | `ModelFamily` = `claude` \| `gpt` \| `gemini` \| `llama` | 门控用的粗粒度分组。 |
| **Model** | `Model` | 一个选项条目:`provider` + `model_family` + `model_id`(provider 侧标识,如 `claude-opus-4-7`)+ `display_name` + `is_active`。租户无关的目录。 |
| **Credential** | `CredentialResponse` / `CredentialUpsert` | **每个 provider 一把。** Upsert body 是 `api_key`(+ 可选 `extras`,如 `api_base`/`region`)。key 在服务端加密、**永不回传** —— GET 只返回 `provider` + 时间戳。这是**解锁某 provider 模型的根依赖**。 |
| **MCP server** | `MCPServer`(注册表)+ `CoworkerMCPBinding`(绑定) | 工具服务器。注册表是租户级(`/mcp-servers`);每个 coworker 通过 `/coworkers/{id}/mcp-servers` **绑定**其子集,带 `enabled_tools` 白名单(三态:`null`=全部、`[]`=全关、列表=允许清单)。 |
| **Skill** | `Skill` / `SkillSummary` + `CoworkerSkillBinding` | 一个"指令 + 文件"的包。租户级目录(`/skills`);按 coworker 绑定。一个 skill = `SKILL.md`(始终存在、受保护)+ 一个 `files` map:`path → SkillFile{content, mime_type}`。 |
| **Safety rule** | `SafetyRule` + `SafetyCheck` / `SafetyDecision` | 确定性护栏。平台拥有 **checks**(`/safety/checks`,只读目录);租户从中**组合 rules**(`check_id` + config + stage + 作用域 + verdict)。fail = block。决策会被记录。*(见 §10.2。)* |
| **Run** | `Run` | 一次 agent 执行(挂在某 conversation 上):`status`、`usage`、`error`、时间戳。 |

### 两道门控(model/provider/credential 关系的核心)

一个模型**对某 coworker 可用**,当且仅当**两道门都过**:

1. **引擎门** —— coworker 的引擎支持该模型的 `provider` **且** `model_family`。
   读自 `GET /backends`:`claude` 支持 `{anthropic, bedrock}` × family `claude`;
   `pi` 支持所有 provider,`supported_model_families = null`(任意)。
2. **凭据门** —— 该模型的 `provider` 已配置凭据。
   `GET /models` **租户无关、不携带凭据状态**,所以前端必须把
   **`GET /models`(按 provider 分组)和 `GET /tenant/credentials` 交叉**,才能算出 ready / 缺凭据。
   加凭据(`PUT /tenant/credentials/{provider}`)会让整个 provider 组从锁定→就绪,且是响应式的。

设计推论:**凭据是 provider 级的,所以它的入口出现在两处** —— Credentials 页 *和* Models 页/向导模型步的就地入口。
**一个动作(`PUT credential`)必须更新所有依赖它的画面。**

---

## 2. 信息架构与出发点

### 原则:*operate(操作)* vs *configure(配置)* vs *observe(观测)*

- **Operate** —— 和 coworker 对话。在带 coworker 切换器的 **chat 外壳**里。
- **Configure** —— 配置 coworker 及其零件。在一个统一的**管理外壳**里。
- **Observe** —— 运行记录、安全决策。在 **Activity**(只读)里。

每个治理对象都有一对**孪生**:一个**规则**(configure)和一个**日志**(observe)。例如:safety *rules* 在外壳里;safety
*decisions* 在 Activity。

### 统一的管理外壳("Settings"画面)

一个整页外壳,左侧导航,从**成品**起、向下到它的**零件**,再到治理,再到 workspace:

```
Coworkers                ← 成品(置顶、一等公民)
Building blocks          ← 零件
  · MCP servers
  · Skills
  · Models
  · Credentials
Governance
  · Safety rules
Workspace
  · General
  · Members
Account
  · Appearance
```

出发点:
- **Coworker 是主角,所以置顶、不被埋没。** 它由两个入口到达**同一个**画面:chat 切换器里的
  "Manage coworkers…",以及齿轮 / 用户菜单。多入口 → 易发现;一个 canonical 画面 → 不割裂。
- **"Building blocks" 是刻意的对照词。** 上面是 Coworkers(成品)、下面是 blocks(零件)——这个排序本身
  在教用户它们的关系。coworker **不是**一个 building block,所以绝不放进那个组里。
- **building blocks 的顺序就是依赖顺序**,自上而下:MCP servers · Skills · Models · Credentials。
  (实现顺序相反——见分片计划——因为数据流是 Credential → Models → MCP/Skills → Coworker。)
- **Activity 刻意**不放进外壳。它是 observe 模式(只读日志 + 实时数据)。它有自己的顶栏入口、更显眼;
  塞进配置导航反而会降级它、并把 operate/configure/observe 的分界搞糊。

### 顶栏(在 chat 外壳里)

两个图标动作 + 一个租户 pill:**Activity**(脉冲)→ 观测画面;**Settings**(齿轮)→ 管理外壳。
左下角的**用户栏**向上弹出菜单,含 **Settings** 和 **Log out**。

### 一致性规则

- Coworkers、MCP servers、Skills、Models、Credentials 这几页共用**一套页面模板**:页头(`<h2>` +
  一个 "New …" 动作)+ 卡片列表 + 悬停出现的**编辑 / 删除**。
- 简单零件(credential、MCP server、skill)的**创建是单步的** —— 一个聚焦的对话框浮在整页外壳之上
  (不会"浮层摞浮层",因为外壳是页面、不是浮动卡片)。只有 **coworker** 用多步向导,因为它要装配很多面。
  对话框沿用向导的视觉语言(控件、页头、底栏),但不用它的步骤条。

---

## 3. 创建 agent 的动线(向导)

`rm-coworker-wizard` —— 一个聚焦的 6 步居中面板,浮在 Coworkers 页之上。状态是一个跨步骤累积的草稿对象;
在点 **Create** 之前什么都不写。

| # | 步骤 | 读取 | 写入 / Create 时 |
|---|---|---|---|
| 1 | **Identity** | — | `name`;`folder`(slug,`CoworkerCreate` 必填);instructions → `system_prompt`。*(关于"role"字段见 §8。)* |
| 2 | **Engine** | `GET /backends` | `agent_backend` ∈ `{claude, pi}`。该选择会**重新过滤第 3 步**。 |
| 3 | **Model** | `GET /models?provider=&family=` ⨯ `GET /tenant/credentials` ⨯ backend 矩阵 | `model_id`。列表受引擎门控;选中一个 provider 缺凭据的模型时,显示**就地的"needs X credential — add it now"**,点开凭据对话框,成功后当场解锁该行。 |
| 4 | **Tools** | `GET /mcp-servers` | 一组 MCP server 绑定 → 每个 server `POST /coworkers/{id}/mcp-servers`(`enabled_tools` 白名单)。"+ Connect a new server" 就地打开 MCP 对话框。 |
| 5 | **Skills** | `GET /skills` | 一组 skill 绑定 → `POST /coworkers/{id}/skills/{skill_id}`。"+ New skill" 就地打开 skill 对话框。 |
| 6 | **Review** | 草稿 | `POST /coworkers`(identity + engine + model),然后是第 4、5 步的绑定调用。新 coworker 以 `draft`/`paused` 出现在 roster 里,激活后转 active。 |

> **Guardrails 不是向导的一步。** safety rules 是租户级的(创建后再按 coworker 限定),
> 放在 Governance —— 硬塞进创建流程会让一个多数人会跳过的步骤占据过重的位置。新 coworker 继承租户默认。

用户**感受到的**级联:选 Engine → 模型列表变化 → 若选中模型的 provider 没 key,就在那儿补上。两道门(引擎、凭据)
作为概念始终是隐形的;它们只表现为"列表变了"和"补个 key"。

---

## 4. 零件的创建 / 编辑流程

三个都是浮在整页外壳之上的单步对话框(`rm-*-dialog`)。

**Credential**(`PUT /tenant/credentials/{provider}`,删除用 `DELETE`)。字段:provider(从 Models 的
"Connect" 进来时预选好)、`api_key`。保存后:追加一张凭据卡片,**并且**把该 provider 在 Models 页的组翻成就绪。
编辑 = 重开同一对话框(重新输入 key)。

**MCP server**(`POST /mcp-servers`,`PATCH`/`DELETE /mcp-servers/{id}`)。字段:`name`、`type`
(`sse`/`http`)、`url`、`auth_mode`(`user`/`service`/`both`),以及 `credential_ref`(仅当 auth 需要时显示)。
可选:`extra_headers`、`tool_reversibility`、`description`。绑定到 coworker 在别处发生(向导 / coworker 的页),
不在这里。

**Skill**(`POST /skills`,随后逐文件操作)。对话框照真实结构来:一个 **`SKILL.md`** 编辑器(它的 frontmatter
会填充 `frontmatter_common`/`frontmatter_backend` 以及列表视图的 `description`)**外加一个 "Additional files"
列表** —— 每行一个 `path` → content。创建时把 `SkillCreate.files` 作为 `path → content` map 发出(`SKILL.md`
必填)。编辑时:元数据走 `PATCH /skills/{id}`,文件内容变更逐文件走 `PUT /skills/{id}/files/{path}`(删除用
`DELETE`)。**`SKILL.md` 不可删**(`409 SKILL_MANIFEST_PROTECTED`)—— UI 不应在它上面提供删除控件。

---

## 5. 画面 → 组件 → 端点 映射

`feat/ui` 上已经有一套真实的 Lit 前端(hash 路由、扁平 sidebar)。这次重设计是把这些画面**重新分组、重新换肤**;
下表里的组件大多已存在,是重构而非重写。

| 画面(重设计后) | 已有组件 | 主要端点 |
|---|---|---|
| Chat 外壳 + 切换器 | `rm-chat-panel`、`rm-message-*`、`rm-sidebar` | `/coworkers`、`/coworkers/{id}/conversations`、`/conversations/{id}/messages`、WS ticket 走 `/auth/ws-ticket` |
| Coworkers(roster + 向导) | `rm-coworkers-page`(+ 新增 `rm-coworker-wizard`) | `GET/POST /coworkers`、`GET/PATCH/DELETE /coworkers/{id}`、`/coworkers/{id}/mcp-servers` 与 `/coworkers/{id}/skills` 下的绑定 |
| MCP servers | `rm-mcp-servers-page`(+ `rm-mcp-server-dialog`) | `/mcp-servers`(注册表)CRUD |
| Skills | `rm-skills-page`、`rm-skill-detail-page`(+ `rm-skill-dialog`) | `/skills` CRUD、`/skills/{id}/files/{path}` 逐文件 |
| Models | `rm-models-page` | `GET /models` ⨯ `GET /tenant/credentials` ⨯ `GET /backends` |
| Credentials | `rm-credentials-page`(+ `rm-credential-dialog`) | `GET /tenant/credentials`、`PUT/DELETE /tenant/credentials/{provider}` |
| Governance · Safety rules | `rm-safety-rules-page` | `/safety/rules`(+ `/{id}/audit`)、`/safety/checks` |
| Activity · Runs | *(新增)* | `GET /runs/{id}`、`POST /runs/{id}/cancel`、run 事件走 WS |
| Activity · Safety decisions | `rm-safety-decisions-page` | `GET /safety/decisions`(+ `/{id}`) |

**要应用的 IA delta:** 当前 `web/src/router.ts` 是**扁平** sidebar(Chat、Coworkers、MCP servers、Models、
Skills、Credentials、Bindings、Safety —— 全是顶层)。重设计引入:(a) 带 coworker 切换器的 chat 外壳、
(b) Coworkers 置顶的分组管理外壳、(c) 作为独立 observe 画面的 Activity。路由可继续用
hash;把管理路由嵌进一个外壳下(`#/manage/coworkers`、`#/manage/mcp-servers`、…),`#/activity/*` 保持独立。

---

## 6. 原型省略、但实现里必须有的状态

每个列表和表单的 loading / empty / error;乐观更新 + 失败回滚;凭据→模型的响应式交叉(任何地方加了 key,
都要更新 Models + 向导模型步);每个请求带租户上下文;列表会增长处的分页(runs、decisions);
Activity runs 用**实时**数据(WS,不是轮询,沿用已有的 `web/src/ws/v1_client.ts`)。

---

## 7. 视觉语言(简要)

"Studio minimal":奶油色背景、陶土/terracotta 强调色、Fraunces(展示衬线)+ Hanken Grotesk(正文)、
CSS 自定义属性做 design tokens、支持暗色。tokens 定义在原型的 `:root` 上 —— 原样抄过去。**在 Lit 里用 CSS
自定义属性驱动主题(它们会穿透 shadow 边界);不要指望 Tailwind 工具类在 shadow root 内部生效**(已知的
v4 + Shadow DOM 摩擦)。每个画面用 Playwright 截图循环对着原型对应区域比对。

---

## 8. 原型 ↔ 契约 对照修正(以契约为准)

| 原型里显示的 | 契约里是 | 怎么做 |
|---|---|---|
| MCP transport `http` / **`stdio`** | `MCPType` = `sse` \| `http` | 用 **`sse`/`http`**;去掉 stdio。 |
| MCP auth `none` / `oauth` / `apikey` | `MCPAuthMode` = `user` \| `service` \| `both` | 用真实枚举;`service`/`both` 时显示 `credential_ref`。 |
| Coworker **"role"** = operations / logistics / finance / … | `agent_role` = `super_agent` \| `agent`;另有必填的 `folder`(slug) | 把 operations/logistics 这些标签当作**纯展示命名**(不是契约字段)。向导必须采集 **`folder`**(slug),可选地暴露 `agent_role`。把 "instructions" 映射到 `system_prompt`。 |
| Skill **"Description"** 作为独立字段 | `description` 来自 `SKILL.md` frontmatter(在 `SkillSummary` 上,不在 `Skill`/`SkillCreate`) | 要么在 `SKILL.md` frontmatter 里编辑它,要么保留一个 description 字段、保存时由 UI 折进 frontmatter。 |
| Skill 文件是自由文件名 | `files` 是 `path → SkillFile` map;`SKILL.md` **必填且受保护** | 预置 `SKILL.md`、禁止删除它、其余作为 `files` map 发送;编辑逐文件进行。 |
| Models 是一个扁平的 enabled/disabled 列表 | `Model` 带 `provider`+`family`;可用性是算出来的(引擎门 + 凭据门) | 按 provider 分组,用 `/backends` + `/tenant/credentials` 算 ready/锁定。 |
| "Engine" | `agent_backend` / `BackendName` = `claude` \| `pi` | "Engine" 是 UI 叫法;值是 `claude`(Claude Agent SDK)/ `pi`。 |

---

## 9. 待定决策

- **外壳命名** —— 一个叫 "Settings" 的面板以 "Coworkers" 起头略别扭。选项:把外层改名为 "Workspace"/"Manage",
  或保留 "Settings" 求熟悉(当前选择)。
- **Activity 是否进外壳?** —— 刻意保持独立(observe ≠ configure)。仅当某个 "Observability" 组确有必要时再议。
- **是否暴露 `agent_role`** —— `super_agent`/`agent` 是向导里给用户的选择项,还是推断得出。对照 A2A 编排设计确认
  (`docs/4-multi-tenant-architecture`、orchestration 模块)。

---

## 10. 对照契约后的实现澄清项

通读 `contracts/openapi.yaml` 后确认:设计在模型层面是一致的 —— 实体、credential → provider → model 的依赖链、
按 coworker 的绑定、以及治理对象的孪生关系都对得上。下面是浮现出来的缺口与待定项。**§10.1 是 blocking** ——
做对应分片之前必须先解决;§10.2 是契约比原型更丰富、需要定范围的决策;§10.3 是向导的一个心智模型说明。

### 10.1 Blocking —— 实现前必须确认

1. **`folder` 是 `CoworkerCreate` 的必填项**(slug,`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`),而向导从没采集它。
   在第 1 步加一个字段,或从 `name` 自动派生 slug。不解决就建不出 coworker。
2. **凭据不总是"只有一个 `api_key`"。** `CredentialUpsert` 带一个 open 的 `extras` 对象(`api_base`、
   `region` 等);credential proxy "读 provider 需要的任意形状"。**Bedrock 尤其**通常要 region / AWS 风格字段。
   凭据对话框必须**按 provider 区分字段**,而不是"仅 API key"。
3. **没有 list `GET /runs`** —— 只有 `GET /runs/{id}` 和 `POST /runs/{id}/cancel`。Activity → **Runs**
   标签页没有数据来源。需拍板:按 conversation 维度取 runs、把 Runs 限定到某会话、或后端补一个列表端点。
4. **发消息 / 起 run 走 WS,不是 REST。** `/conversations/{id}/messages` 只有 GET(读历史)。发送 = WS
   `request.run { input, idempotency_key }`;Stop = WS `request.cancel` 或 `POST /runs/{id}/cancel`。
   `system` / `safety` 帧**不进**持久化消息,只走 WS 事件流(`event.run.token` / `…completed` / `…error` /
   `…requires_reauth`)。chat 外壳必须**把 GET 历史和实时 WS 流合并渲染**,composer 的 send 要走 WS。

### 10.2 范围决策 —— 契约比原型更丰富

- **Safety 是分层的:平台 checks + 租户 rules。** `GET /safety/checks` 是只读的平台目录(每个 check 有
  `stages`、`cost_class`、`config_schema`)。`/safety/rules` 是租户级:一条 rule 选一个 `check_id` + `config`
  + `stage` + `priority` + 可选 `coworker_id` 作用域;verdict 有 `allow` / `block` / `redact` / `warn` /
  `require_approval`。**所以 Safety rules 页是一个基于 check 目录的、可编辑的规则编排页,而非只读的 "enforced"
  列表。**(这把"平台所有、不可改"的说法收紧到 *check* 这一层。)
- **MCP 绑定的粒度。** 契约支持按工具的 `enabled_tools` 白名单(三态)和按工具的 `tool_reversibility`。
  向导只整服务器绑定(`enabled_tools = null` = 全开)。需拍板:v1 是否只做整服务器、per-tool 后置。
- **`agent_role` / A2A 层级。** `agent_role` ∈ `{super_agent, agent}` 暗示有编排层级(super-agent 协调
  sub-agent)。扁平 roster 没表达它。需决定:UI 是否呈现这种层级、向导是否让用户选 `agent_role`。(扩展 §9。)
- **Channel。** `ChannelType` = `web` / `telegram` / `slack`(注意:**没有** "feishu" —— 那是原型里的一个 MCP
  server 名,没问题)。conversation 带 `channel_binding_id` / `channel_chat_id`,原型忽略了。v1 自动建好 `web`
  绑定;需决定会话列表是否体现 channel。
- **独立的 "Bindings" 路由。** `web/src/router.ts` 里有个 `Bindings`(coming-soon),但重设计把绑定收进了
  coworker 编辑里(和 `/coworkers/{id}/mcp-servers` + `/coworkers/{id}/skills` 一致)。需决定:废弃这个独立面,
  还是留作一个跨 coworker 的绑定矩阵。
- **`max_concurrent`**(默认 2)—— 在向导里暴露,还是静默用默认。

### 10.3 心智模型说明 —— 向导里的 provider 切换

在向导 Engine = Claude Agent SDK 的路径里,那个 **"Anthropic / AWS Bedrock" 切换不是 coworker 的字段** ——
它是**按 `provider` 过滤 `Model` 列表**。一个 `family = claude` 的模型在 `provider = anthropic` 和
`provider = bedrock` 下各是一条*独立的* `Model`。coworker 只存 `model_id`(它本身就带 provider)。实现时把这个
切换做成**列表筛选**,不要误建成 coworker 上一个独立的 provider 字段。
