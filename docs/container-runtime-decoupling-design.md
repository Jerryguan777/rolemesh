# 容器运行时解耦设计：`ROLEMESH_CONTAINER_RUNTIME=docker|k8s`

> 状态：设计稿 **rev3**（2026-06-12）
> 目标：同一份业务代码，通过一个环境变量在 Docker 与 Kubernetes 之间切换；
> 本地 Ubuntu（amd64）用 Docker 模式与 kind 模式验证，生产部署到
> Rancher RKE2 + Helm chart。

---

## 0. 修订历史与 rev3 要旨

| 版本 | 基线 | 内容 |
|---|---|---|
| rev1 | `0e72ae3` | 初版：Protocol 扩展 + EgressGatewayProvider + HostAccessPolicy |
| rev2 | `be953c5` | 与 main 重对齐：token 身份取代源 IP、`compute_egress_routing` 收敛 |
| **rev3** | `be953c5` | **架构转向：基础设施全面声明化、orchestrator 容器化、删除 EC=off 与宿主机拓扑** |

**rev3 核心论点**：rev1/rev2 把"声明式基础设施 + 应用只校验"原则用在了
K8s 侧，却默许 Docker 侧继续在业务进程里命令式地造网桥、启 gateway、
运行时发现 DNS IP。rev3 把同一原则**对称地**应用到两边：

1. **静态基础设施下沉到部署层**。网络、gateway、NATS、Postgres 由
   docker compose（本地）/ Helm（生产）声明；应用代码不再创建它们，
   只在启动时校验不变量，不满足即拒绝启动（fail-closed）。
2. **orchestrator 进入网络栈**（compose 服务 / K8s Deployment）。本地与
   生产的拓扑同构，"宿主机视角 vs 容器视角"的翻译层
   （`host.docker.internal`、loopback 重写、ExtraHosts host-gateway）
   失去存在前提，整体删除。
3. **删除 EC=off 模式与 hybrid 运行方式**（已拍板，2026-06-12）。
   `EGRESS_CONTROL_ENABLE` 开关及其代码分支移除——**注意：这撤销了
   main PR #84 刚恢复的 EC=off 回退**，撤销理由：回退应当是"部署另一份
   compose profile"这种部署期选择，而非业务代码里的运行时分支（#84 自己
   的提交注释印证了分支散落的代价：一处 fork 带病发布了两个版本周期）。
   现处开发阶段、无生产数据，是收编代码分支的唯一低成本窗口。
4. 应用代码保留的唯一命令式容器操作：**按 job 创建/销毁 agent 沙箱**。
   这是动态的、每请求的、真正属于应用的职责。

预期净效果：删除约 900 行命令式 provisioning / 视角翻译代码，换来
~150 行 compose YAML；启动时序 bug 类别（gateway 先于 agent、DNS IP
注册顺序——参见 #79 修的 egress-dev-nats-unreachable）整类消失。

---

## 1. 现状分析（基线 main@be953c5）

### 1.1 已有的好基础

- `ContainerRuntime` / `ContainerHandle` Protocol 与 `ContainerSpec` /
  `VolumeMount` frozen dataclass 已就位；`get_runtime()` 工厂预留 k8s 分支。
- **token 身份**（#82/#83）：spawn 时签发 HMAC token 带内传递，gateway
  无状态验签——与 L3 拓扑无关，K8s 下零改动可用。
- **路由已收敛**（#84）：`compute_egress_routing() -> EgressRouting`
  是 EC 拓扑事实的单一出口，runner/executor 统一从它取值。
- DNS 白名单平台化（#81），DNS 面与身份解耦。
- gateway 是无状态边界：镜像不含 `rolemesh.db`/`rolemesh.auth`，凭证经
  NATS RPC 回 orchestrator 解析（`RemoteCredentialResolver`）。
- agent ↔ orchestrator IPC 走 NATS（KV 初始化 + JetStream 结果流），
  不依赖 stdin/stdout。

### 1.2 rev3 要消除的耦合（按处置方式分组）

**第一组：随"基础设施声明化"直接删除的代码**

| 代码 | 现职责 | rev3 处置 |
|---|---|---|
| `container/network.py` 创建/探针逻辑（~400 行） | aiodocker 造 agent-net / egress-net、Alpine 探针 | 网络由 compose 声明；代码只留只读校验 |
| `egress/launcher.py`（~350 行） | aiodocker 启动 gateway、双网卡接线、就绪轮询 | gateway 是 compose 服务 / Helm Deployment；删除 |
| `egress/bootstrap.py`（~240 行） | 幂等启动 + inspect 拿 IP + `set_egress_gateway_dns_ip` 全局注册 | DNS IP 变成部署期静态配置；删除 |
| `runner.set_egress_gateway_dns_ip` 全局变量 | 跨模块传递运行时发现的 IP | 改为 `EGRESS_GATEWAY_DNS_IP` 配置项 |

**第二组：随"orchestrator 容器化"失去前提而删除的代码**

| 代码 | 存在理由 | rev3 处置 |
|---|---|---|
| `CONTAINER_HOST_GATEWAY` 常量 | 容器寻址宿主机 | 宿主机上无服务可寻址；删除 |
| `rewrite_loopback_to_host_gateway()` | 宿主机视角 URL → 容器视角翻译 | 全员同视角，配置直接写服务名；删除 |
| `get_host_gateway_extra_hosts()` / ExtraHosts host-gateway | Linux 上让 `host.docker.internal` 可解析 | 删除 |
| `compute_egress_routing` 的 EC=off 分支 | agent 经宿主机访问 NATS / 凭证代理 | 随 EC=off 模式删除 |
| host 侧 `start_credential_proxy`（bind 127.0.0.1 路径） | EC=off 下的凭证注入 | 删除（EC=on 的凭证注入在 gateway，经 NATS RPC 解析，保留） |
| `EGRESS_CONTROL_ENABLE` 配置 | EC 模式开关 | 删除；EC 永远开启 |

**第三组：真正保留并平台化的运行时差异**

| 关注点 | 说明 |
|---|---|
| 沙箱生命周期 | docker create/wait/log/delete vs Pod create/watch/log/delete——§4.2 |
| 挂载路径翻译 | 两个 runtime 是**同一形状**的问题（§6.1）：orchestrator 容器内路径 ≠ 沙箱挂载源路径 |
| 加固字段映射 | HostConfig ↔ securityContext——§4.5 |
| 隔离校验方式 | 校验 internal 网桥 vs 校验 NetworkPolicy——§4.4 |

### 1.3 核心判断（rev3 版）

rev1 说"C3–C8 必须从『代码做什么』提升为『运行时承诺什么』"。rev3 给出
承诺的统一载体：**部署产物（compose/Helm）承诺拓扑，应用启动时校验承诺，
契约测试锁定承诺**。网络隔离、gateway 归属、DNS、身份、存储五件事中，
身份已被 token 化解决，前三件不再需要抽象——它们退出代码；只有存储
（挂载翻译）和生命周期留在代码里，而这两件恰好是机械工作。

---

## 2. 设计原则

1. **业务代码只依赖契约，不依赖机制。**
2. **对称声明式**：两个运行时的静态基础设施都由部署产物声明；应用启动
   只做只读校验，fail-closed，不降级、不自举、不修复。
3. **单一拓扑**：orchestrator 与 agent 在同一网络栈内，全部互访走服务名；
   不存在第二种视角，因此不存在视角翻译代码。
4. **单一代码路径**：没有 EC 开关，没有部署形态分支。需要不同形态时，
   部署不同的 compose profile / values，而不是翻转环境变量走另一条代码。
5. **契约测试是契约的可执行定义**：同一套用例参数化跑两个 runtime，
   用例体内零 `if runtime == ...`。

---

## 3. 两套运行时的机制映射总表

| 契约 | Docker（compose 声明） | K8s（Helm 声明） |
|---|---|---|
| agent 无直接出网 | `internal: true` 网络 | default-deny egress NetworkPolicy |
| gateway 存在且双面 | compose 服务，挂 agent-net + egress-net 双网络 | Deployment + Service（单网卡，policy 区分内外） |
| gateway DNS 地址 | compose ipam 固定 IP → `EGRESS_GATEWAY_DNS_IP` 配置 | Service ClusterIP → 同名配置 |
| agent DNS 强制走 gateway | `HostConfig.Dns=[配置 IP]` | `dnsPolicy: None` + `dnsConfig.nameservers=[配置 IP]` |
| 请求级身份 | 签名 token 带内传递 | **同一机制，零改动** |
| 凭证解析 | gateway → NATS RPC → orchestrator（密钥不出 orchestrator/DB） | 同一机制，零改动 |
| NATS/DB 可达 | compose 服务名 | Service DNS 名 |
| 启动顺序 | `depends_on: condition: service_healthy` | readiness probe + 应用侧校验重试 |
| 沙箱生命周期 | `docker run` 等价（aiodocker） | 裸 Pod，`restartPolicy: Never` |
| 流式诊断日志 | container log (stderr) | pod log（stdout/stderr 合流，见 §5.5） |
| 孤儿清理 | name 前缀 + 镜像白名单 | label selector + 镜像白名单 |
| 资源限制 | Memory/NanoCpus/PidsLimit | resources.limits（pids 为 kubelet 级，见 §9） |
| 加固 | CapDrop/ReadonlyRootfs/no-new-privileges/tmpfs | securityContext + emptyDir(Memory) |
| gVisor | compose `runtime: runsc`（agent 沙箱经 spec.runtime） | `runtimeClassName: gvisor` |
| metadata 防护 | internal 网桥（裸 IP）+ /etc/hosts 黑洞（域名，白送的保底） | default-deny NetworkPolicy + gateway DNS 白名单（不模拟 hostAliases） |
| 存储共享 | orchestrator 与 agent bind 同一宿主目录（经路径翻译，§6.1） | 共享 PVC + subPath（同一翻译层） |

---

## 4. 代码设计

### 4.1 配置

```python
CONTAINER_RUNTIME = os.environ.get("ROLEMESH_CONTAINER_RUNTIME", "docker")
```

| 变量 | 模式 | 说明 |
|---|---|---|
| `ROLEMESH_CONTAINER_RUNTIME` | 通用 | `docker` \| `k8s`（旧 `CONTAINER_BACKEND` 删除，无兼容层——开发期无包袱） |
| `EGRESS_GATEWAY_DNS_IP` | 通用 | **静态配置**：compose 固定 IP / Service ClusterIP。取代运行时发现 |
| `EGRESS_GATEWAY_HOST` | 通用 | gateway 服务名（compose: `egress-gateway`；helm: `rolemesh-egress-gateway`） |
| `NATS_URL` 等服务地址 | 通用 | 一律写服务名；不再有 localhost 重写 |
| `ROLEMESH_HOST_DATA_DIR` | docker | DATA_DIR 在**宿主机**上的真实路径（DooD 路径翻译用，§6.1）；compose 注入 `${PWD}/data` |
| `ROLEMESH_K8S_NAMESPACE` / `_DATA_PVC` / `_IMAGE_PULL_SECRET` / `_RUNTIME_CLASS` | k8s | 同 rev2 |

删除：`EGRESS_CONTROL_ENABLE`、`CONTAINER_HOST_GATEWAY` 及全部派生逻辑。

### 4.2 `ContainerRuntime` Protocol（最终形态）

```python
class ContainerRuntime(Protocol):
    name: str
    async def ensure_available(self) -> None: ...          # API 可达 + 版本
    async def verify_infrastructure(self) -> None: ...     # 校验部署层承诺（只读，fail-closed）
    async def run(self, spec: ContainerSpec) -> ContainerHandle: ...
    async def stop(self, name: str) -> None: ...
    async def cleanup_orphans(self, ...) -> list[str]: ...
    async def close(self) -> None: ...
```

与 rev1/rev2 的区别：没有 `provision_*`（两边都不创建任何东西），没有
`get_network_info`（IP 身份已死），没有 EgressGatewayProvider /
HostAccessPolicy（前提已被消除）。`verify_infrastructure()` 两边同形：

- **Docker 版**：agent-net 存在且 `Internal=true`、egress-net 存在、
  gateway 容器健康（/healthz）、`EGRESS_GATEWAY_DNS_IP` 与 gateway 实际
  地址一致、NATS 可达。
- **K8s 版**：四条 NetworkPolicy 存在且 selector 命中、deny 实测生效
  （探针 Pod 出网必须失败——防 CNI 不支持 NetworkPolicy 的假绿）、PVC
  Bound、gateway Service/healthz、RBAC 自检（SelfSubjectAccessReview）。

### 4.3 `compute_egress_routing` 退化为单路径配置读取

EC 分支删除后，函数体收缩为：

```python
def compute_egress_routing(egress_token: str | None) -> EgressRouting:
    forward = f"http://job:{egress_token}@{EGRESS_GATEWAY_HOST}:{FORWARD_PORT}"
    return EgressRouting(
        nats_url=NATS_URL,                       # 服务名，无重写
        proxy_base=f"http://{EGRESS_GATEWAY_HOST}:{CREDENTIAL_PROXY_PORT}",
        provider_prefix=f"/proxy/{egress_token}" if egress_token else "/proxy",
        proxy_env={"HTTP_PROXY": forward, "HTTPS_PROXY": forward,
                   "NO_PROXY": f"{EGRESS_GATEWAY_HOST},localhost,127.0.0.1"},
        network_name=CONTAINER_NETWORK_NAME or None,   # k8s runtime 忽略
        dns_servers=[EGRESS_GATEWAY_DNS_IP],
        extra_hosts=_METADATA_BLACKHOLE if runtime_is_docker else {},
        mcp_proxy_host=EGRESS_GATEWAY_HOST,
    )
```

docker/k8s 跑同一段代码，差异只在配置值与两个 runtime 专属小字段
（`network_name` / `extra_hosts`），后续可进一步把这两个字段移交
ContainerSpec 组装侧——不强求，避免过度设计。`warn_missing_dns`
fail-open 路径删除：`EGRESS_GATEWAY_DNS_IP` 缺失是配置错误，启动即拒。

### 4.4 K8sRuntime 实现要点（沿袭 rev2，仅列要点）

`container/k8s_runtime.py`，依赖 `kubernetes_asyncio`（可选 extra，延迟导入）。
ContainerSpec → 裸 Pod（`restartPolicy: Never`、
`automountServiceAccountToken: false`、`enableServiceLinks: false`、
label `rolemesh.io/role=agent` + `rolemesh.io/managed-by=orchestrator`）；
加固逐字段映射 securityContext；tmpfs → emptyDir(medium=Memory, sizeLimit)；
`wait()` = watch + resourceVersion 续传 + 周期 read 兜底；重名 = 先删后建
（对齐 docker create_or_replace）；孤儿清理 = label selector + 镜像白名单。

### 4.5 orchestrator 容器化

- 新增 `container/orchestrator.Dockerfile`（Python 3.12-slim + uv sync）。
- compose 中挂载：`./src → /app/src`（开发热重载，watchfiles/reload 模式）、
  `./data → /app/data`、`/var/run/docker.sock`（仅 docker 模式；唯一用途是
  spawn agent 沙箱——provisioning 权限需求已随 rev3 消失）。
- 调试：debugpy 端口（compose 暴露 5678），替代宿主机进程直挂。
- orchestrator 同时挂 agent-net（reach NATS/gateway）与 egress-net
  （出网：LLM 安全检查等自身流量）。
- evaluation CLI（仅 docker 模式，已拍板）：作为一次性容器
  `docker compose run --rm orchestrator python -m rolemesh.evaluation ...`。

### 4.6 不动的部分

scheduler、container_executor 主流程、NATS IPC（含 orch_glue 快照 RPC、
RemoteCredentialResolver）、token_identity、dns_policy/dns_resolver、
safety 管道、skill_projection、mount_security、erofs_watcher、WebUI。

---

## 5. 网络与安全模型

### 5.1 K8s NetworkPolicy（Helm 模板，4 条）

1. `agent-default-deny`：`role=agent`，Ingress+Egress 全拒（独立存在，
   便于单独校验"默认拒绝"不变量）。
2. `agent-allow-egress`：仅放行 agent → gateway（53/udp+tcp、3001、3128）、
   → NATS（4222）。**不放行 kube-dns**。
3. `gateway-policy`：入站仅 agent/orchestrator；出站放行 **NATS（4222，
   凭证 RPC 与快照订阅必需）**、53/80/443 及 values 声明端口。
4. `orchestrator-policy`：出站 → K8s API、Postgres、NATS、gateway、
   外网 HTTPS（自身 LLM 调用）。

### 5.2 身份与凭证（沿袭 rev2）

token 带内传递，gateway 无状态验签，两个 runtime 零差异。
`EGRESS_TOKEN_SECRET` 经 compose env / K8s Secret 同时下发 orchestrator
与 gateway，绝不进 agent。真实 LLM 密钥只存在于 DB（Fernet 加密）与
orchestrator 解密路径；gateway 每请求经 NATS RPC 取用、TTL 缓存。
token TTL 须覆盖 K8s 冷启动延迟（values 校验下限）。

### 5.3 DNS

gateway resolver 统一监听 **1053**（两边一致，去掉 NET_BIND_SERVICE：
compose 端口映射 53→1053 / K8s Service port 53 → targetPort 1053，
K8s namespace 可全量 PSA restricted）。agent 的 nameserver 指向
`EGRESS_GATEWAY_DNS_IP`（静态配置）。

### 5.4 gVisor

docker：spec.runtime=runsc；K8s：runtimeClassName=gvisor。本地 Ubuntu
可装 runsc，docker 模式全链路可验；K8s 侧以 RKE2 实测为准
（`k8s-prod-only` 标记）。

### 5.5 已知语义差异（契约文档显式列出）

| 差异 | 处理 |
|---|---|
| K8s 日志 stdout/stderr 合流 | 协议输出走 NATS，stderr 仅诊断 → 可接受；用例只断言"诊断行出现" |
| pids limit 节点级 | Helm NOTES + 启动告警 + RKE2 kubelet 配置文档 |
| emptyDir 无 uid/gid 挂载选项 | 镜像 UID 1000 运行，默认属主即运行用户；T-FS 用例锁定 |
| Pod 名冲突 409 | runtime 内先删后建，对齐 docker 行为 |

---

## 6. 存储模型

### 6.1 挂载翻译（两个 runtime 同一形状）

orchestrator 容器化引入 docker-out-of-docker 路径翻译：orchestrator 在
容器内看到 `/app/data/...`，但它通过宿主机 dockerd 创建 agent 容器时，
bind 源必须是**宿主机路径**。这与 K8s 的 PVC subPath 翻译是同一问题：

```
业务代码生成：  VolumeMount(host_path=DATA_DIR / "spawns/<job>/skills")
                                │ relpath = path.relative_to(DATA_DIR)
DockerRuntime： bind 源 = ROLEMESH_HOST_DATA_DIR / relpath
K8sRuntime：    volumeMounts: {name: data, subPath: relpath}
```

业务代码（skill_projection 等）零改动；`DATA_DIR` 之外的路径在两个
runtime 下都默认拒绝，例外经部署文件显式声明（compose 额外 volume /
values `agent.extraVolumes`），`mount_security.py` 仍校验容器内目标路径。

### 6.2 各环境卷方案

| 环境 | 方案 |
|---|---|
| 本地 docker | compose 把 `./data` 挂给 orchestrator；agent bind `ROLEMESH_HOST_DATA_DIR`（=`${PWD}/data`） |
| 本地 kind | kind node `extraMounts` 映射 `./data`，hostPath PV 或 local-path PVC（单节点 RWO 即够） |
| RKE2 生产 | RWX StorageClass（Longhorn RWX/NFS）；无 RWX 时 `storage.mode=rwo-colocated`（podAffinity 同节点） |

---

## 7. 契约测试

```
tests/container/contract/
  conftest.py          # --runtime=docker|k8s；fixture 产出真实 runtime + 校验部署层已就位
  test_verify.py       # T-VER-*: verify_infrastructure 各不变量缺失时 fail-closed
  test_lifecycle.py    # T-LC-*:  run/退出码/stop/重名顶替/孤儿清理
  test_filesystem.py   # T-FS-*:  EROFS/tmpfs 可写+属主/挂载翻译 ro|rw
  test_env_security.py # T-SEC-*: env 白名单/CapDrop/非 root/无 SA token(k8s)
  test_network.py      # T-NET-*: agent 无直接外网/裸 IP metadata 不可达/
                       #          DNS 只能经 gateway/无 token 请求 407
  test_streams.py      # T-IO-*:  诊断流可见/输出上限
```

- 前置：docker 模式要求 `docker compose up` 的基础设施已起；k8s 模式要求
  chart 的 `rolemesh-test` values 已装（PVC+NetworkPolicy+gateway+NATS）。
  **测试不自建基础设施**——这本身就是对"声明式"的测试。
- 验收：Ubuntu 本机 docker 全绿（含 runsc）；同机 kind（Calico）全绿；
  同一份用例零 runtime 分支。
- 现有 `tests/egress/integration/` 中与基础设施自举相关的 fixture 改为
  依赖 compose；`test_bootstrap.py` 随 bootstrap.py 删除。

---

## 8. 部署面

### 8.1 本地：`deploy/compose/compose.yaml`（取代 README 的进程式启动）

```yaml
networks:
  agent-net:  {name: rolemesh-agent-net, internal: true,
               ipam: {config: [{subnet: 172.28.100.0/24}]}}   # 冷门网段防碰撞
  egress-net: {name: rolemesh-egress-net}
services:
  nats:     {networks: {agent-net: {aliases: [nats]}}, ...}
  postgres: {...}
  egress-gateway:
    networks:
      agent-net: {ipv4_address: 172.28.100.53}   # = EGRESS_GATEWAY_DNS_IP
      egress-net: {}
    healthcheck: {test: curl -f http://localhost:3001/healthz, ...}
  orchestrator:
    depends_on: {nats: {condition: service_healthy},
                 egress-gateway: {condition: service_healthy}, ...}
    volumes: [./src:/app/src, ./data:/app/data,
              /var/run/docker.sock:/var/run/docker.sock]
    environment: {ROLEMESH_HOST_DATA_DIR: ${PWD}/data, ...}
  webui: {...}
```

README Quick Start 变为：`container/build.sh && docker compose up`。
不设 hybrid profile（已拍板）：orchestrator 不再支持宿主机进程方式运行。

### 8.2 生产：Helm chart（`deploy/charts/rolemesh/`）

结构沿袭 rev2（orchestrator 单副本 Deployment + RBAC 仅 pods、webui、
gateway Deployment+Service、4×NetworkPolicy、PVC、Secrets 含
`EGRESS_TOKEN_SECRET`、seed Job、NOTES），更新两点：
gateway-policy 放行 NATS 出站（§5.1）；gateway DNS 走 53→1053 端口映射，
namespace 全量 PSA restricted。NATS/Postgres 为 chart 依赖，可切外部实例。

### 8.3 本地 kind

沿袭 rev2 §8.2：rootful Docker、Calico（kindnet 无 NetworkPolicy）、
inotify 预检、单架构 amd64 镜像 `kind load`、`values-kind.yaml`。

---

## 9. 风险与缓解

| 风险 | 缓解 |
|---|---|
| DooD 路径翻译配错（`ROLEMESH_HOST_DATA_DIR` 与实际不符） | `verify_infrastructure` 启动时做回环自检：orchestrator 写入 data 下哨兵文件 → 以宿主路径 bind 给探针容器读取，读不到即拒启 |
| compose 固定子网与宿主已有网络冲突 | 选冷门网段 + 文档说明可覆盖；`verify` 检查网络属性而非假设创建成功 |
| kind CNI 假绿 | deny 实测探针（§4.2），探不到拒启 |
| RWX 不可用 | `storage.mode=rwo-colocated` 档 |
| pids limit 节点级 | NOTES + 告警 + kubelet 配置文档 |
| K8s watch 断流 | resourceVersion 续传 + 周期 read + CONTAINER_TIMEOUT 兜底 |
| 撤销 #84（EC=off）引发回退顾虑 | 回退语义改由部署层承担：需要"无管控"环境时另写一份 compose 文件（不进主仓库默认路径）；业务代码不再为此分叉 |
| 开发体验变化（容器内调试） | bind-mount + reload；debugpy 5678；文档给 IDE 配置样例 |

**已拍板决策汇总**：同 namespace 起步；evaluation CLI 仅 docker 模式；
采纳 rev3 声明化方向；不设 hybrid profile；删除 EC=off（撤销 #84）。

---

## 10. 实施阶段与交付物

| 阶段 | 内容 | 验收 |
|---|---|---|
| **P1 声明化 + 解耦** | compose 文件 + orchestrator Dockerfile；删第一、二组代码（§1.2）；`verify_infrastructure` 两 stub（docker 实现 + k8s NotImplemented）；挂载翻译层；`compute_egress_routing` 单路径化；契约测试框架 | docker 模式契约测试全绿；`docker compose up` 端到端跑通一条 agent 会话；**净删码** |
| **P2 K8sRuntime** | `k8s_runtime.py` + verify K8s 版 + kind 脚本 | 契约测试 `--runtime=k8s` kind 全绿 |
| **P3 Helm chart** | chart + 三档 values + NOTES + RKE2 文档 | helm lint/template 快照；kind 上 `helm install` 端到端一条 agent 会话 |

**工作量估算（rev3 修订）**：

| 阶段 | 估算 | 备注 |
|---|---|---|
| P1 | 新增 ~700（compose 150 + verify 150 + 翻译层 80 + 契约测试 ~700 中先落 docker 侧）；**删除 ~1,100** | 生产代码净负 |
| P2 | ~1,200（k8s_runtime 650–800 + verify-k8s 100 + kind 150 + k8s 测试 fixture 300） | 较 rev2 略降（无 provider/policy 三件套） |
| P3 | ~1,100（YAML 为主） | 与 rev2 持平 |

---

## 附录 A：rev1 泄漏点清单的 rev3 终局

| 泄漏点 | rev3 处置 |
|---|---|
| #1 executor MCP 主机选择 | main #84 已解决（读 EgressRouting） |
| #2 runner proxy 拼接 EC 分支 | EC=off 删除后单路径化（§4.3） |
| #3 bootstrap 直接 aiodocker | **文件删除**（不再有东西需要 bootstrap） |
| #4 `getattr(_ensure_client)` | main #83 已随 IP 身份删除 |
| #5 `CONTAINER_HOST_GATEWAY` | **删除**（无宿主机服务可寻址） |
| #6 `rewrite_loopback_to_host_gateway` | **删除**（视角统一） |
| #7 `_build_extra_hosts` host-gateway 部分 | **删除**；metadata 黑洞保留为 docker 侧两行常量 |
| #8 "Docker's default resolver" fail-open 告警 | 分支删除：DNS IP 缺失 = 配置错误，启动即拒 |
| #9 ContainerSpec docstring Docker-isms | P1 顺手改写为中立语义 |
| #10 protocol.py 注释示例 | P1 顺手改（示例改服务名） |
| #11 "dockerd version gate" 注释 | 随 bootstrap.py 删除 |

11 处中 7 处的解法是**删除其存在前提**而非封装——这是 rev3 与
rev1/rev2 的本质区别。
