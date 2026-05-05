# 容器运行时与 Agent Executor 架构

本文档描述位于协调器 (Orchestrator) 与实际容器进程之间的两个抽象层：**ContainerRuntime**（容器如何启动与管理）和 **AgentExecutor**（agent 工作如何派发）。文中涵盖这些抽象解决的问题、其背后的设计决策，以及它们如何使容器后端 (Docker → Kubernetes) 与 agent 后端 (Claude SDK → Pi) 能够独立替换。

> **项目演进。** RoleMesh 由 [NanoClaw](https://github.com/qwibitai/nanoclaw) 演进而来；这套两层切分是在重写过程中从一个 340 行的单体函数里梳理出来的，因此下文的历史段落描述的是被替换掉的最初 NanoClaw 形态。抽象本身是 RoleMesh 时代的产物；后续阶段（多租户、容器加固、出向控制）通过为 `ContainerSpec` 增加额外字段扩展了它，但并未改变层次切分。

---

## 问题：两件事被纠缠在一起

最初的 NanoClaw 有一个约 340 行的单体函数 `run_container_agent()`，把所有事情塞在一次调用里：

1. 构建 volume mount 列表
2. 把 `docker run` CLI 参数构造为字符串数组
3. 调用 `asyncio.create_subprocess_exec("docker", "run", ...)`
4. 把初始输入写入 NATS KV
5. 订阅 NATS JetStream 以流式接收结果
6. 从子进程管道读取 stderr
7. 管理基于活跃度的超时
8. 把执行日志写入磁盘
9. 返回最终结果

这里混杂了两类不相关的关注点：

- **容器生命周期**（步骤 1–3）：如何启动、停止与监控容器——Docker 命令、volume mount、环境变量、进程管理这些机制层面的内容。
- **agent 编排**（步骤 4–9）：容器跑起来之后做什么——写入提示词、订阅结果、处理超时、收集输出。

它们必须能独立演进：

- 从 Docker 切换到 Kubernetes 改变的是容器生命周期，而不是 agent 编排。
- 从 Claude SDK 切换到 Pi 改变的是容器内部的 agent，而不是容器本身的管理方式。

把它们塞在单一函数里，沿任一方向演进都会牵连另一边。

---

## 解决方案：两层架构

```
                        Orchestrator (main.py)
                              │
                    ┌─────────▼──────────┐
                    │   AgentExecutor     │  "What work to do"
                    │   Protocol          │
                    ├─────────────────────┤
                    │ ContainerAgent-     │  Writes KV, subscribes NATS,
                    │   Executor          │  manages timeout, collects output
                    └─────────┬──────────┘
                              │
                    ┌─────────▼──────────┐
                    │  ContainerRuntime   │  "How to run containers"
                    │  Protocol           │
                    ├─────────────────────┤
                    │  DockerRuntime      │  Docker Engine API (aiodocker)
                    │  (K8sRuntime)       │  Kubernetes Jobs (future)
                    └─────────┬──────────┘
                              │
                         Container
                    (Claude SDK or Pi)
```

两层之间通过小型数据类型 (`ContainerSpec`、`ContainerHandle`、`AgentInput`、`AgentOutput`) 通信，这些类型只承载形状、不承载行为。边界两侧的代码可以互相替换、mock 或检视，互不干扰。

---

## 第 1 层：ContainerRuntime

底层。它知道如何检查容器后端是否可用、依据规约启动一个容器、停止一个运行中的容器，以及清理上次崩溃残留的孤儿。它**不**知道任何关于 agent、提示词、NATS、会话或业务逻辑的事情。

该 Protocol 暴露五个方法：`ensure_available`、`run`、`stop`、`cleanup_orphans`、`close`。任何满足该形状的对象（Python `Protocol`——见下文"设计权衡"）都是合法的运行时。

### ContainerSpec：要运行什么

一个 frozen dataclass，描述启动容器所需的一切。最小核心是你预期会有的——`name`、`image`、`mounts`、`env`、`user`、`memory_limit`、`cpu_limit`、`entrypoint`、`extra_hosts`。在此之上，后续阶段又添加了两组字段：

- **加固字段**——`cap_drop`、`security_opt`、`readonly_rootfs`、`tmpfs`、`pids_limit`、`memory_swap`、`memory_swappiness`、`ulimits`、`runtime`（`runc` / `runsc`）。全部默认为安全值；只设置 name/image/env 的现有调用点仍然能编译。设计依据见 [`safety/container-hardening.md`](safety/container-hardening.md)。
- **网络字段**——`network_name`、`dns`。EC-1 把每个 agent 接入一个 `Internal=true` 的桥接网络，并以出向网关 (egress gateway) 作为权威 DNS。拓扑详见 [`egress/deployment.md`](egress/deployment.md)。

这里的契约只是"spec 携带了运行时所需的一切"；这些字段的*内容*由安全/网络相关文档负责。

spec 由 `container/runner.py` 中的纯函数 (`build_volume_mounts`、`build_container_spec`) 构建——它们仅根据某个 coworker 的配置计算出 spec，不做任何 I/O。让它们保持纯净，意味着诸如"展示给我看 spec 长什么样"的 dry-run 模式或单元测试都能直接调用，而无需 mock Docker。

### ContainerHandle：你拿回的句柄

一个指向运行中容器的句柄。刻意保持极小——只有三个方法：`wait()` 等待退出、`stop(timeout)` 终止容器、`read_stderr()` 流式读取日志。**没有 `read_stdout_line()` 或 `write_stdin()`。** 三个原因：

1. **NATS 取代了 stdin/stdout。** agent 从 NATS KV 读取初始输入，并把结果发布到 JetStream。完全不需要管道通信。见 [`nats-ipc-architecture.md`](nats-ipc-architecture.md)。
2. **Docker API 的 stdin/stdout 很复杂。** 它需要带自定义缓冲与帧的 WebSocket attach——要在 Docker 与 Kubernetes 上都做得可靠，比看起来要难。
3. **句柄越简单越容易移植到 K8s。** Kubernetes Job 根本没有 stdin 管道。

唯一保留的 I/O 方法 `read_stderr()` 只是普通的日志流——没有帧、没有双向通信，只是用于诊断的字节流。

### DockerRuntime：当前实现

使用 `aiodocker`（异步 Docker Engine API 客户端），而不是子进程调用。

| 方案 | 为何未采用 |
|---|---|
| `subprocess.run(["docker", "run", ...])` | 基于字符串拼接参数过于脆弱、缺乏结构化错误处理、无法干净地流式读取 stderr。 |
| `asyncio.create_subprocess_exec(...)` | 好一些，但仍然是字符串参数；需要手工管理进程；无法平滑迁移到 Kubernetes。 |
| **`aiodocker`（Docker Engine API）** | 结构化的 config 字典、原生 async、合理的错误类型、与 Kubernetes 客户端形态相似的 API。 |

`aiodocker` 用大约 200 行代码的 `DockerRuntime` 就完美满足我们的需求，且原生支持 async。

**值得了解的一个坑**：Docker 的 `AutoRemove` 标志（即 API 等价于 `docker run --rm`）会与 `container.wait()` 发生竞态——等你去读退出码时容器可能已经消失。我们不使用 `AutoRemove`，而是在 `ContainerHandle.stop()` 中显式删除。

### K8sRuntime：未来的扩展点

`ContainerSpec` 可以干净地映射到一个 Kubernetes Job (`name → metadata.name`、`image → spec.containers[0].image`、`mounts → volumes + volumeMounts` 等)。`K8sRuntime` 会使用 `kubernetes-asyncio` 创建 Job、监听完成、流式读取日志；它上方的 `ContainerAgentExecutor` 完全不需要修改。今天它还只是个抛 `NotImplementedError` 的占位实现——这个抽象的主要价值在于：当真的需要部署到 Kubernetes 时，它就是一个干净的替换点。

### 运行时选择

工厂方法读取 `CONTAINER_RUNTIME`（默认 `docker`，未来 `k8s`）并返回匹配的运行时实例。Orchestrator 在启动时调用一次，并将该实例传递给所有需要它的组件。

---

## 第 2 层：AgentExecutor

高层。它知道如何把 agent 的初始输入写入 NATS KV、启动容器（通过 ContainerRuntime）、订阅 JetStream 以流式接收结果、管理基于活跃度的超时、读取并记录 stderr，并返回结构化输出。它**不**知道容器是怎么启动或停止的——那是 ContainerRuntime 的职责。

该 Protocol 接受一个 `AgentInput` 并返回一个 `AgentOutput`，外加两个回调：`on_process(container_name, job_id)` 让调度器追踪活跃容器，以及 `on_output(parsed)` 让 orchestrator 把每一块结果在到达时流式回送给用户。

### 为什么是单一实现，而不是每个后端一个 executor 类

在评估 Pi 后端时我们发现，**协调器侧**的流程对每种 agent 后端都是一样的：

1. 构建 volume mount
2. 构建 container spec
3. 把初始输入写入 NATS KV
4. 启动容器
5. 订阅 NATS 结果
6. 管理超时
7. 读取 stderr
8. 返回输出

唯一的差异在于配置：容器镜像、入口点、几个额外的 mount、几个额外的 env 变量。所以与其写成：

```
❌  ClaudeCodeExecutor   (orchestration logic)
❌  PiExecutor           (same orchestration logic, different config)
```

我们采用：

```
✅  ContainerAgentExecutor + AgentBackendConfig
```

——一个类，通过一个小型 frozen dataclass 按后端进行配置。新增第三种后端只需要添加一个新的 `AgentBackendConfig` 常量，而不是新建一个类。

### AgentBackendConfig 与单镜像设计

`AgentBackendConfig` 携带 `name`、`image`、`entrypoint`、`extra_mounts`、`extra_env`、`skip_claude_session`。今天有两个预设：

- `CLAUDE_CODE_BACKEND` (`name="claude"`)
- `PI_BACKEND` (`name="pi"`)

两个预设都引用**同一个 Docker 镜像** (`rolemesh-agent:latest`)；镜像内部的 agent_runner 在启动时根据 `AGENT_BACKEND` 环境变量选择运行时路径，而该变量由 executor 通过 `extra_env` 注入。

**为什么用单一镜像，而不是每个后端一个镜像？**

- 镜像缓存更小、构建流水线更简单、加固只需要在一个地方应用。
- 运行时切换后端只是改一个 env 变量——无需拉镜像，也无需滚动部署。
- 中途切换后端的 coworker 在下次 spawn 时立即获得新行为，无需任何容器构建协调。

代价是镜像稍大一些（即便 coworker 使用 Claude SDK，Pi 的依赖也会被打入）。对于一个自托管平台镜像而言，这是几百 MB 的一次性成本，并非每次 spawn 都付出——这个权衡是划算的。

### 后端选择：按 coworker 派发

orchestrator **不**在进程级别选择一个后端。它在启动时为每个 `AgentBackendConfig` 构造一个 `ContainerAgentExecutor`，并存到一个 dict 里：

```
_executors = {
    "claude": ContainerAgentExecutor(CLAUDE_CODE_BACKEND, ...),
    "pi":     ContainerAgentExecutor(PI_BACKEND, ...),
}
```

当一个 turn 到达时，orchestrator 通过 `coworker.agent_backend` 查找对应的 executor。**同一个 orchestrator 内的不同 coworker 可以并发地运行在不同后端上**——这在多租户场景中很常见，比如某个租户偏好 Claude SDK，另一个出于合规需要走 Pi+Bedrock。

一个全局默认值（`ROLEMESH_AGENT_BACKEND` 环境变量）只对那些 `agent_backend` 字段为 NULL 的 coworker 起作用——在实践中只是一个空的逃生通道。

---

## 两层如何协同工作

一次完整的 agent 调用：

```
1. Orchestrator receives a message for a coworker
        │
2. Pick executor by coworker.agent_backend → ContainerAgentExecutor
        │
3. ContainerAgentExecutor.execute(AgentInput(...))
        │
        ├── build_volume_mounts(coworker, permissions, backend_config)
        │     → list[VolumeMount]
        │
        ├── build_container_spec(mounts, name, job_id, backend_config)
        │     → ContainerSpec   (with hardening + network fields filled in)
        │
        ├── Write AgentInitData to NATS KV "agent-init.{job_id}"
        │     (carries permissions, mcp_servers, safety_rules, approval_policies, …)
        │
        ├── runtime.run(spec)                    ← ContainerRuntime layer
        │     → ContainerHandle
        │
        ├── on_process(container_name, job_id)   ← scheduler tracks this
        │
        ├── Subscribe to agent.{job_id}.results  ← NATS JetStream
        │
        ├── Start timeout watcher + stderr reader tasks
        │
        ├── (Inside the container, hooks/safety/approval/skills run as the
        │    LLM produces tool calls and outputs — orchestrator side just
        │    consumes events from NATS)
        │
        ├── Wait for container exit              ← handle.wait()
        │
        ├── Cancel subscriptions and tasks
        │
        └── Return AgentOutput(status, result, new_session_id)
```

切分非常干净：任何调用 `runtime.*` 或 `handle.*` 的代码都属于 ContainerRuntime 层，其余（NATS、超时、日志、输出解析）都属于 AgentExecutor 层。

容器*内部*运行的内容——hook 处理器、safety 流水线、approval 阀门、skill 加载、MCP 工具派发——分别由各自的文档说明 ([`hooks-architecture.md`](hooks-architecture.md)、[`safety/safety-framework.md`](safety/safety-framework.md)、[`approval-architecture.md`](approval-architecture.md)、[`skills-architecture.md`](skills-architecture.md)、[`external-mcp-architecture.md`](external-mcp-architecture.md))。从 executor 的视角看，它们只是 NATS 上的事件。

---

## 并发：GroupQueue

`ContainerAgentExecutor` 是*调用*原语——每次调用 spawn 一个容器。*何时* spawn 的决定由 `GroupQueue`（位于 `container/scheduler.py`）负责，它强制三道相互独立的并发上限：

- **全局**——orchestrator 内全部 agent 容器的总数。
- **按租户**——单个租户不能耗尽全局配额。
- **按 coworker**——某个话痨 coworker 不能耗尽其租户的配额。

当一个 turn 到达时，`GroupQueue` 要么立即把它派给 executor，要么排队直到对应上限有空闲。运行时层对此一无所知——它看到的只是稳定的 `runtime.run(spec)` 调用流。

---

## 平台辅助

两个小的平台相关关注点以模块级函数的形式存在于 `runtime.py`，独立于任何运行时实现：

- **代理 bind 主机**——凭据代理需要绑定到容器可达的地址。在 macOS / WSL 上是 `127.0.0.1`（Docker Desktop 把 `host.docker.internal` 路由到宿主机 loopback）；在原生 Linux 上是 `docker0` 的桥接 IP（通常是 `172.17.0.1`）；兜底是 `0.0.0.0`。检测使用 `fcntl.ioctl` 配合 `SIOCGIFADDR`。
- **宿主网关**——在 Linux 上 `host.docker.internal` 默认无法解析；会向每个 spec 注入一条 `--add-host`。

EC-1 之后大部分 agent 流量已经不再到达宿主 loopback——agent 处于 `Internal=true` 桥接网络，出向流量经过出向网关 (egress gateway)。这些辅助函数仍服务于遗留 / 调试代码路径以及网关容器自身；生产路径详见 [`egress/deployment.md`](egress/deployment.md)。

---

## 设计权衡

### 为什么用 `Protocol`，而不是 `ABC`？

Python 的 `typing.Protocol` 启用结构化子类型——一个类只要拥有正确的方法就满足 Protocol，无需继承它。这意味着：

- `DockerRuntime` 不需要写 `class DockerRuntime(ContainerRuntime)`；它只需实现这些方法。
- 测试可以使用简单的 mock 对象，无需继承。
- 运行时模块不需要 import 实现模块。

ABC 会强制继承、引入 import 依赖以及注册样板代码，却没有任何实际收益。

### 为什么不使用更高层的编排库？

像 `docker-py`（同步）或全套编排框架（Kubernetes Operator SDK）这类库会带来我们并不需要的复杂度。我们的需求非常简单——按一份 config 启动容器、等待退出、读取 stderr、停止、清理孤儿。`aiodocker` 用大约 200 行的 async 代码就覆盖了。

### 为什么把 `build_volume_mounts` / `build_container_spec` 与 executor 拆开？

这些是纯函数：给定输入，它们产出一个 `ContainerSpec`，没有副作用。把它们放在 executor 类外面意味着：

- 它们可以测试，无需 mock Docker 或 NATS。
- 它们可以被复用（dry-run 模式、容器 spec 预览、评测框架）。
- executor 类专注于编排流程，而不是配置计算。

### 为什么 `ContainerHandle` 上没有 stdin/stdout？

上文（"第 1 层 → ContainerHandle"）已说明。简短版：NATS 替代了 stdin/stdout，Docker API 的 stdin/stdout 复杂，更简单的句柄能干净地移植到 Kubernetes。

---

## 容器命名与孤儿清理

容器名遵循 `rolemesh-{safe_group_folder}-{epoch_ms}` 这一模式（例如 `rolemesh-main-1711612800000`）。启动时 Orchestrator 调用 `runtime.cleanup_orphans("rolemesh-")` 来寻找并移除上次崩溃遗留的容器。基于前缀的过滤可以捕获所有 RoleMesh 容器，无论是哪个 coworker 或 job 创建的。

---

## 依赖图

```
main.py
  │
  ├── get_runtime() → DockerRuntime
  │
  ├── For each AgentBackendConfig (CLAUDE_CODE_BACKEND, PI_BACKEND):
  │       _executors[name] = ContainerAgentExecutor(cfg, runtime, transport, get_coworker)
  │
  └── GroupQueue(transport, runtime, orchestrator_state)
        │
        ├── Dispatches to _executors[coworker.agent_backend].execute(...)
        ├── runtime.stop(name)            ← for shutdown
        └── transport.nc.request("agent.{job_id}.shutdown", ...) ← graceful close
```

`ContainerRuntime` 同时被注入到 executor（用于启动容器）和调度器（用于在关停时停止容器）。两边都不依赖具体实现——都是面向 Protocol 编程。

---

## 相关文档

- [`nats-ipc-architecture.md`](nats-ipc-architecture.md) —— executor 使用的 IPC 协议
- [`switchable-agent-backend.md`](switchable-agent-backend.md) —— agent 侧的 `AgentBackend` protocol（容器*内部*的运行时）
- [`backend-stop-contract.md`](backend-stop-contract.md) —— 任意后端在停止时必须交付的可观测行为
- [`safety/container-hardening.md`](safety/container-hardening.md) —— 谁来填充 `ContainerSpec` 的加固字段
- [`egress/deployment.md`](egress/deployment.md) —— 谁来填充 `ContainerSpec` 的网络字段
