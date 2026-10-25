# 会话状态存储：emptyDir + 对象存储（设计要点）

> 状态：已定稿，待实施。本文是交给实施者的设计要点，包含背景、需求、
> 已定决策、设计、按文件的改动清单、分阶段计划和验收标准。
> 实施时以本文为准；遇到本文未覆盖或与代码现状冲突的地方，先在 PR
> 描述里写明再决定，不要静默偏离。

相关文档：[`22-container-runtime-decoupling.md`](22-container-runtime-decoupling.md)
（runtime 抽象、§7 存储模型，本文替换其 §7）、
[`23-rke2-production-deploy.md`](23-rke2-production-deploy.md)（§2.2 RWO 部署，本文之后作废）、
[`16-egress-control-architecture.md`](16-egress-control-architecture.md)（agent 出向控制，凭证不进容器的原则）、
[`19-skills-architecture.md`](19-skills-architecture.md)（skills 投影）。

---

## 1. 背景

### 1.1 现状

- 每次处理会话都会启动一个**临时的** agent 容器（K8s 下是 pod）。它空闲
  `IDLE_TIMEOUT`（默认 5 分钟）后被回收，下一条消息很可能落到一个新容器里。
- 跨容器保留的状态目前都放在一个共享数据目录 `DATA_DIR` 上：
  - K8s 下是 PVC `rolemesh-data`，以 subPath 方式挂进每个 agent pod。
  - Docker 下是 bind mount，容器化的 orchestrator 需要做 DooD 路径转换。
- 挂载内容见 `src/rolemesh/container/runner.py` 的 `build_volume_mounts`：

| 容器内路径 | DATA_DIR 下的来源 | 粒度 |
|---|---|---|
| `/workspace/group` | `tenants/{tid}/coworkers/{folder}/workspace` | **每个 coworker** |
| `/home/agent/.claude` | `tenants/{tid}/coworkers/{folder}/.claude` | **每个 coworker** |
| `/workspace/sessions` | `…/coworkers/{folder}/sessions/{conversation_id}` | 每个会话 |
| `/workspace/logs` | `…/coworkers/{folder}/logs` | 每个 coworker |
| `/workspace/shared` | `tenants/{tid}/shared`（只读，存在时才挂） | 每个租户（实际未使用） |
| skills 目录 | `spawns/{job_id}/skills`（只读） | 每次 spawn |
| `/workspace/extra/*` | `additional_mounts` | 运维配置 |

### 1.2 问题

1. **扩展性**
   - `storage.mode=rwo-colocated` 时，所有 agent 被固定在 PVC 所在的单个节点上。
   - `storage.mode=rwx` 时，所有 agent 的 IO 都压在 RWX 后端上。Longhorn RWX
     只有一个 share-manager，是单点；网络文件系统处理大量小文件也很慢。
2. **隔离性**：workspace 和 `.claude`（含 Claude 自动记忆）按 coworker 共享。
   同一个 coworker 的不同会话、不同用户会互相看到并修改对方的文件和记忆，
   带来跨用户数据泄露，以及记忆或工作区"投毒"（一次提示注入影响后续所有会话）的风险。
3. **orchestrator 被绑定在存储上**：它要往 PVC 写 settings.json、生成 skills、
   写容器日志，所以必须挂 PVC，这也是它将来做多副本的障碍之一。
4. **并发写没有定义**：同一个 coworker 的多个容器会同时写同一个目录，没有任何锁。

### 1.3 前提

- 尚未正式上线，按 **greenfield** 方式开发：**不需要数据迁移，不需要兼容已有会话**。
- memory 模块和知识库模块以后单独开发，**不在本次范围内**。
- Slack/Telegram、`group` 命名等其他历史遗留问题**不在本次范围内**
  （`/workspace/group` 的改名例外，见 §3）。

---

## 2. 需求

### 2.1 产品语义（硬性要求）

- **R1 隔离单位是会话。** 平台提供的所有持久状态（工作区文件、agent 会话记录、
  SDK 产生的任何状态）都按会话存放。**不允许**跨会话、跨用户共享。
- **R2 Claude Agent SDK 侧同样不允许跨会话持久化。** 关闭自动记忆，同时把整个
  `.claude` 目录按会话存放（靠结构隔离，不靠功能开关）。
- **R3 用户相关的长期记忆**以后由 memory 模块提供，**跨用户的能力共享**由 skills
  提供。工作区这一层不承担这两项职责。
- **R4 每次 agent 运行都必须属于某个会话**，包括定时任务和委托。
  评估（evaluation）运行例外，使用临时状态（§4.9）。

### 2.2 非功能需求

- **N1** agent pod 只用 emptyDir，不挂任何 PVC，可以调度到任意节点。
- **N2** orchestrator 不挂 PVC，代码里不再依赖 `DATA_DIR`。
- **N3** agent 容器内**不能出现**任何对象存储凭证，只能拿到短期、限定 key 的预签名 URL。
- **N4** 同一会话的状态只有一个写入者。要防止"旧容器用过期数据覆盖新数据"（§4.5 的代数号防护）。
- **N5** Docker 和 K8s 两种 runtime 用同一条状态链路。
- **N6** 保持现有安全不变式：只读根文件系统、`cap_drop ALL`、agent 默认拒绝所有出向流量、
  agent 只能访问 gateway 和 NATS，外加本次新增的 MinIO 一个地址。

---

## 3. 已定决策

| 编号 | 决策 |
|---|---|
| D1 | 采用 **emptyDir + 对象存储**。对象存储开发和生产都用 **MinIO**（公司内部云上已有实例）。代码**只依赖 S3 API**，不使用 MinIO 专有功能 |
| D2 | 网络出口用**方案 A**：在 `agent-allow-egress` NetworkPolicy 里增加一条 `ipBlock` 规则，只放行 MinIO 的网段和端口，agent 直连 MinIO，不经过 egress gateway |
| D3 | `/workspace/group` **改名为 `/workspace/conversation`**，在 P1 完成 |
| D4 | **删除** `additional_mounts`（`/workspace/extra`）以及所有 `agent.extraVolumes` 的引用 |
| D5 | **删除** `/workspace/shared` 和 `/workspace/global/CLAUDE.md` 的读取逻辑 |
| D6 | 关闭 Claude 自动记忆：`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`。settings.json 不再落盘，改为通过 SDK 的 env 注入 |
| D7 | 下载和上传由 **agent_runner 自己完成**，不用 init container 或 sidecar。预签名 URL 通过 NATS 下发，**不放进 pod spec 的 env** |
| D8 | 快照用**整目录 tar + zstd**，每一代一个对象。增量同步以后再做（P4 之后） |

---

## 4. 设计

### 4.1 容器内目录和卷

| 容器内路径 | 卷 | 内容 | 是否快照 |
|---|---|---|---|
| `/workspace` | emptyDir（工作区卷，默认 2Gi） | 包含下面两个子目录 | — |
| `/workspace/conversation` | 位于上面的卷内 | agent 的工作目录（cwd），存放所有产物，包括 agent 自己写的 `CLAUDE.md` 和 `conversations/` 下的转录归档 | **工作区快照** |
| `/workspace/sessions` | 位于上面的卷内 | Pi 后端的会话 jsonl | **agent 状态快照** |
| `/home/agent/.claude` | emptyDir（状态卷，默认 256Mi） | Claude 会话记录 `projects/…/*.jsonl`、todos 等 | **agent 状态快照**。排除 `skills/`、`shell-snapshots/`、`statsig/`、`debug/` 等可以重新生成的目录 |
| `/home/agent/.claude/skills`、`/home/agent/.pi/skills` | 位于上面的卷内或现有 tmpfs 中 | 每次启动重新下发的 skills | 否 |
| `/tmp`、`~/.cache`、`~/.config`、`~/.pi` | 现有 tmpfs，保持不变 | — | 否 |

- 不再有 `/workspace/logs`、`/workspace/shared`、`/workspace/global`、`/workspace/extra`。
- 两种 runtime 下卷的实现：
  - **K8s**：emptyDir 加 `sizeLimit`。同时给 agent 容器设置 `resources.requests/limits.ephemeral-storage`，
    让调度器把磁盘占用算进去。
  - **Docker**：用带 `uid/gid=1000`、`size=` 的 tmpfs（`ContainerSpec.tmpfs` 已经支持）。
    Docker 部署的大小通过配置单独设置，默认可以小一些。容器删除时卷随之消失。
- 目录归属：卷的根目录要让 UID 1000 可写。子目录由 agent_runner 启动时创建
  （避免 Docker 在 mount 准备阶段创建 root 拥有的中间目录，见 `ipc/skill_mount.py` 的注释）。

### 4.2 对象存储布局

```
s3://{bucket}/t/{tenant_id}/c/{conversation_id}/state/gen-{N}.tar.zst   agent 状态快照
s3://{bucket}/t/{tenant_id}/c/{conversation_id}/ws/gen-{M}.tar.zst      工作区快照
s3://{bucket}/skills/{sha256}.tar.zst                                   skills 包（按内容哈希去重）
```

- **gen（代数号）**：会话状态的版本号，每成功提交一轮就加 1。新旧版本用不同的 key 存放。
- 工作区只在有变化时才上传，所以 `M ≤ N`。数据库里的 `ws_key` 指向最近一次
  发生变化的那一代（§4.3）。
- 打包要求：
  - 符号链接保存为链接本身，不跟随链接打包。
  - 解压时拒绝绝对路径、包含 `..` 的条目、设备文件，以及指向解压根目录之外的硬链接。
  - 只保留文件的属主、权限和 mtime。

### 4.3 提交记录表 `conversation_state`（Postgres）

```
conversation_id  UUID PK  REFERENCES conversations(id) ON DELETE CASCADE
tenant_id        UUID NOT NULL          -- 启用 RLS，和其他租户表一致
generation       BIGINT NOT NULL
state_key        TEXT NOT NULL
state_sha256     TEXT NOT NULL
state_bytes      BIGINT NOT NULL
ws_key           TEXT                   -- NULL = 还没有工作区快照
ws_sha256        TEXT
ws_bytes         BIGINT
ws_manifest_hash TEXT                   -- 用于判断工作区是否有变化
updated_at       TIMESTAMPTZ NOT NULL
```

- **这一行是唯一的真相来源**，对象存储里的文件只是内容。没有被这一行引用的对象都是垃圾。
- 在 `src/rolemesh/db/schema.py` 里按现有的幂等风格建表，并调用 `_enable_rls_on`。

### 4.4 一轮的完整流程

**冷启动（容器第一次服务这个会话）**

1. orchestrator（`container_executor`）读取 `conversation_state`，为以下对象签发 **GET** 预签名 URL：
   - 当前代的 `state_key` 和 `ws_key`（存在的话）
   - 本次 skills 包
2. 把 URL 和期望的 `generation`、sha256 放进下发给 agent 的初始化数据
   （`AgentInput` / `ipc/protocol.py` 的 `AgentInitData`）里，经 NATS 下发。
3. agent_runner 启动 SDK **之前**：
   - 下载、校验 sha256、解压到对应目录。
   - 解压 skills 包到对应的 skills 目录。
   - 从当前工作区算出 `manifest_hash` 作为基线。
4. **下载或校验失败时，本轮直接报错退出**，不能在空状态上继续运行。

**轮次结束（agent_runner 发出 batch-final 标记之前，`src/agent_runner/main.py:550` 附近）**

1. 打包 agent 状态。重新计算工作区的 `manifest_hash`（基于路径、大小、mtime），
   和基线不同才打包工作区。
2. 检查体积上限（§4.8），超出时本轮以错误结束，并给出明确的提示。
3. 向 orchestrator 发 NATS 请求 `agent.{job_id}.state.upload_url`，
   拿到 gen n+1 的 **PUT** URL（只有在需要时才包含 ws 的 URL）。
4. 上传，PUT 带上 `Content-Length`，最好把内容的 sha256 也签进 URL。
5. 发 NATS 请求 `agent.{job_id}.state.commit`，内容为 `{generation: n+1, sha256, bytes, manifest_hash}`。
6. orchestrator 收到提交后：
   - 先用 HEAD 核对对象存在、大小一致。
   - 再执行条件更新：
     `UPDATE … SET generation=n+1, … WHERE conversation_id=$1 AND generation=n`。
     首轮没有记录时，用 `INSERT … ON CONFLICT DO NOTHING` 写入 `generation=1`。
   - 返回成功或冲突。
7. 提交成功后，agent_runner 才发出 batch-final（`is_final=True`）。
   回复文字照常流式显示，只有"本轮完成"的状态要等提交成功。
8. 下一轮如果复用同一个保温容器，**不需要重新下载**，本地 emptyDir 就是最新的一代。

**安全要点**
- orchestrator 必须根据 **job_id 反查**它自己记录的 conversation、tenant 和期望的代数号。
  **绝不信任** agent 上报的 conversation_id 或 key。
- PUT URL 的 key 由 orchestrator 生成。

### 4.5 一致性和代数号防护

- 同一会话同一时刻只有一个容器：`GroupQueue` 以 conversation_id 为键，现在已经保证了这一点。
  实施时要确认 P1 之后**所有**入队路径都用 conversation_id 作为键（§4.9）。
- 代数号的条件更新用来挡住**残留的旧容器**（例如 orchestrator 重启后认领到的旧 pod）。
  提交返回冲突时，agent_runner 必须以错误结束本轮，orchestrator 要停止这个容器。
- 每次提交成功后，删除早于 n-1 的旧代，保留一代用于排查。这一步是尽力而为的，
  另外由一个定期任务清理所有没有被引用的对象（§4.10）。

### 4.6 预签名 URL 和凭证

- orchestrator 持有 MinIO 凭证（放在 Secret 里），权限只限于这一个桶的读写，不需要管理员权限。
- 签名在本地计算，`boto3` 的 `generate_presigned_url` 不发网络请求。HEAD 和 DELETE 用异步客户端。
- agent 侧**不需要 S3 SDK**，用普通的 HTTP GET/PUT 即可（httpx 或标准库）。
- 有效期短，默认 300 秒，可以配置。
- **签名包含 Host。** 如果 orchestrator 和 agent 访问 MinIO 用的域名不同，需要单独配置
  "签名用的 endpoint"（`ROLEMESH_STATE_S3_PRESIGN_ENDPOINT`），
  保证 agent 实际请求的 Host 和签名时用的一致。
- **URL 不能完整写进日志**：签名参数要脱敏。

### 4.7 skills 分发

- `skill_projection.py` 不再往 `DATA_DIR/spawns/{job_id}` 写文件，改为：
  1. 在内存中生成 tar.zst，算出 sha256。
  2. HEAD `skills/{sha}.tar.zst`，不存在才上传。
  3. 把 GET URL 放进初始化数据。
- 不再需要 `cleanup_spawn_skills`。没有被引用的 skills 包由定期任务清理，或者靠存储桶的生命周期规则清理。
- skills 每次启动重新下发，**不进快照**，保证管理员的更新在下一次冷启动时生效。

### 4.8 容量和配额

| 配置 | 默认值 | 说明 |
|---|---|---|
| `ROLEMESH_AGENT_WORKSPACE_SIZE` | `2Gi` | `/workspace` 卷 |
| `ROLEMESH_AGENT_STATE_SIZE` | `256Mi` | `/home/agent/.claude` 卷 |
| `ROLEMESH_STATE_SNAPSHOT_MAX_BYTES` | `512MiB` | 压缩后单个快照的上限 |

- 起始值是经验估计。上线前用 evaluation runner 跑一组代表性任务，统计工作区和状态目录的大小以及压缩后的大小，
  再按 p99 校准：emptyDir 约为 2 倍 p99，快照上限略高于压缩后的 p99。最好支持按 coworker 覆盖。
- **K8s 上 emptyDir 写满时，pod 会被驱逐，进程不会收到"磁盘已满"的错误。** 所以 agent_runner 要：
  - 定期检查用量，超过约 80% 时向 agent 注入一条系统提示，要求清理文件。
  - 超过快照上限时，提交失败并给出明确的错误。

### 4.9 会话归属的统一

- **定时任务**：`scheduled_tasks.conversation_id` 改为 `NOT NULL`。
  创建任务时如果没有指定会话，就创建一个专属的"任务会话"。
  涉及的路径有 agent 的 schedule_task IPC、Web API，以及其他创建入口，实施时要用 grep 全部找出来。
  这样 `task_scheduler.py:115` 的 `task.conversation_id or task.coworker_id` 回退分支就可以删掉。
  `context_mode` 只影响是否续接 Claude session，工作区始终是任务会话自己的。
- **委托**：子会话本来就是独立的会话，不用改。
- **评估（evaluation）**：初始化数据里不带状态信息时，agent_runner 跳过下载和提交，使用临时状态。
  `evaluation/runner.py` 里 `conversation_id=group_folder` 这种不是真实会话的用法，继续走临时状态模式。

### 4.10 生命周期

- 删除会话时（`db/chat.py:585` 的 `delete_conversation`），也要删除对象存储里的
  `t/{tid}/c/{cid}/` 前缀。这一步异步执行，失败可以重试，最终靠清理任务兜底。
- 清理任务（orchestrator 后台运行）：
  - 删除没有被 `conversation_state` 引用、且超过一定时间的 state/ws 对象。
  - 删除没有被引用的 skills 包。
  - 先实现成简单的定期扫描即可。
- 长期不活跃会话的归档策略以后再定，不在本次范围内。

### 4.11 网络（方案 A）

- **Chart**：在 values 里增加 `stateStore.endpoint`、`stateStore.port`、`stateStore.cidrs`（MinIO 的网段列表）。
  在 `templates/networkpolicy.yaml` 的 `agent-allow-egress` 里增加：
  ```yaml
  - to:
      - ipBlock: { cidr: <每个 stateStore.cidrs 条目> }
    ports:
      - { protocol: TCP, port: <stateStore.port> }
  ```
  MinIO 在集群外部，Cilium 上的 `ipBlock` 对离开集群的流量是生效的（参见 `cilium-networkpolicy.yaml` 的说明）。
  `verify_infrastructure` 的网络策略检查只看选择器，不看具体规则，**不需要修改**。
- **DNS**：agent 只能通过 gateway 的 DNS 解析器解析域名，所以要把 MinIO 的域名加入 gateway 的
  `EGRESS_DNS_ALLOWLIST`（`egress/dns_policy.py`，平台级配置）。
- **代理**：在 `runner.py` `compute_egress_routing` 生成的 `NO_PROXY` 里加上 MinIO 的域名，
  让 agent 绕过 forward proxy 直连。
- compose 和 Docker 下：MinIO 作为 compose 里的一个服务，加入 agent 网络。

### 4.12 可观测性

- 指标或结构化日志：
  - `state_restore_ms`、`state_commit_ms`
  - `state_snapshot_bytes`（state 和 ws 分开统计）
  - `state_commit_failures`
  - `state_commit_conflicts`：正常情况下应该接近 0，出现就说明有残留容器
  - `state_skip_ws_upload`：工作区未变化、跳过上传的次数
- orchestrator 原来写在 `logs_dir` 下的容器日志文件（`container_executor.py:623`、`:670`），
  改为通过结构化日志输出到 stdout。

### 4.13 orchestrator 启动检查

- 两种 runtime 的 `verify_infrastructure` 都要：
  - 删除 PVC 检查（`k8s_runtime.py:1069` `_check_pvc_bound`）。
  - 新增对象存储检查：桶存在、可以写入和删除一个探测对象。失败时启动直接报错（fail-closed）。

---

## 5. 按文件的改动清单（供实施者参考，以实际 grep 结果为准）

**agent_runner（`src/agent_runner/`）**
- 新增 `state_sync.py`（或类似名称）：下载、校验、安全解压、打包、manifest 哈希、排除规则、
  体积检查、上传、提交请求。
- `main.py`：启动时恢复状态；在 batch-final 之前提交；评估模式跳过。
- `claude_backend.py`：
  - `cwd="/workspace/conversation"`
  - 删除 `/workspace/global` 和 `/workspace/extra` 的逻辑
  - 原 settings.json 里的环境变量并入 `self._sdk_env`，其中 `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`
- `pi_backend.py`：
  - `cwd` 改名
  - 删除 `/workspace/global` 的逻辑
  - `/workspace/sessions` 保持不变
- `hooks/handlers/transcript_archive.py`：`_ARCHIVE_DIR` 改为 `/workspace/conversation/conversations`。

**orchestrator（`src/rolemesh/`）**
- 新增 `state/`（或放进 `container/`）：
  - `StateStore` 协议，方法包括 `presign_get`、`presign_put`、`head`、`delete_prefix`、`put_bytes`
  - 基于 S3 的实现
  - `conversation_state` 的数据库访问
  - NATS 处理函数 `state.upload_url` 和 `state.commit`
  - 清理任务
- `container/runner.py`：
  - `build_volume_mounts` 删除，或者精简到只剩卷声明
  - 删除 settings.json、shared、logs、additional_mounts 的逻辑
  - `NO_PROXY` 加上 MinIO 域名
- `agent/container_executor.py`：
  - 不再创建 coworker 目录
  - 签发 URL 并填入初始化数据
  - 日志改走 stdout
  - 删除 `cleanup_spawn_skills` 的调用
- `container/skill_projection.py`：改为上传到对象存储（§4.7）。
- `agent/executor.py`：
  - `AgentInput` 和 `AgentBackendConfig` 增加状态字段
  - 删除 `extra_mounts`
  - 检查 `skip_claude_session` 是否还需要
- `ipc/protocol.py`：`AgentInitData` 增加状态字段，例如
  `state: {generation, state_get_url, state_sha256, ws_get_url, ws_sha256, skills_get_url, skills_sha256} | None`。
- `security/mount_security.py`、`db/coworker.py` 的 `additional_mounts`、`core/types.py` 里相关的
  `container_config` 字段：删除。
- `core/config.py`：
  - 删除 `DATA_DIR` 的使用方、`ROLEMESH_HOST_DATA_DIR`、`ROLEMESH_K8S_DATA_PVC`、`ROLEMESH_K8S_AGENT_PIN_NODE`
  - 新增 `ROLEMESH_STATE_S3_*` 配置：endpoint、presign endpoint、bucket、access key、secret key、
    region、path-style、presign TTL，以及 §4.8 的容量配置
- `container/k8s_runtime.py`：
  - 删除 PVC subPath 转换（`_mount_subpath`）和 `pin_node`
  - 改为生成 emptyDir 和 ephemeral-storage 设置
  - 更新 `verify_infrastructure`
- `container/docker_runtime.py`：
  - 删除 DooD 路径转换（`_translate_bind_source`、`_translate_mounts`）和哨兵文件校验
  - 改为用 tmpfs 卷
- `container/runtime.py`：`ContainerSpec` 和 `VolumeMount` 视需要简化，比如新增 `scratch_volumes: dict[path, size]`。
- `db/schema.py`：新建 `conversation_state` 表；`scheduled_tasks.conversation_id` 改为 `NOT NULL`。
- `db/chat.py`：`delete_conversation` 时触发前缀删除。
- `orchestration/task_scheduler.py` 和任务创建路径：保证每个任务都有会话（§4.9）。

**部署**
- `deploy/charts/rolemesh/`：
  - 删除 `templates/pvc.yaml`、`storage.*`，以及 orchestrator 的数据卷和相关 env
  - 新增 `stateStore.*`，MinIO 凭证放进 Secret
  - NetworkPolicy 增加 §4.11 的规则
  - gateway 的 DNS 白名单
  - 更新 `NOTES.txt`
- `deploy/compose/compose.yaml`：增加 MinIO 服务和初始化桶的步骤；去掉 `./data` 挂载和 `ROLEMESH_HOST_DATA_DIR`。
- `deploy/kind/`：去掉 extraMounts 和数据 PVC；kind 环境里部署一个 MinIO（Deployment 或 Helm）用于测试。
- `container/Dockerfile`：
  - `mkdir -p /workspace/group /workspace/global /workspace/extra` 改为只创建需要的目录
  - `WORKDIR` 改为 `/workspace/conversation`
- `scripts/verify-hardening.sh`：路径改名。

**文档**
- `docs/22` §7 改为引用本文；`docs/23` §2.2 删除，换成 MinIO 和网络配置说明；
  `docs/19` 的 skills 投影部分；`docs/3` 里涉及挂载的描述。中英文两版都要改。

**测试**
- 需要修改：`tests/container/test_runner.py`、`tests/test_multi_tenant_e2e.py`、`tests/container/contract/*`
  （尤其是 `test_filesystem.py`）、`tests/container/test_k8s_runtime.py`、`test_docker_runtime.py`、
  `test_dood_translation.py`（删除）。
- 需要新增的测试见 §7。

---

## 6. 分阶段实施和验收标准

每个阶段一个或多个 PR。每个 PR 都要通过 `ruff`、`mypy --strict` 和现有的单元测试。

### P1 语义收紧（仍然使用现有的 DATA_DIR / PVC）
- `/workspace/group` 改名为 `/workspace/conversation`，涉及：两个后端的 cwd、runner 挂载、
  transcript_archive、Dockerfile、verify-hardening.sh、测试、文档。
- 挂载改为按会话存放：`tenants/{tid}/conversations/{conversation_id}/{workspace,claude,sessions}`，
  对应 `/workspace/conversation`、`/home/agent/.claude`、`/workspace/sessions`。
- 关闭自动记忆，settings 改为 env 注入，不再写 settings.json。
- 删除 shared、global、extra（additional_mounts）、logs 挂载；容器日志改走 stdout。
- 每个定时任务都要有会话（§4.9）。

**验收**：
- 现有测试全部通过（按改名更新）。
- 新增测试证明：同一个 coworker 的两个会话，挂载的工作区和 `.claude` 来源路径不同。
- grep 确认 `src/` 中没有 `/workspace/group`、`additional_mounts`、`workspace/shared`、`workspace/global`。

### P2 状态链路（Docker 加 compose 里的 MinIO 先跑通）
- `StateStore` 和 S3 实现、`conversation_state` 表、NATS 的上传 URL 和提交接口、
  agent_runner 的恢复和提交、skills 改为对象存储分发、启动检查。
- 这一阶段持久状态从 DATA_DIR 切换到对象存储。工作区卷改为 tmpfs 或 emptyDir。

**验收**（§7 的测试全部通过）：
- 冷启动能恢复。
- 会话被回收后，第二轮能接着上一轮继续：Claude session 能续接，文件都还在。
- 工作区没有变化时不上传。
- 残留容器的提交被拒绝。

### P3 去掉 PVC 和 DATA_DIR
- K8s runtime 改为 emptyDir；删除 PVC、`DATA_DIR`、subPath 转换、DooD 转换、`PIN_NODE`。
- 修改 chart（stateStore、NetworkPolicy、DNS 白名单）、compose、kind 和文档。

**验收**：
- 在多节点 kind 集群里，同一租户的多个会话的 agent pod 分散在不同节点上运行，状态续接正常。
- orchestrator pod 不挂任何 PVC。
- grep 确认 `src/` 中没有 `DATA_DIR`。
- 容器契约测试通过。

### P4 打磨
- 旧代清理和清理任务、体积用量警告、指标、冷启动耗时测量。
- 用 evaluation 样本统计实际大小，校准 §4.8 的默认值。

---

## 7. 测试要求（P2 起）

- **单元测试**：
  - 打包和解压的往返一致性。
  - 安全解压：拒绝 `..`、绝对路径、指向外部的符号链接或硬链接、设备文件。
  - 排除规则；manifest 哈希能检测出新增、修改、删除。
  - 体积上限。
- **orchestrator 的提交处理**：
  - HEAD 校验失败时拒绝。
  - 代数号冲突返回 conflict。
  - 首轮插入。
  - 用不属于自己的 job_id 不能提交到别的会话。
  - URL 的 key 只能由服务端生成。
- **失败语义**：
  - 下载失败时本轮报错，并且没有启动 SDK。
  - 上传失败时本轮报错，`generation` 不变。
  - 轮次中途崩溃后，下一次冷启动回到上一代。
- **集成测试**（Docker 加 MinIO，标记为 `integration`）：
  - 两轮对话之间强制回收容器，第二轮能读到第一轮生成的文件，Claude session 能续接。
  - 同一个 coworker 的两个会话互相看不到对方的文件。
- **安全**：
  - agent 容器的 env、文件系统和进程参数里都找不到 MinIO 凭证。
  - 日志中的 URL 已经脱敏。
- **K8s**（kind 环境，P3）：pod 规格里没有 PVC，有 emptyDir 的 `sizeLimit` 和 ephemeral-storage 设置；
  NetworkPolicy 里包含 MinIO 的规则。

---

## 8. 不在本次范围内

- memory 模块、知识库或 RAG、跨会话的文件共享或"发布到 coworker"。
- 委托时的文件交接。
- 工作区增量同步、节点本地缓存、按需拉取。
- 长期不活跃会话的归档策略。
- orchestrator 多副本。
- Slack/Telegram 的删除，以及除 `/workspace/group` 之外的 group 命名清理。

## 9. 实施前需要先确认的事实

实施者在动手前先验证以下几点，结论写进 PR 描述：

1. Claude Code 在 `~/.claude` 下的实际目录结构（会话记录路径、自动记忆路径、哪些是可以丢弃的缓存），
   据此确定 §4.1 的排除列表。可以在容器里跑一轮之后 `find ~/.claude` 查看。
2. `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` 通过 `ClaudeAgentOptions(env=…)` 传入后确实生效。
3. 改名后 Claude session 的续接正常：同一个容器内续接，以及恢复快照后在新容器里续接。
4. 所有入队路径（消息、定时任务、委托）在 P1 之后都以 conversation_id 作为队列键。
5. 公司内部 MinIO 的访问域名、网段、端口；agent 和 orchestrator 使用的域名是否一致（§4.6）；
   是否开启了 TLS 以及 CA 证书如何分发给 agent。
