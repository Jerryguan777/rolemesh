# 容器运行时解耦设计：`ROLEMESH_CONTAINER_RUNTIME=docker|k8s`

> 状态：设计稿（待评审）
> 目标：同一份业务代码，通过一个环境变量在 Docker 与 Kubernetes 之间切换；
> 本地 Mac（Apple Silicon）用 Docker 模式与 kind 模式验证，生产部署到
> Rancher RKE2 + Helm chart。

---

## 1. 现状分析

### 1.1 已有的好基础

- `src/rolemesh/container/runtime.py` 已定义 `ContainerRuntime` / `ContainerHandle`
  Protocol 与 `ContainerSpec` / `VolumeMount` frozen dataclass；`get_runtime()`
  工厂已预留 `k8s` 分支（当前抛 `NotImplementedError`）。
- 规格构建（`runner.py`）与调度（`scheduler.py`）是纯函数 / 纯 asyncio，
  不直接触碰 Docker。
- Agent 与 orchestrator 之间的 IPC 走 NATS（KV 初始化 + JetStream 流式结果），
  **不依赖 stdin/stdout**——这在 K8s 下天然成立。
- 环境变量注入有 `CONTAINER_ENV_ALLOWLIST` 白名单，挂载有
  `mount_security.py` 校验，均与运行时无关。

### 1.2 耦合点清单（按修复优先级）

| # | 耦合点 | 位置 | 性质 |
|---|--------|------|------|
| C1 | `container_executor._publish_agent_started()` 用 `getattr(runtime, "_ensure_client")` 绕过抽象直接查容器 IP | `agent/container_executor.py:710-756` | 抽象泄漏 |
| C2 | `DockerRuntime` 上挂了 Protocol 之外的网络方法（`ensure_agent_network` / `ensure_egress_network` / `verify_*`），`main.py` 用 `hasattr` 探测调用 | `container/docker_runtime.py:322-358`, `main.py:1724-1742` | 抽象泄漏 |
| C3 | egress gateway 由 orchestrator 用 aiodocker 直接启动（双网卡 connect、镜像 inspect、就绪轮询） | `egress/launcher.py`, `egress/bootstrap.py` | 进程级 Docker 依赖 |
| C4 | 网络拓扑 = Docker 特有概念：`Internal=true` bridge、双网卡、`enable_icc`、Alpine 探针容器 | `container/network.py` | 架构级 |
| C5 | `host.docker.internal` / docker0 探测 / `rewrite_loopback_to_host_gateway()`——隐含"orchestrator 跑在宿主机、agent 跑在容器"的拓扑假设 | `container/runtime.py:146-238` | 拓扑假设 |
| C6 | `VolumeMount.host_path` 是 orchestrator 本机绝对路径（`DATA_DIR = PROJECT_ROOT/"data"`），bind mount 假设 orchestrator 与容器同宿主 | `runner.py`, `skill_projection.py` | 存储假设 |
| C7 | DNS 走 `HostConfig.Dns`（注入 egress gateway 的 bridge IP）；IP 在 gateway 启动后由 `set_egress_gateway_dns_ip()` 全局注册 | `runner.py:280-299,584`, `egress/bootstrap.py` | Docker API 特有 |
| C8 | 身份识别：gateway 按 source IP 反查 agent 身份，IP 取自 `docker inspect NetworkSettings` | `egress/identity.py` + C1 | 依赖 C1 修复 |
| C9 | 开关名为 `CONTAINER_BACKEND`，需统一为 `ROLEMESH_CONTAINER_RUNTIME` | `core/config.py:74` | 命名 |
| C10 | 无 K8s/Helm/kind 任何文件；无契约测试框架（现状 = mocked 单测 + 真 Docker 集成测试） | — | 缺失 |

**核心判断**：生命周期抽象（run/wait/stop/logs）的迁移是机械工作；
**真正的设计工作在 C3–C8**——网络隔离、egress gateway 归属、DNS、身份、存储，
这五件事在两个运行时下的"实现机制"完全不同，必须把它们从"代码做什么"
提升为"运行时承诺什么"。

---

## 2. 设计原则

1. **业务代码只依赖契约，不依赖机制。** 业务层（executor、scheduler、egress
   逻辑、safety）只看到 `ContainerRuntime` 及配套 Protocol；"用 bridge 还是
   NetworkPolicy 实现隔离"是 runtime 实现的私事。
2. **声明式优先。** Docker 模式下 orchestrator 必须自己创建网络、启动 gateway
   （命令式）；K8s 模式下这些由 Helm chart 声明，runtime 只做**发现与校验**
   （fail-closed：校验不过拒绝启动 agent）。
3. **契约测试是契约的可执行定义。** 同一套测试用例参数化跑在两个 runtime 上；
   一切"两边行为应当一致"的承诺都必须有对应用例，否则不算承诺。
4. **fail-closed 不降级。** K8s 模式下若 NetworkPolicy 不可校验、gateway 不可达、
   DNS 未指向 gateway，与 Docker 模式下 dockerd 版本过低同等处理：拒绝启动。

---

## 3. 两套运行时的机制映射总表

| 契约（业务代码看到的） | Docker 实现 | K8s 实现 |
|---|---|---|
| 启动一个 agent 沙箱 | `containers.create_or_replace` + start | 创建 Pod（`restartPolicy: Never`，打标签 `rolemesh.io/role=agent`） |
| 等待退出 / 取退出码 | `container.wait()` | watch Pod phase → `Succeeded/Failed`，取 terminated.exitCode |
| 流式 stderr | `container.log(stderr=True, follow=True)` | `read_namespaced_pod_log(follow=True)`（注：K8s 不分流 stdout/stderr，见 §5.6） |
| 停止 + 清理 | stop + delete force | delete Pod（grace period = timeout） |
| 孤儿清理 | name 前缀 + 镜像白名单 | label selector `rolemesh.io/managed-by=orchestrator` + 镜像白名单 |
| 查询沙箱网络身份（IP） | inspect `NetworkSettings.Networks[net].IPAddress` | Pod `status.podIP` |
| 网络隔离（agent 无直接出网） | `Internal=true` bridge | 默认拒绝的 egress `NetworkPolicy`（仅放行 → gateway Pod、→ NATS、→ gateway:53/udp） |
| egress gateway 双面性 | 双网卡（agent-net + egress-net） | 单网卡即可：对内是 Service，对外不受 agent 侧 NetworkPolicy 限制（gateway 自己的 policy 放行出网） |
| agent 的 DNS 强制走 gateway | `HostConfig.Dns = [gateway bridge IP]` | Pod `dnsPolicy: None` + `dnsConfig.nameservers: [gateway Service ClusterIP]` |
| gateway 生命周期 | orchestrator 用 aiodocker 启动（launcher.py） | Helm 管理的 Deployment + Service；orchestrator 只发现 + 健康检查 |
| 凭证代理可达性 | `host.docker.internal` + ExtraHosts host-gateway | orchestrator 本身是 Pod，agent 通过 orchestrator Service 直达；无需任何 URL 重写 |
| 资源限制 | Memory/NanoCpus/PidsLimit | resources.limits（pids 是 kubelet `podPidsLimit`，集群级，见 §9 风险） |
| 加固 | CapDrop ALL / ReadonlyRootfs / no-new-privileges / tmpfs | securityContext（drop ALL、readOnlyRootFilesystem、runAsNonRoot、seccompProfile RuntimeDefault）+ emptyDir(medium=Memory, sizeLimit) |
| gVisor | `HostConfig.Runtime: runsc` | `runtimeClassName: gvisor`（RKE2 需预装 RuntimeClass） |
| 存储（workspace/sessions/skills） | bind mount 宿主路径 | 共享 PVC + `subPath`（orchestrator 与 agent Pod 挂同一 PVC，见 §6） |
| 连通性自检 | Alpine 探针容器 | 探针 Pod（同镜像策略），或 orchestrator Pod 内直接 TCP 探测 |

---

## 4. 代码解耦设计

### 4.1 配置开关（修 C9）

```python
# core/config.py
CONTAINER_RUNTIME: str = (
    os.environ.get("ROLEMESH_CONTAINER_RUNTIME")
    or os.environ.get("CONTAINER_BACKEND")   # 兼容别名，读到时打 deprecation 警告
    or "docker"
)
```

新增 K8s 专属配置（全部带 `ROLEMESH_K8S_` 前缀，docker 模式下忽略）：

| 变量 | 默认 | 用途 |
|---|---|---|
| `ROLEMESH_K8S_NAMESPACE` | 当前 Pod 的 namespace（downward API） | agent Pod 所在 namespace |
| `ROLEMESH_K8S_DATA_PVC` | `rolemesh-data` | orchestrator/agent 共享 PVC 名 |
| `ROLEMESH_K8S_EGRESS_GATEWAY_SERVICE` | `rolemesh-egress-gateway` | gateway Service 名（发现 DNS IP 用） |
| `ROLEMESH_K8S_IMAGE_PULL_SECRET` | 空 | 私有 registry 凭证 |
| `ROLEMESH_K8S_RUNTIME_CLASS` | 空 | gVisor 等 RuntimeClass |

`CONTAINER_IMAGE` / `EGRESS_GATEWAY_IMAGE` 保持通用（值改为 registry 限定名即可）。

### 4.2 Protocol 扩展（修 C1、C2、C3）

`runtime.py` 中的 `ContainerRuntime` Protocol 扩展为：

```python
class ContainerRuntime(Protocol):
    # ——既有——
    name / ensure_available / run / stop / cleanup_orphans / close

    # ——新增：堵住 C1——
    async def get_network_info(self, container_name: str) -> ContainerNetworkInfo:
        """返回沙箱的网络身份（ip），供 agent-lifecycle 事件 / identity 映射使用。"""

    # ——新增：吸收 C2 的 hasattr 探测——
    async def provision_infrastructure(self) -> InfraReport:
        """Docker：创建/复用 agent-net + egress-net（命令式）。
        K8s：校验 NetworkPolicy / RBAC / PVC 存在且符合不变量（声明式发现）。
        任何不变量不满足 → 抛 InfrastructureError，orchestrator 拒绝启动。"""

    async def verify_connectivity(self, nats_url: str, gateway: GatewayEndpoints) -> None:
        """两个 runtime 各自用自己的机制验证 agent→gateway、agent→NATS 可达。"""
```

`main.py:1724-1742` 的 `hasattr(_runtime, "ensure_agent_network")` 序列改为
统一调用 `provision_infrastructure()` + `verify_connectivity()`。

### 4.3 EgressGatewayProvider（修 C3、C7）

把 `egress/launcher.py` + `bootstrap.py` 里"如何让 gateway 存在并拿到它的
DNS IP"抽成新 Protocol（放在 `egress/provider.py`）：

```python
class EgressGatewayProvider(Protocol):
    async def ensure_running(self) -> GatewayEndpoints: ...
    # GatewayEndpoints: dns_ip / reverse_proxy_url / forward_proxy_url / nats_visible_url

class DockerEgressGatewayProvider:
    """现 launcher.py 逻辑原样搬入：镜像 inspect、双网卡 connect、健康轮询。"""

class K8sEgressGatewayProvider:
    """不启动任何东西。读取 gateway Service 的 ClusterIP（dns_ip），
    探测 /healthz；Service 不存在或不健康 → fail-closed。"""
```

`set_egress_gateway_dns_ip()` 全局变量保留（runner.py 的纯函数继续从它读），
但唯一写入方变成 provider 的返回值——业务侧完全不变。

### 4.4 拓扑假设的消除（修 C5）

`host.docker.internal` / `rewrite_loopback_to_host_gateway()` /
`_detect_proxy_bind_host()` 全部收编为 runtime 的策略对象：

```python
class HostAccessPolicy(Protocol):
    def rewrite_url_for_sandbox(self, url: str) -> str: ...
    def extra_hosts(self) -> dict[str, str]: ...
    def proxy_bind_host(self) -> str: ...
```

- **DockerHostAccessPolicy**：现行为（loopback→`host.docker.internal`、
  Linux 加 `host-gateway` ExtraHosts、docker0 探测）。
- **K8sHostAccessPolicy**：`rewrite_url_for_sandbox` 是恒等函数（orchestrator
  是 Pod，配置里本来就写 Service DNS 名，如 `nats://rolemesh-nats:4222`）；
  `extra_hosts()` 返回空；`proxy_bind_host()` 返回 `0.0.0.0`（凭证代理监听
  Pod 网卡，由 NetworkPolicy 限制谁能访问，而非 bind 地址）。

`runner.py:434-452` 的 NATS URL 重写改为调 policy，分支逻辑消失。

### 4.5 挂载翻译（修 C6）

`VolumeMount.host_path` 语义改为 **「DATA_DIR 下的逻辑路径」**（业务代码
继续生成绝对路径，不改一行）；翻译发生在 runtime 内部：

- **DockerRuntime**：恒等翻译（绝对路径直接 bind）。
- **K8sRuntime**：要求路径在 `DATA_DIR` 之下，计算
  `relpath = host_path.relative_to(DATA_DIR)`，生成
  `volumeMounts: {name: data, subPath: relpath}`，volume 指向共享 PVC。
  `DATA_DIR` 之外的路径（mount-allowlist 的额外挂载）在 K8s 模式下默认拒绝，
  允许的例外通过 values 显式声明为额外 volume（见 §6.3）。

`skill_projection.py` 不变：它写文件到 `DATA_DIR/spawns/<job_id>/skills/`，
在 K8s 下这恰好就是 PVC 里的路径，agent Pod 通过 subPath 看到同一份数据。

### 4.6 K8sRuntime 实现要点

新文件 `container/k8s_runtime.py`，依赖 `kubernetes_asyncio`（加入 pyproject
可选 extra `k8s`；docker 模式不安装也能跑——`get_runtime()` 里延迟导入，
与现在 `DockerRuntime` 的延迟导入一致）。

`ContainerSpec → Pod` 映射（关键字段）：

```yaml
metadata:
  name: {spec.name}                       # 已是 DNS-safe 的 rolemesh- 前缀
  labels:
    rolemesh.io/managed-by: orchestrator   # cleanup_orphans 的选择子
    rolemesh.io/role: agent                # NetworkPolicy 的选择子
spec:
  restartPolicy: Never
  automountServiceAccountToken: false      # agent 绝不能拿到 K8s API 凭证
  enableServiceLinks: false
  runtimeClassName: {ROLEMESH_K8S_RUNTIME_CLASS or omit}
  dnsPolicy: "None"                        # 仅 role=agent；gateway 探活等用默认
  dnsConfig: {nameservers: [spec.dns]}
  containers:
  - image: {spec.image}
    env: [...]                             # 白名单已在 runner.py 过滤
    securityContext:
      readOnlyRootFilesystem: {spec.readonly_rootfs}
      capabilities: {drop: spec.cap_drop, add: spec.cap_add}
      runAsUser/runAsGroup: {spec.user}
      allowPrivilegeEscalation: false      # ≙ no-new-privileges
      seccompProfile: {type: RuntimeDefault}
    resources:
      limits: {memory: spec.memory_limit, cpu: spec.cpu_limit}
    volumeMounts: [...]                    # §4.5 翻译结果 + tmpfs→emptyDir
  volumes:
  - name: data
    persistentVolumeClaim: {claimName: ROLEMESH_K8S_DATA_PVC}
  - name: tmp                              # spec.tmpfs 逐项转 emptyDir
    emptyDir: {medium: Memory, sizeLimit: "64Mi"}
```

`K8sContainerHandle`：
- `wait()` — `watch` Pod，`Succeeded→0`，`Failed→containerStatuses[0].state.terminated.exitCode`；
  watch 断线用 resourceVersion 续传 + 最终 read 兜底。
- `stop()` — `delete_namespaced_pod(grace_period_seconds=timeout)`。
- `read_stderr()` — pod log follow 流（语义差异见 §5.6）。

孤儿清理：`list_namespaced_pod(label_selector="rolemesh.io/managed-by=orchestrator")`
+ 镜像白名单复核（沿用 `_normalize_image_ref` 思路），双信号不变量与 Docker 版一致。

`provision_infrastructure()`（K8s 版，全是只读校验）：
1. PVC `ROLEMESH_K8S_DATA_PVC` 存在且 Bound；
2. NetworkPolicy `rolemesh-agent-default-deny` 与 `rolemesh-agent-allow-gateway`
   存在，且 podSelector 命中 `rolemesh.io/role=agent`；
3. CNI 支持 NetworkPolicy 的探测：创建一个带 deny-all 标签的探针 Pod 尝试访问
   外网 IP，必须失败（kind 默认 kindnet **不支持** NetworkPolicy——见 §8.2）；
4. RBAC 自检：SelfSubjectAccessReview 确认有 pods 的 create/delete/watch/log 权限。

### 4.7 不动的部分（明确边界）

- `scheduler.py`、`container_executor.py` 主流程（除删掉 C1 的 getattr 特例，
  改调 `runtime.get_network_info()`）、NATS IPC、safety 管道、skill 投影、
  `mount_security.py`、WebUI/channels——零改动。
- `erofs_watcher.py`：readonly-rootfs 在两个 runtime 下报错形态相同（EROFS），不动。
- evaluation CLI：**仅支持 docker 模式**（已拍板）。改用与 main.py 相同的
  provider 入口；`ROLEMESH_CONTAINER_RUNTIME=k8s` 时 CLI 直接报错退出。

---

## 5. 网络与安全模型映射（细化）

### 5.1 K8s 侧 NetworkPolicy（Helm 模板，共 4 条）

1. `agent-default-deny`：`podSelector: rolemesh.io/role=agent`，
   `policyTypes: [Egress, Ingress]`，无规则（全拒）。
2. `agent-allow-egress`：放行 agent →
   gateway Pod（53/udp+tcp、3001、3128）、→ NATS Pod（4222）、
   → orchestrator Pod（凭证代理端口，若启用）。**不放行** kube-dns——agent 的
   DNS 只能去 gateway（与 Docker 模式 `HostConfig.Dns` 等价且更强）。
3. `gateway-policy`：ingress 仅来自 agent/orchestrator；egress 放行
   53/443/80 及 values 声明的端口（gateway 自身仍有应用层 allowlist，双保险）。
4. `orchestrator-policy`：egress → K8s API、Postgres、NATS、gateway。

### 5.2 身份映射（C8）

Docker：lifecycle 事件携带 `NetworkSettings...IPAddress`。
K8s：`run()` 返回后 watch 至 `status.podIP` 非空（PodScheduled 之后立即有），
`get_network_info()` 返回 podIP。gateway 的 `IdentityResolver` 逻辑零改动——
它只消费 "ip → identity" 事件，不关心 IP 怎么来。

**注意点**：Pod IP 在 Pod 删除后可被复用，与 Docker bridge IP 行为一致，
现有 `handle_stopped()` 清理路径已覆盖；契约测试加用例锁定（§7 T-NET-3）。

### 5.3 DNS

gateway 的 DNS server 监听 `0.0.0.0:53`（容器内），两个模式都一样；
区别只在"agent 怎么被指过去"：Docker 用 `HostConfig.Dns=[bridge IP]`，
K8s 用 `dnsPolicy: None + nameservers=[Service ClusterIP]`。
ClusterIP 稳定（Service 不重建不变），比 Docker 下"gateway 重启换 IP 需重新注册"
更可靠。

### 5.4 凭证代理

K8s 下 orchestrator 是 Deployment，凭证代理端口暴露为 headless/ClusterIP
Service；`HTTP_PROXY` 注入值从 policy 拿 Service DNS 名。真实密钥仍只存在于
orchestrator Pod（来自 K8s Secret），agent Pod 只见占位符——安全模型不变。

### 5.5 gVisor

RKE2 节点装好 containerd-shim-runsc + RuntimeClass `gvisor` 后，
`ROLEMESH_K8S_RUNTIME_CLASS=gvisor` 即可；与 Docker 模式
`CONTAINER_OCI_RUNTIME=runsc` 的 fail-closed 语义一致（RuntimeClass 不存在则
Pod 创建失败）。Mac 本地两种模式都跑不了 gVisor，契约测试将其标记为
`linux-prod-only`。

### 5.6 已知语义差异（写进契约文档，不掩盖）

| 差异 | Docker | K8s | 处理 |
|---|---|---|---|
| 日志流 | 可只取 stderr | stdout/stderr 合流 | agent_runner 的协议输出走 NATS 而非 stdout，stderr 仅用于诊断日志 → 合流可接受；契约测试只断言"诊断行出现在流中" |
| pids limit | 每容器 `PidsLimit` | kubelet `podPidsLimit`（节点级） | Helm NOTES + `provision_infrastructure` 打告警；RKE2 values 文档给 kubelet 参数 |
| tmpfs uid/gid/mode 选项 | 支持 | emptyDir 不支持 mount 选项 | 镜像内以 UID 1000 运行，emptyDir 默认属主即运行用户，实测等价；契约测试 T-FS-2 锁定 |
| 容器重名 | create_or_replace 顶替 | Pod 名冲突报 409 | K8sRuntime.run() 先 delete 旧 Pod 再 create（与 Docker 行为对齐） |

---

## 6. 存储模型

### 6.1 数据面清单

`DATA_DIR` 下：`workspace/<coworker>`、`shared/`、`sessions/`、`logs/`、
`spawns/<job_id>/skills/`。写入方 = orchestrator（投影、初始化），
读写方 = agent Pod。**同一时刻同一路径只有一个 agent 写**（per-spawn 目录 +
scheduler 串行化），所以对存储一致性要求不苛刻。

### 6.2 各环境的卷方案

| 环境 | 方案 |
|---|---|
| Mac + Docker 模式 | 现状不变（bind mount） |
| Mac + kind | `hostPath` PV 绑到 kind node 的 `extraMounts`（kind 配置把宿主 `./data` 映射进 node），或单副本 `local-path` PVC——orchestrator 与 agent Pod 同节点（kind 单 node），RWO 即够 |
| RKE2 生产 | **RWX StorageClass**（Longhorn RWX / NFS / CephFS）。若集群只有 RWO：orchestrator 与 agent Pod 用 podAffinity 钉到同节点 + RWO PVC（chart 提供 `storage.mode=rwx|rwo-colocated` 两档） |

### 6.3 额外挂载（mount-allowlist）

Docker 模式不变。K8s 模式：`values.yaml` 的 `agent.extraVolumes` 显式声明
（hostPath/PVC/configMap），orchestrator 端 `mount_security.py` 依旧校验容器内
目标路径；不在声明清单里的额外挂载请求 fail-closed。

---

## 7. 契约测试设计

### 7.1 框架

```
tests/container/contract/
  conftest.py          # runtime fixture: --runtime=docker|k8s（pytest 选项）
  test_lifecycle.py    # T-LC-*: run/wait/exit code/stop/重名顶替/孤儿清理
  test_filesystem.py   # T-FS-*: readonly rootfs(EROFS)/tmpfs 可写+属主/挂载翻译 ro|rw
  test_env_security.py # T-SEC-*: env 注入/CapDrop 生效/no-new-privileges/
                       #          非 root UID/K8s 无 SA token
  test_network.py      # T-NET-*: get_network_info 返回可路由 IP/agent 无直接外网/
                       #          agent→gateway 可达/DNS 只能经 gateway/IP 复用语义
  test_streams.py      # T-IO-*:  stderr 诊断行可见/输出大小上限
```

要点：
- fixture 产出真实 runtime 实例（不 mock）：docker 模式要求本机 dockerd；
  k8s 模式要求 `KUBECONFIG` 指向 kind/任意集群且 chart 的
  `rolemesh-test` values 已安装（仅 PVC+NetworkPolicy+gateway，不装 orchestrator）。
- 测试镜像用 `rolemesh-agent` 本体 + entrypoint 覆盖为 shell 断言命令
  （`ContainerSpec.entrypoint` 已支持），不再依赖 Alpine。
- 同一用例体两边跑：`pytest tests/container/contract --runtime=docker` 与
  `--runtime=k8s`；CI 矩阵两个 job。
- 现有 `tests/container/test_docker_runtime.py`（mocked）保留为单测；
  `tests/egress/integration/` 的隔离验证逐步并入 T-NET。

### 7.2 通过标准（交付验收）

- Docker 模式：Mac M4 本机全绿。
- K8s 模式：Mac M4 上 kind（+ Calico）全绿。
- 同一份用例文件，零 `if runtime == ...` 分支（语义差异只允许出现在
  §5.6 列出的、以 marker 显式标注的用例上）。

---

## 8. 部署面

### 8.1 Helm chart（`deploy/charts/rolemesh/`）

```
deploy/charts/rolemesh/
  Chart.yaml                  # dependencies: nats（官方 chart）, postgresql（bitnami，可选 enabled=false 用外部 DB）
  values.yaml
  values-kind.yaml            # 本地 kind 档
  values-rke2.example.yaml    # 生产示例档
  templates/
    orchestrator-deployment.yaml   # replicas 固定 1（scheduler 全局并发控制是进程内的）
    orchestrator-rbac.yaml         # Role: pods create/get/list/watch/delete + pods/log；绑定 SA
    webui-deployment.yaml / webui-service.yaml / ingress.yaml
    egress-gateway-deployment.yaml / egress-gateway-service.yaml
    credential-proxy-service.yaml
    data-pvc.yaml
    networkpolicy-*.yaml           # §5.1 的 4 条
    secrets.yaml                   # LLM keys / WS_TICKET_SECRET / DB URL（支持 existingSecret）
    job-seed-admin.yaml            # ROLEMESH_SEED_ADMIN_EMAIL 的 Helm hook（可选）
  NOTES.txt                        # podPidsLimit、RWX、gVisor、CNI NetworkPolicy 支持等检查清单
```

values 关键面：`image.registry/repository/tag`（orchestrator/webui/agent/gateway 四个镜像）、
`storage.mode`、`networkPolicy.enabled`（强制默认 true，关掉时 orchestrator 启动告警）、
`gvisor.runtimeClassName`、`postgresql.enabled|externalUrl`、`nats.*`。

**RKE2 专项**：默认 CNI Canal/ Calico 均支持 NetworkPolicy ✅；私有 registry 用
`imagePullSecrets`；若启 PSA，namespace 标 `pod-security.kubernetes.io/enforce=baseline`
（agent Pod 本身满足 restricted，gateway 需要 NET_BIND_SERVICE——或把 gateway DNS
改听 1053 + Service 端口映射 53→1053，即可全 namespace restricted，**推荐后者**）。

### 8.2 本地 Mac（Apple Silicon M4）

**Docker 模式**（README Quick Start 不变）：现有流程原样保留，回归即可。

**kind 模式**：
```bash
deploy/kind/cluster.yaml      # extraMounts: ./data → node；禁用默认 CNI
deploy/kind/up.sh             # kind create cluster && 安装 Calico（kindnet 不支持
                              # NetworkPolicy，契约测试 T-NET 会 fail-closed 拦住）
container/build.sh --push-kind  # buildx 构建 arm64 镜像 + kind load docker-image
helm install rolemesh deploy/charts/rolemesh -f values-kind.yaml
pytest tests/container/contract --runtime=k8s
```

**镜像多架构**：`build.sh` 增加 `--platform`（默认本机架构；CI/发布用
`linux/amd64,linux/arm64` buildx 双架构推 registry）。M4 上 kind 与生产 RKE2
（通常 amd64）镜像架构不同，这是双架构构建必须进交付物的原因。

---

## 9. 风险与开放问题

| 风险 | 影响 | 缓解 |
|---|---|---|
| kind 默认 CNI 不支持 NetworkPolicy | 本地 k8s 验证出现"假绿"（隔离形同虚设） | `provision_infrastructure()` 主动探测 deny 是否生效（§4.6 第 3 条），探不到就拒启 |
| RWX 存储在目标 RKE2 不可用 | agent 与 orchestrator 不能共享 DATA_DIR | chart 的 `rwo-colocated` 档（podAffinity 同节点）；长期可演进为 init-container 拷贝/对象存储 |
| podPidsLimit 是节点级 | fork 炸弹防护弱于 Docker 模式 | NOTES + 启动告警；RKE2 文档给 kubelet 配置片段 |
| Pod 启动延迟 > Docker 容器启动 | per-message agent 冷启动用户可感 | 镜像预拉（DaemonSet puller 或 `imagePullPolicy: IfNotPresent` + 节点预热）；后续可做 agent Pod 池（明确不在本期范围） |
| watch 断流 / API server 抖动 | wait() 卡死或误判 | resourceVersion 续传 + 周期 read 兜底 + 现有 CONTAINER_TIMEOUT 上限兜底 |
| `aiodocker` 与 `kubernetes_asyncio` 依赖共存 | 镜像/安装体积 | 双 extra：`uv sync --extra k8s`；运行时按 `ROLEMESH_CONTAINER_RUNTIME` 延迟导入 |

**已拍板的决策**（2026-06-11 评审）：
1. agent Pod 与平台组件**同 namespace** 起步（RBAC 已按 namespace 收窄；
   分 namespace 是后续增强）。
2. evaluation CLI **仅在本地 docker 模式运行**，不支持 K8s。

---

## 10. 实施阶段与交付物

| 阶段 | 内容 | 验收 |
|---|---|---|
| **P1 解耦**（纯重构，行为不变） | C9 配置统一；Protocol 扩展（§4.2）；EgressGatewayProvider 抽出（§4.3）；HostAccessPolicy（§4.4）；挂载翻译接缝（§4.5）；删 C1/C2 绕路；契约测试框架 + docker 模式全绿 | 现有全部测试 + 新契约测试在 Docker 模式通过；README Quick Start 不变 |
| **P2 K8sRuntime** | `k8s_runtime.py` + provider + policy 三件套；kind 配置与脚本；多架构 build | 契约测试 `--runtime=k8s` 在 kind（M4）全绿 |
| **P3 Helm chart** | chart 全量模板 + 三档 values + NOTES；RKE2 部署文档 | `helm lint` + `helm template` 快照测试；kind 上 `helm install` 后端到端跑通一条 agent 会话 |

最终交付物 = P1 解耦代码 + P2 K8sRuntime + 契约测试双绿 + P3 可执行 Helm chart。

---

## 附录 A：泄漏点清单 → 修复归属（评审核对表）

评审中逐行核对出的 11 处 Docker 泄漏，按处理方式分三组。
原则：实质泄漏必须落在 §4 已有接缝里（不为单点新增抽象）；
注释/文案级不立设计条目，但列入 P1 清理清单防止遗漏。

### A.1 设计已显式点名（4）

| 位置 | 泄漏 | 归属 |
|---|---|---|
| `runner.py:435-447` | EC 开关分支拼 proxy_base / forward_proxy_url / NO_PROXY，gateway 容器名当主机名 | §4.4：改调 `HostAccessPolicy`，主机名来自 `GatewayEndpoints`，分支消失 |
| `egress/bootstrap.py:93-129` | 业务代码直接持有 aiodocker、解析 NetworkSettings | §4.3（= C3）：整体并入 `DockerEgressGatewayProvider`——aiodocker 调用不消失，搬进 docker 专属实现 |
| `container_executor.py:728-733` | `getattr(runtime, "_ensure_client")` 偷客户端 inspect | §4.2（= C1）：改调 `runtime.get_network_info()` |
| `runtime.py:202-238` | `rewrite_loopback_to_host_gateway()` 字符串替换成为公共 API | §4.4（= C5）：收编进 `DockerHostAccessPolicy`，公共模块删除 |

### A.2 已有接缝的必然结果，此处显式登记（3）

| 位置 | 泄漏 | 归属 |
|---|---|---|
| `container_executor.py:299-303` | MCP proxy URL 在 gateway 名与 `host.docker.internal` 间二选一 | `GatewayEndpoints` 增加 `reverse_proxy_base` 字段；executor 拼 MCP URL 从 endpoints 取主机，EC/回退判断收进 provider |
| `runtime.py:146` | `CONTAINER_HOST_GATEWAY = "host.docker.internal"` 公共常量 | 迁入 `DockerHostAccessPolicy` 私有；§4.4 落地后 runner/executor 的 import 清零 |
| `runner.py:319-340` | `_build_extra_hosts()`：host-gateway 映射 + metadata 黑洞 | 即 `HostAccessPolicy.extra_hosts()`。metadata 黑洞（/etc/hosts 名字劫持）是 docker 侧纵深防御；K8s 由 default-deny NetworkPolicy 承担同一职责，`K8sHostAccessPolicy` 返回空，不做 hostAliases 模拟 |

### A.3 注释/文案级——P1 顺手清理，不立设计条目（4）

| 位置 | 内容 | 处理 |
|---|---|---|
| `runner.py:577-597` | 告警文案 "Docker's default resolver" | 措辞改运行时中立。附注：该 `elif` 分支本身 fail-open（EC 开但 DNS IP 未注册→告警继续跑）；provider 化后 `dns_ip` 是 `ensure_running()` 返回值必填字段，生产路径自然关闭，测试跳过 gateway 的路径保留原告警即可 |
| `runtime.py:75-85` | `ContainerSpec` docstring 写死 Docker 行为（127.0.0.11 等） | P1 改 Protocol 时重写为中立语义；契约由契约测试定义，不靠 docstring |
| `ipc/protocol.py:27` | `McpServerSpec.url` 注释举例 `host.docker.internal` | URL 值的来源由 A.2 第一条覆盖；注释示例一并改掉 |
| `egress/bootstrap.py:16` | 流程注释 "dockerd version gate" | bootstrap 并入 `DockerEgressGatewayProvider` 后该注释留在 docker 专属文件中是恰当的；通用流程文档改措辞 |

**刻意不做的**（防过度设计）：不为日志文案建抽象、不为 docstring 建 lint、
不在 K8s 侧模拟 ExtraHosts/metadata 黑洞（NetworkPolicy 已覆盖）、
不把 `GatewayEndpoints` 泛化为服务发现框架。
