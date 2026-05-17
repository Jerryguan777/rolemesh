# 行级安全（Row-Level Security）架构

本文档说明 RoleMesh 如何使用 PostgreSQL 的行级安全（RLS）在数据库层强制多租户隔离 —— 为什么要把信任边界下沉到数据库、考虑过哪些备选方案，以及让 RLS 与合法的跨租户维护工作共存的双 pool / 四函数分类架构。

它是 [`4-multi-tenant-architecture-cn.md`](4-multi-tenant-architecture-cn.md) 的安全架构续篇。如果你还不熟悉租户数据模型，建议先读那篇 —— 本文档假设你已经具备这个上下文。

读者对象：要新增租户表的开发者；正在排查"用户看不到自己数据"的同事；要新写一个跨多租户的后台循环的人；正在 review 一段新代码是否正确尊重租户边界的 reviewer。

---

## 背景：仅靠应用层过滤为什么不够

RoleMesh 的多租户模型采用共享 schema，所有业务表都带 `tenant_id UUID` 列。最早的实现完全靠应用代码在每条查询里写 `WHERE tenant_id = $1`。后来的一次重构（"把所有 by-id 查询都收敛到带 tenant_id 的形式"）把 `tenant_id` 提升为每个 by-id DB 函数的必填 keyword 参数，让 Python 语言本身拒绝忘记传 `tenant_id` 的调用。

这次重构消除了一整类 bug —— "忘写 WHERE" —— 但信任边界仍然在应用代码里。还有四类失败场景：

- **任一 endpoint 的 SQL 注入**能绕过整个模型。
- **新写的查询路径**如果没遵循模式，会静默泄漏；只能靠 review 纪律守住。
- **运维用 `psql` 直连**默认拥有跨租户可见性，读了什么也没有审计。
- **触发器派生的列**（如 `approval_audit_log.tenant_id`）如果触发器被禁用、或新写入路径绕过它，就会漂移。

RLS 是这四类问题的统一答案：无论是应用 bug、运维误操作、还是 schema 漂移导致的查询，DB 自己就拒绝它。启用 RLS 之后，应用层的 tenant 过滤变成**纵深防御**——仍然有用，但不再是主防线。

RLS 的非显而易见的代价是：DB 现在需要知道"这条查询服务于哪个租户？"这个上下文必须在每条查询前从某处传过来。本文档的核心内容就是讲这个上下文如何流动。

---

## 设计目标

1. **三层防御**。应用参数（函数实参）、连接上下文（PostgreSQL session 变量）、数据库策略（RLS）。任何一层失效都不应造成数据泄漏。
2. **对已工作代码零行为变化**。所有现有测试、所有正常工作的 REST endpoint、所有 NATS handler 在启用 RLS 后必须继续无修改地工作。
3. **跨租户操作显式化**。维护循环、调度器、几个 resolver 边界确实需要跨租户。它们必须**物理隔离**于业务代码之外，不能只靠约定标记。
4. **逐表、逐 PR 上线**。每张表的 RLS 都可以用一条语句开启或关闭。一张表出问题不需要回滚其它表。
5. **不能靠角色权限绕过**。所有受保护的表都要 `FORCE ROW LEVEL SECURITY`，确保即便是 owner 角色也受策略约束。唯一逃生通道是**专门**的 `BYPASSRLS` 角色，仅用于维护路径。
6. **纪律由静态检查保证**。约定是脆弱的。少数不可违反的规则（"webui 永不 import admin 原语"、"所有 `resolve_*` 函数必须有退役元数据"）由 CI 测试通过解析源码强制，而不是靠 docstring 警告。

---

## 备选方案对比

### 方案 A —— 仅应用层过滤（RLS 之前的状态）

继续到处写 `WHERE tenant_id = $N`，根本不引入 RLS。

**优点**
- 没有新基础设施（角色、策略、GUC）。
- 测试更简单 —— 一个 Postgres 角色，没有策略交互。
- 调试更容易 —— `EXPLAIN` 计划不变。

**缺点**
- 一处漏写 WHERE 就是一次跨租户泄漏。
- SQL 注入绕过整个模型。
- 运维通过 `psql` 没有任何约束。
- 没法防御未来"我就快速跑个临时查询"这种路径。

**否决。** 对于这种风险等级的共享 schema 多租户，把数据库强制纳入纵深防御是业界标准做法。代价是有限的。

### 方案 B —— 仅 RLS，不要应用层过滤

把应用 SQL 里的 `WHERE tenant_id` 全部删掉，让 RLS 成为唯一防线。

**优点**
- 代码更少。
- 租户边界单一真相源。
- 不存在两层不一致的风险。

**缺点**
- 如果某张表的 RLS 配错、或某个连接没设 GUC，就完全没有兜底。
- 复合索引 `(tenant_id, id)` 只有通过 RLS 才生效，查询规划器可能利用得不如显式 WHERE 充分。
- 失败模式更糟：没设 GUC 的连接静默返回 0 行，看起来像"用户没数据"，而不是明显的错误。
- 应用代码的租户意图变得不透明（"为什么这条查询读 `approval_requests` 但根本看不到任何租户上下文？"）。

**否决。** 应用层过滤成本低且显式。把它保留为纵深防御，能在查询层捕捉"忘了设 GUC"这一类 bug，避免它跑到生产环境。

### 方案 C —— Session 抽象替代 `tenant_id` 参数

引入 `TenantSession` 和 `AdminSession` 作为类型化对象，作为每个 DB 函数的第一个参数。Session 同时携带连接和隐式租户上下文；函数永远看不到裸的 `tenant_id` 字符串。

**优点**
- 更强的类型纪律（`TenantSession` 和 `AdminSession` 是不同类型，不能互换）。
- 跨租户意图在函数签名里就能看到，不只是 docstring。
- 每个调用点更干净（`tenant_id` kwarg 消失）。

**缺点**
- 所有约 60 个 DB 函数和所有调用方必须一次性重构。
- 所有测试 fixture 必须重写。
- Python 的类型系统不足以完全防止类型走私；纪律依然需要 mypy + lint 配合。
- 连接生命周期绑定到 session 生命周期，引入新的失败模式（长时间持有 session、泄漏）。
- 相对当前模式（"漏传 `tenant_id` kwarg 编译期报错"）的安全边际增量是真实的，但小于重构成本。

**否决。** 认真评估过；对一个已有可工作的 `tenant_id` kwarg 模式的代码库来说，重构成本太高。当前设计保留了未来若团队觉得 kwarg 噪音不可接受时迁移到 session 的可能。

### 方案 D —— Schema-per-tenant 或 Database-per-tenant

物理隔离：每个租户独占一个 Postgres schema（或数据库），应用每次请求设置 `search_path`。

**优点**
- 最强隔离 —— 任何 JOIN bug 都不可能跨租户。
- 按租户备份和恢复极简单。
- 没有 `tenant_id` 列、没有 RLS、没有 GUC。

**缺点**
- DDL 必须循环应用到 N 个 schema，迁移复杂度成倍上升。
- 跨租户分析（计费、报表）变成另一个独立问题。
- 连接池开销随租户数线性增长。
- 如此规模的架构改造与现有数据层完全不兼容。

**否决。** 适合高合规场景（金融、医疗）或极少租户数。对 RoleMesh 的目标场景 —— 中小规模租户共享基础设施 —— 共享 schema + RLS 是标准做法。

---

## 架构

### 三层防御

```
Layer 1: 应用参数
    函数签名：get_X(id, *, tenant_id)        # 必填 kwarg
    SQL 过滤：WHERE id = $1 AND tenant_id = $2
    防的是：  调用点漏检查、复合索引 (tenant_id, id) 命中

Layer 2: 连接上下文
    获取连接时（在事务内）：
        SELECT set_config('app.current_tenant_id', $1, true)
    防的是：  无上下文的连接（fail-closed）

Layer 3: 数据库策略
    每张表：  ENABLE ROW LEVEL SECURITY + FORCE
              POLICY USING (tenant_id = current_tenant_id())
    防的是：  SQL 注入、裸查询、角色绕过尝试
```

Layer 1 是 `tenant_id` kwarg 重构留下的遗产。Layer 2 和 Layer 3 是 RLS 工作新加的。

三层是**故意冗余的**。RLS 启用后，Layer 1 的 `WHERE tenant_id = $2` 返回的行集和 Layer 3 允许的完全相同。这个冗余正是重点 —— 它意味着即便某张表的策略配错，应用层也仍在请求正确的租户，不会立刻泄漏。

### 双 Pool，双角色

```
┌─────────────────────────────────────────────────────────────┐
│   App Pool   (角色: app_user,  NOBYPASSRLS)                 │
│   ├── 用于：webui REST handler、NATS 业务 handler            │
│   └── 包装为：tenant_conn(tenant_id) 上下文管理器             │
├─────────────────────────────────────────────────────────────┤
│   Admin Pool (角色: app_admin, BYPASSRLS)                   │
│   ├── 用于：维护循环、调度器、resolver、启动期迁移            │
│   └── 包装为：admin_conn() 上下文管理器                       │
└─────────────────────────────────────────────────────────────┘
```

**为什么用双 pool 而不是 `SET ROLE` 切换**。单 pool 配角色切换很脆弱：忘记 `RESET ROLE` 就把连接还回池里，下一个请求拿到了错误的权限。物理隔离让这种错误根本不可能发生 —— 业务代码根本拿不到 admin pool 对象。

**为什么用双角色而不是两个权限相同的用户**。角色权限由 Postgres 在每条查询上检查。`BYPASSRLS` 是角色级属性；如果让它由应用逻辑控制，就回到了类型走私风险。

### 函数四分类

每个数据库函数恰好属于以下四类之一：

| 类别 | 连接 | 签名 | 返回 | 例子 |
|---|---|---|---|---|
| **A. 租户内业务** | `tenant_conn(tenant_id)` | `tenant_id` 必填 kwarg | 完整 row | `get_approval_request`、`list_safety_rules` |
| **B. 跨租户维护** | `admin_conn()` | 无 `tenant_id` 参数 | 含 `tenant_id` 字段的行，供下游分发 | `list_expired_pending_approvals`、`cleanup_old_safety_approval_contexts` |
| **C. 租户解析器（边界 bootstrap）** | `admin_conn()` | 无 `tenant_id`（输出即权威） | **最小化标量**（str / tuple） | `resolve_request_tenant`、`resolve_user_for_auth` |
| **D. 启动期 / DDL** | `admin_conn()` | 不限 | 不限 | `init_database`、`_create_schema` |

分类不是仅文档约定 —— 是结构化强制的：
- A 类函数拿不到 admin pool（它对 `webui/` 不导出）。
- C 类函数必须以 `resolve_*` 为名，CI 测试验证它们只返回最小化标量，且只出现在白名单调用方中。
- B 和 D 类靠调用点已知（engine reconcile 循环、调度器、应用启动）自然隔离。

C 类要从 B 类里单独拎出来，尽管两者都跑在 `admin_conn` 上，是因为**信任范围**不同。B 类函数返回的行包含 `tenant_id` 字段，下游用它重新进入 `tenant_conn(row.tenant_id)`。C 类**只**返回 `tenant_id`（或 `(tenant_id, role)`），调用方必须立即用它构造租户作用域的 session。如果 C 也返回完整 row，就破坏了这个目的：调用方可以信任来自 admin 连接的 row，跳过租户作用域的复查，等于绕过了 RLS。

### 租户解析器契约

租户解析器之所以存在，是因为某些入口点确实还没有租户上下文：

- **NATS legacy fallback**。像 `approval.decided.<request_id>` 这样的 subject 可能在 body 里没有 `tenant_id`（比如某条消息是协议加 tenant_id 之前发的）。executor 必须先解析出 request 所属的租户，才能做任何 RLS 作用域的工作。
- **JWT resume**。用户提交一个仅含 `user_id` 的已签名 JWT 时，auth provider 必须先查到该用户的租户，才能构造 session。

这是**唯一**的合法用途。每个 resolver 都携带元数据，记录：
- **类型**：结构性（永久，如 JWT resume）或 legacy（在某个条件满足时可删除）。
- **允许的调用方**：显式文件路径。CI 检查没有其它模块 import 这个 resolver。
- **退役追踪**：legacy resolver 在什么条件下可以被删除。

元数据不是可选项。CI 套件解析源码，拒绝任何缺元数据块的 `resolve_*` 函数。

### Schema 协同设计

两张表值得显式提及，因为它们的形态是被 RLS 塑造的：

- **`approval_audit_log`** 有一个去规范化的 `tenant_id` 列（通过插入触发器从 `approval_requests` 复制），以及一个复合外键 `(request_id, tenant_id) → approval_requests(id, tenant_id)`。触发器让写入保持便利；复合外键即便触发器被禁用也能在 DB 层阻止漂移。读热路径使用复合索引 `(tenant_id, request_id, created_at)` —— 前两列索引 seek，再按 created_at 排序扫描。
- **`oidc_user_tokens`** 本质是用户级表（user_id 是自然键），但加了一个去规范化的 `tenant_id` 列，通过同样的触发器 + 复合外键模式从 `users.tenant_id` 同步。没有这个列，这张表无法有合理的 RLS 策略。

两张表显式不启用 RLS：
- **`tenants`** 是根表；只能通过 owner endpoint 和 admin 连接访问。
- **`external_tenant_map`** 是 OIDC 租户查找表；`app_user` 对它没有任何权限。

---

## 关键取舍

### 静默不匹配 vs. 信息泄露

当应用代码调用 `get_approval_request(req_id, tenant_id="错的")`，函数返回 `None`。这和"请求不存在"无法区分。这个行为是**故意**的 —— 区分两者会让攻击者能探测其它租户的资源是否存在。

代价是调试。一个 endpoint 有 bug、传错 `tenant_id`，表现是"找不到"，让开发者跑去查错的地方。缓解措施是：最安全敏感的几个 by-id 函数（`get_approval_request`、`get_user`、`list_approval_audit`）内部用 CTE 模式检测不匹配，并发出结构化告警日志和 `tenant_mismatch_attempted` 指标 —— 对运维可观测，但不暴露给调用方。

### 冗长 vs. 可审计性

每个业务调用点都显式带 `tenant_id=user.tenant_id` keyword 参数。相比隐式上下文（contextvars）或 session 抽象更吵。换来的是：任何跨租户意图在调用点就能看见 —— `grep admin_conn` 能枚举所有合法跨租户的代码路径。冗长被接受为这种可审计性的代价。

### 触发器便利 vs. 漂移风险

`approval_audit_log.tenant_id` 由插入触发器填充，不是应用代码。触发器可能被 DBA 静默禁用，新写入路径也可能绕过触发器而不写这一列。复合外键 `(request_id, tenant_id) → approval_requests(id, tenant_id)` 抵御两者：含 NULL 或错误 `tenant_id` 的行根本插不进去。触发器保留作为便利；外键是安全网。

### 双 Pool vs. 单 Pool

单 pool 配 `SET ROLE app_user` / `SET ROLE app_admin` 切换能节省内存和连接槽位。风险 —— 忘记 reset 角色把 admin 权限泄漏到下个请求 —— 对当前的风险等级是不可接受的。多一个 pool 的成本是几个连接；这是一种廉价的物理隔离。

---

## 迁移路径

RLS 分五个串行阶段落地。每个阶段设计为可独立部署和回滚，且任一阶段不会让系统比上一阶段更弱。

1. **应用层 pinning**。给剩余 by-id 函数加 `tenant_id` 必填 kwarg；新增 `resolve_user_for_auth` 用于 JWT resume；用两步 bootstrap 模式重写 auth provider。DB 还不变。
2. **基础设施**。创建 `current_tenant_id()` SQL 函数、`app_user` / `app_admin` 角色、双 pool 和 `tenant_conn` / `admin_conn` 包装。RLS 仍未启用；业务行为不变。
3. **连接迁移**。把每个 `pool.acquire()` 替换为 `tenant_conn(tenant_id)` 或 `admin_conn()`，按函数类别决定。本阶段后所有业务路径都正确携带租户上下文 —— 但 RLS 仍未强制。
4. **逐表启用**。一次一张表启用 RLS，从 blast radius 最小的（`approval_audit_log`）开始作 canary，最后是 `users` 和 `oidc_user_tokens`。每张表一个 commit，可单独用一条 `ALTER TABLE ... DISABLE ROW LEVEL SECURITY` 回滚。
5. **强制力测试**。新增测试验证 RLS 真的在 DB 层挡住跨租户访问（区别于应用层验证 WHERE 子句的测试）。新增静态分析 CI 检查保证四分类不被破坏。

顺序至关重要：阶段 4 不能早于阶段 3，因为路径还有裸 `pool.acquire()` 时启 RLS 会静默地把那条路径打挂（未设 GUC → fail-closed → 返空）。

---

## 本架构**不**做什么

- **不防御 `app_admin` 凭据泄漏**。任何掌握 admin 角色的人都拥有完整的跨租户访问。该角色的凭据必须像 Postgres superuser 一样小心管理。
- **不按租户分区 NATS 消息总线**。被攻陷的容器可能订阅 `agent.*.tasks` 这种 subject 并观察（但无法修改）其它租户的消息。Engine 在写入路径校验租户，但读侧观察被记录为 `tests/attack_sim/test_E_tenant_isolation.py` 的 XFAIL（`test_E6`）。
- **不提供按租户的资源配额**。并发限制存在（`max_concurrent_containers`），但 token / 花费 / API 配额不在范围内。
- **不自动退役 `resolve_*` 函数**。每个 resolver 带退役元数据，但实际删除需要未来某个 PR 在条件满足时手动决定。
- **不在运行时强制角色纪律**。"webui 永不 import `admin_conn`" 由 CI 时的 AST 测试检查。决心要绕过的开发者不会被运行时机制阻止。

---

## 运维考量

### 用 `psql` 直连

运维的 `psql` session 默认是 Postgres superuser，带 `BYPASSRLS`，能看到一切。要模拟应用视图：

```sql
SET ROLE app_user;
SELECT set_config('app.current_tenant_id', '<tenant_uuid>', false);
-- 后续查询会按该 tenant 作 RLS 作用域
```

用 `false` 而非 `true` 让设置在 session 内持久，方便临时排查。

### 备份与恢复

`pg_dump` 包含 RLS 策略和 `FORCE` 设置。`pg_restore` 必须以 superuser 跑（要创建角色和策略）。恢复后应用的 `_create_schema` 是幂等的，会和好任何漂移。

### 排查"用户看不到数据"

如果用户报告应该看到的数据缺失，按顺序排查：

1. 用户的 JWT 是否被解码到了正确的 `tenant_id`？（`auth/oidc/provider.py` 日志包含解析到的 tenant。）
2. 连接是否走了 `tenant_conn`？在可疑查询点临时加一条 `current_setting('app.current_tenant_id')` 的日志。
3. 相关表的 RLS 策略是否符合预期？`SELECT * FROM pg_policies WHERE tablename = '<table>'`。
4. 角色是 `app_user` 还是不小心走到了 `app_admin`？连接里跑 `SELECT current_user`。

### 新增租户表

新加一张租户表时：

1. 加 `tenant_id UUID NOT NULL REFERENCES tenants(id)`（考虑 `ON DELETE CASCADE`）。
2. 如果读频繁，加复合索引 `(tenant_id, <热查询列>)`。
3. 在 `_create_schema` 里加标准的四条 RLS 策略（SELECT / INSERT / UPDATE / DELETE），加上 `ENABLE ROW LEVEL SECURITY` 和 `FORCE ROW LEVEL SECURITY`。
4. 加 CRUD 函数，遵循 A 类模式（`tenant_id` 必填 kwarg，`tenant_conn(tenant_id)` 包装）。
5. 在 `tests/db/test_cross_tenant_isolation.py` 加跨租户隔离测试。

CI 的 AST 测试会捕捉到大部分模式违反；建议加新函数前先看一眼这些测试。

---

## 相关文档

- [`4-multi-tenant-architecture-cn.md`](4-multi-tenant-architecture-cn.md) —— 租户数据模型、实体层级、消息路由
- [`6-auth-architecture-cn.md`](6-auth-architecture-cn.md) —— `AgentPermissions`、使用 `resolve_user_for_auth` 的 JWT resume 流程
- [`12-approval-architecture-cn.md`](12-approval-architecture-cn.md) —— approval audit log schema 和同步 `tenant_id` 的触发器
- [`2-nats-ipc-architecture-cn.md`](2-nats-ipc-architecture-cn.md) —— NATS subjects 和 `resolve_request_tenant` 服务的 legacy fallback
