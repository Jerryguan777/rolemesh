# Skills 架构

本文档解释 RoleMesh 如何支持用户自定义的 Skills——可复用的工作流定义，coworker 在任务匹配时自主调用——并在两个 agent backend（Claude SDK 与 Pi）之间保持统一，以 PostgreSQL 作为唯一权威来源，并在每次容器启动时投射到容器内。

目标是记录这套设计形态背后的*原因*：哪些备选方案被拒绝、这套设计抹平了两个 backend 之间哪些不对称，以及它有意保留了哪些扩展点。

目标读者：增加新 skill backend、新增 skill 管理 REST 接口的开发者，以及在调试"为什么 coworker 运行时没有发现已启用的 skill"的人。

---

## 背景：coworker 的第四条配置轴

一个 RoleMesh coworker 沿四条正交配置轴定义：

1. **System prompt** —— 身份、角色、默认行为。
2. **Tools** —— coworker 可触达的 MCP servers。
3. **Permissions** —— coworker 被允许做什么（数据范围、调度、委派）。
4. **Skills** —— agent 在任务匹配时自主调用的可复用工作流定义。

前三项已经落在 `coworkers` 表里，通过 `/api/admin/agents` 流转。Skills 是第四条轴，也是在本设计之前唯一没有一等公民支持的一条。

两个 backend 都已经原生理解同一种 skill 形态：

- **Claude Agent SDK** 从 `~/.claude/skills/<name>/SKILL.md` 加载 skill（同目录可选放支持文件）。模型读 frontmatter 的 `description` 字段决定何时调用。RoleMesh 已经传入 `setting_sources=["project","user"]` 并把 `"Skill"` 加进 `allowed_tools`，SDK 侧已经接通。
- **Pi** 通过 `pi.coding_agent.core.skills` 从 `~/.pi/agent/skills/<name>/SKILL.md` 加载。触发语义完全一致：模型读 `description` 后自主调用。Pi 私有的 `disable-model-invocation: true` 可以关掉某个 skill。

两边的触发模型一致：**没有 slash command，没有终端，没有人手敲 `/skill-name`**。RoleMesh 容器里没有交互终端——skill 必须完全靠 frontmatter `description` 驱动的自主模型调用工作。这一点决定了整个设计的形状。

---

## 设计目标

1. **单一权威来源** —— skill 在且仅在一个地方存在（PostgreSQL）。不做文件系统复刻，不做"DB 存元数据、FS 存内容"的混合分裂。
2. **backend 透明** —— 同一行 skill 能正确物化为任一 backend 期望的目录结构。绝大多数 skill 写一次就能两端跑。
3. **严格租户隔离** —— skill 通过 RLS 做 tenant 隔离，附属于单个 coworker，v1 内不跨 coworker 共享。跨租户泄露在应用、RLS、trigger 三层阻断。
4. **运行时只读** —— 容器内 agent 无法改动 skill 文件。Skill 是配置，不是工作空间。
5. **新 skill 不需要重启** —— 每次容器启动时重新物化，因此下一次对话即可拾取改动，无需重启 orchestrator。
6. **不让 `pg.py` god-module 变得更糟（超过它原本就有的程度）** —— schema 与 CRUD 同其他 12 个 domain 一起落进 `pg.py`；将来的重构按 domain 整体拆分。
7. **v1 仅 REST 编辑入口** —— 没有 CLI、没有 WebUI、没有 IDE 集成。未来的 CLI（`rolemesh skill pull/push`）是已规划的演进路径。

---

## 什么是 Skill

一个 skill 就是一个文件夹。具体来说，它由两部分组成：

- **一份位于根目录的 `SKILL.md`**，作为入口。frontmatter 里的 `description` 是模型用来决定何时调用的字段。正文解释工作流。
- **零或多份支持文件** —— 参考文档、示例、脚本、模板等 —— `SKILL.md` 可以引用它们。模型在工作流需要时按需读取。

```
code-review/
├── SKILL.md            ← 入口；frontmatter 驱动调用
├── reference.md        ← 详细指南，按需加载
├── examples.md
└── scripts/
    └── helper.py
```

这种文件夹形态是 Claude 官方推荐的 canonical 形式，Pi 端同样支持。平铺单文件形式两端都支持但已不主推；RoleMesh 在文件夹形态上统一。

---

## 存储决定：PostgreSQL 作为权威

整个设计中影响最大的决定是 **skill 内容存在哪里**。备选项是 PostgreSQL、宿主机文件系统、或混合（元数据进 DB，内容在 FS）。

**PostgreSQL 胜出**，按权重排序：

1. **多租户隔离已有现成方案** —— 通过 `current_tenant_id()` 实现的 RLS 是 `pg.py` 中已成熟的模式。基于 FS 的方案要从零造一套基于路径的隔离系统。
2. **多 host 部署需要共享存储** —— PostgreSQL 已经是共享服务。FS 需要 NFS、S3FS 或同步层。
3. **备份与 DR 是 free 的** —— 一次 `pg_dump` 覆盖全部。FS 需要并行的备份管线。
4. **与 RoleMesh 既有约定一致** —— prompt、tools、permissions 全在 DB。Skills 作为第四条轴与之匹配。
5. **审计 trail 自带** —— 标准的 `created_at` / `updated_at` / actor 字段足够。

**代价**：IDE 与 git 维度的 skill 内容编辑不方便——用户不能直接 `vim ~/.claude/skills/foo/SKILL.md`。这一点真实存在，但被以下两点缓解：

- v1 容器内并没有最终用户的终端，直接编辑从来就不是工作流。
- 未来的 `rolemesh skill pull/push` CLI 作为可选的同步层提供 IDE/git 工效，类比 `kubectl apply -f` 或 `aws ssm get-parameter`。数据模型对该 CLI 向前兼容；v1 不出货，但也不挡路。

**混合方案被明确拒绝**：DB 元数据 + FS 内容同时具备两边的复杂度，又拿不到任何一侧的"单一权威"保证。

---

## 数据模型

两张表，都按 tenant 划分，都启用 RLS：

```
┌──────────────────────────────────────────┐      ┌──────────────────────────────────────┐
│  skills                                  │      │  skill_files                         │
│                                          │ 1..n │                                      │
│  id              UUID                    │◀─────│  skill_id   UUID  FK                 │
│  tenant_id       UUID                    │      │  path       TEXT                     │
│  coworker_id     UUID  FK (CASCADE)      │      │  content    TEXT                     │
│  name            TEXT  (regex 校验)      │      │  mime_type  TEXT                     │
│  frontmatter_common    JSONB             │      │                                      │
│  frontmatter_backend   JSONB             │      │  PK (skill_id, path)                 │
│  enabled         BOOLEAN                 │      │  CHECK 拒绝绝对路径 / '..' / '\\'    │
│  created_at, updated_at, created_by      │      │                                      │
│  UNIQUE (coworker_id, name)              │      │                                      │
└──────────────────────────────────────────┘      └──────────────────────────────────────┘
```

几条关键约束值得点名，因为它们编码了系统其它部分依赖的不变量：

- `UNIQUE (coworker_id, name)` —— 单个 coworker 内 skill 名唯一。
- 应用层不变量：每个 skill 必须有且仅有一行 `skill_files` 满足 `path = 'SKILL.md'`。删除它返回 400。
- 跨租户防御通过 BEFORE-INSERT trigger 实现：`skills.coworker_id` 必须指向同租户的 coworker —— 在 SQL 层拦截，而不是依赖应用层。
- 路径穿越在写入时通过 `CHECK` 阻断，在投射时再通过对 skill 根目录的 `realpath` 校验一次。

schema 有意省略了 `scope` 列、`scope_id` 列以及 `coworker_skills` 关联表 —— v1 不支持跨 coworker 共享 skill。将来加共享是一次纯叠加迁移（一张新表），现有行零改动。

---

## 为什么 frontmatter 要拆成 `common` + `backend`

两个 backend 接受**不同的 frontmatter 字段集**：

| 字段                       | Claude SDK | Pi |
|----------------------------|:---:|:---:|
| `name`                     | ✅ | ✅ |
| `description`              | ✅ | ✅ |
| `allowed-tools`            | ✅ | — |
| `model`                    | ✅ | — |
| `argument-hint`            | ✅ | — |
| `disable-model-invocation` | — | ✅ |

二者交集是 `{name, description}`。其它字段都各自专属。

朴素的方案——把含 frontmatter 的原始 `SKILL.md` 整块存下来——有两种失败模式：

- **把两端字段并集都塞进文件**，依赖各 backend 自己忽略不认识的键。实践上能用，但每个 skill 都带着另一端会静默丢掉的字段噪声。
- **每个 skill 维护两份 `SKILL.md`，每端一份**。这复制了正文，等于放弃"写一次两端跑"。

最终方案把关注点分开：

- **`frontmatter_common`（JSONB）** —— 在每个 backend 上都有效的字段。至少包含 `name` 和 `description`。**绝大多数 skill 只填这一项就够。**
- **`frontmatter_backend`（JSONB）** —— 形状 `{claude: {...}, pi: {...}}`。仅装 backend 专属字段。多数时候是空的。
- **`skill_files.content WHERE path = 'SKILL.md'`** —— **只**存正文，绝不含 frontmatter 块。frontmatter 在投射时重新拼接。

REST API 与之对齐：客户端把 `frontmatter_common`、`frontmatter_backend`、`files` 分字段发送。提交带 `---` 块的 `SKILL.md` body 直接返回 422；正文放在 `files`，frontmatter 放在 JSONB。

---

## 容器投射

DB 存的是 skill，agent 读到的是文件。投射就是每次启动时的转换：

```
                  ┌────────────────────────────────────┐
                  │   PostgreSQL                       │
                  │   skills + skill_files             │
                  │   WHERE coworker_id = $1           │
                  │     AND enabled = TRUE             │
                  └─────────────────┬──────────────────┘
                                    │ 容器启动时
                                    ▼
       ┌─────────────────────────────────────────────────────────────┐
       │  /var/lib/rolemesh/spawns/<job_id>/skills/                  │
       │                                                             │
       │    code-review/                                             │
       │      SKILL.md           ← 把 frontmatter_common 与          │
       │                            frontmatter_backend.<target>     │
       │                            合并，序列化为 YAML，再加正文    │
       │      reference.md       ← skill_files 行，原样写入          │
       │      scripts/helper.py                                      │
       └─────────────────────┬───────────────────────────────────────┘
                             │ 只读 bind mount
                             ▼
       ┌─────────────────────────────────────────────────────────────┐
       │  容器目标路径，按 backend 选择：                            │
       │                                                             │
       │    Claude  →  /home/agent/.claude/skills/                   │
       │    Pi      →  /home/agent/.pi/agent/skills/                 │
       └─────────────────────────────────────────────────────────────┘
```

两个性质让它运行干净：

- **frontmatter 合并按文件、按 backend 进行。** 只有 `SKILL.md` 会做合并步骤（`common ∪ backend.<target>`）。支持文件原样投射，两端完全相同。
- **原子性以 skill 为单位，不是以文件为单位。** 每个 skill 先物化到 `<spawn>/.partial/<name>/`，然后通过一次 `os.rename` 翻成 `<spawn>/<name>/`。模型绝不会看到半写的 skill（例如 `SKILL.md` 已经可见但 `reference.md` 还在写）。

bind mount 是只读的，即便 agent 的工具（Bash、Edit）尝试改 skill 文件，内核也会拒绝写入。Skill 是配置；工作空间挂载（`/workspace/group`）才是唯一可写表面。

被禁用的 skill 在 SQL 查询那一步就被过滤掉，不是在投射器里 —— 它们物理上不进容器，模型连看都看不到，也就不可能误调用。

清理分两层：每次 spawn 的 finalizer 在正常退出时删临时目录；orphan cleaner 周期性扫描遗留目录（兜底 `kill -9` 的情况）。

---

## 触发语义：description 就是路由决策

两个 backend 触发 skill 的方式完全相同：**模型读每个 skill 的 `description`，自主决定何时调用**。host 侧没有规则引擎、没有路由器、没有 slash command。

这把整个路由权重压在 `description` 上。skill 作者必须：

- **写"何时使用"，不只写"做什么"。** "当用户要求 code review，或问'这代码哪里有问题'时" 远胜 "Code review skill"。
- **必要时写反例。** "不要用于一次性语法问题"能避免误触。
- **简洁。** 一到三句。description 每轮都加载——长 description 每个请求都烧 token。

这条指南在 Claude 和 Pi 上一致，因为触发模型一致。description 质量是 skill 实际是否有用的最大单一变量。

---

## 架构总览

```
                         ┌─────────────────────────────────────────┐
                         │  REST API (/api/admin/agents/{id}/skills)
                         │  AdminUser 鉴权，coworker 的子资源       │
                         │  POST / PATCH / DELETE / GET            │
                         └────────────────┬────────────────────────┘
                                          │ Pydantic schemas
                                          ▼
                         ┌─────────────────────────────────────────┐
                         │  pg.py CRUD                             │
                         │  （与其它 12 个 domain 并列）            │
                         │  执行 RLS + 跨租户 trigger              │
                         └────────────────┬────────────────────────┘
                                          │ SQL，绑定 tenant_id GUC
                                          ▼
                         ┌─────────────────────────────────────────┐
                         │  PostgreSQL                             │
                         │   skills + skill_files                  │
                         │   RLS、FK CASCADE、CHECK 约束           │
                         └────────────────┬────────────────────────┘
                                          │ 每次容器启动
                                          ▼
                         ┌─────────────────────────────────────────┐
                         │  skill_projection.py                    │
                         │  查 enabled skill，物化到                │
                         │  /var/lib/rolemesh/spawns/<job_id>/...  │
                         │  以 skill 为单位原子 rename             │
                         └────────────────┬────────────────────────┘
                                          │ 只读 bind mount
                                          ▼
                         ┌─────────────────────────────────────────┐
                         │  Agent 容器                             │
                         │   Claude: /home/agent/.claude/skills/   │
                         │   Pi:     /home/agent/.pi/agent/skills/ │
                         │   模型读 description 后自主调用 skill   │
                         └─────────────────────────────────────────┘
```

---

## 安全与隔离

五层防御，单独任何一层都不足以兜底：

1. **只读 bind mount** —— 内核强制；即便容器内有 `Bash` 与 `Edit` 工具，也无法改写 skill 文件。
2. **per-spawn 目录** —— 每个 job 一个唯一前缀；跨 spawn 不共享 skill staging 目录。
3. **三层租户隔离** —— 应用层 `WHERE tenant_id`、两张表上的 RLS 策略，以及 `skills` 表上的跨租户 trigger 校验 `coworker_id` 属于同租户。
4. **路径穿越双层拦截** —— `CHECK` 约束在写入时拒绝绝对路径、`..`、反斜杠；投射器在物化时再校验 `realpath(target).startswith(skill_root)`。
5. **正文是数据，不是代码** —— RoleMesh 不执行 skill 内容。agent 读 skill 之后做的任何事情都通过既有工具表面（Bash、Edit、MCP）出口，由既有 safety framework 管控。

mutation REST 接口由与 `/api/admin/agents` 其它接口同源的 `AdminUser` 依赖把关。Skill 管理是 admin 级别的操作；普通用户无法改动 agent 的行为形态。

---

## v1 明确不做的

下列项目都被有意排除在 v1 范围之外，每项都有清晰的、向前兼容的扩展路径：

| 不做项                            | 扩展路径 |
|-----------------------------------|----------|
| 跨 coworker 共享 skill            | 加一张 `skill_assignments` 关联表；现有行零改动 |
| group / department 范围            | 加 `scope` enum + `scope_id` 可空列 |
| 二进制 / 可执行资源                | 在 `content TEXT` 旁加 `content_bytes BYTEA` |
| 符号链接、exec bit                 | 加 `mode` / `link_target` 列 |
| 运行时热重载                       | 推送式重载——每次 spawn 已经重新读取，因此热重载只是 opt-in |
| Skill 版本历史                     | 加 `skill_revisions` 表；当前行保持权威 |
| Skill 之间的依赖                   | 加 `depends_on TEXT[]`，投射时解析 |
| CLI（`rolemesh skill pull/push`）  | 客户端层；走同一个 REST 接口 |
| WebUI 编辑器                       | 客户端层；走同一个 REST 接口 |

每一项扩展都是叠加式 —— 新列、新表、或新 enum 值。没有任何一项需要对已有 skill 做数据迁移。

---

## 值得点名的取舍

| 决定 | 选这边的理由 | 代价 |
|------|--------------|------|
| DB 作为权威 | 多租户 + 多 host + DR 一次解决 | 在 CLI 落地之前，IDE/git 编辑不方便 |
| frontmatter 拆 common + backend | "写一次两端跑"，两端都没有字段噪声 | 多一个 JSONB 字段要想 |
| 正文进 `skill_files.content`，frontmatter 只进 JSONB | 单一规范表示；round-trip 干净 | POST payload 是结构化的，不是"把 SKILL.md 整段粘进来" |
| 按 skill 原子 rename，不按文件 | 模型绝不会看到半物化的 skill | 文件系统操作略多 |
| 即使 Pi 侧是 tmpfs 也要叠只读 bind mount | 运行时不可篡改 | Pi 侧挂载层叠有 Docker 版本依赖 |
| CRUD 落进 `pg.py` | 与其它 12 个 domain 保持一致 | god-module 拆分再推后一个 domain |
| v1 没有 CLI / WebUI | 先把底座做出来，再考虑 UX | 早期用户用 curl 写 JSON |
| skill 仅附属于单个 coworker，不共享 | 最小可用模型 | 共享 skill 留给后续 PR |

---

## 已知空白

- **Pi 侧挂载层叠** —— 当前容器里 `/home/agent/.pi` 是 tmpfs，投射会在 `/home/agent/.pi/agent/skills/` 加只读 bind mount。Docker 支持这种叠加，但行为对版本敏感，每个部署需自行验证。
- **`coworkers.skills` JSONB 列** —— 早于本设计，被现有 REST API 引用。v1 内保留不动；后续 PR 在客户端迁移到新子资源 API 之后将其移除。
- **description 质量无自动检查** —— 除了最小长度之外，RoleMesh 无法判断一个 description 能否可靠路由模型。实践指南写在本文档；强制 linter 是将来的事。
- **没有跨租户的 skill 发现 API** —— v1 内有意不做。如果"租户 skill 库"成为产品需求，那是新加一个 scope，不是重设计。

---

## 相关文档

- [`3-agent-executor-and-container-runtime.md`](3-agent-executor-and-container-runtime.md) —— 容器生命周期、挂载构造、spawn 目录。
- [`4-multi-tenant-architecture.md`](4-multi-tenant-architecture.md) —— RLS 模式、`current_tenant_id()` GUC、本设计沿用的跨租户 trigger 模式。
- [`5-webui-architecture.md`](5-webui-architecture.md) —— `/api/admin/*` 接口面与 skills mutation 接口复用的 `AdminUser` 依赖。
- [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md) —— 双 backend 抽象；skills 复用同一个 per-coworker `agent_backend` 选择。
