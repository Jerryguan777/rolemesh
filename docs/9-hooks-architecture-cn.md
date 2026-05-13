# 统一 hook 系统架构

本文档解释 RoleMesh 的统一 hook 系统——这套机制让审计、DLP、对话归档、审批、安全与可观测性模块能够观察并拦截 agent 活动，覆盖两个 agent backend（Claude SDK 与 Pi），而不必耦合到任意一个 backend 的原生 hook API。

目标是记录这套设计形态背后的*原因*：哪些备选方案被拒绝、我们必须抹平哪些 backend 间的不对称，以及这套系统旨在捕获哪些静默 bug。

目标读者：添加新 hook 事件、新处理器或第三个 agent backend 的开发者，以及任何在调试"为什么处理器在一个 backend 上触发但在另一个上不触发"的人。

---

## 背景：两个 backend，两种 hook 方言

RoleMesh 的 agent 运行在两套 LLM 框架之上，共享一个统一的 `AgentBackend` 协议（参见 [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md)）。每套框架都自带一套 hook 系统：

- **Claude Agent SDK** —— 通过 `ClaudeAgentOptions.hooks` 暴露 hook 回调，键为事件名 `PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`PreCompact`、`Stop` 等。每个回调接收一个 SDK 专属的 `input_data` 结构，并返回一个 SDK 专属的响应 dict（例如 `{"hookSpecificOutput": {"permissionDecision": "deny", ...}}`）。
- **Pi** —— 暴露一套扩展系统。扩展订阅 `tool_call`、`tool_result`、`session_before_compact` 等事件。每个处理器接收一个 Pi `@dataclass` 事件，并返回一个 Pi `@dataclass` 结果（例如 `ToolCallEventResult(block=True, reason=...)`）。

两个接口大体上覆盖相同的生命周期节点（工具调用之前、工具调用之后、上下文压缩之前、提示之前），但它们在以下方面存在差异：

1. **事件名** —— `PreToolUse` vs `tool_call`、`PreCompact` vs `session_before_compact`。
2. **载荷结构** —— SDK 使用带 PascalCase 键的 dict；Pi 使用带 snake_case 字段的 dataclass。
3. **响应结构** —— SDK 把权限决策嵌入 `hookSpecificOutput`；Pi 返回带类型的 dataclass。
4. **能力** —— Pi 的 `tool_result` 处理器可以彻底改写内容；SDK 的 `PostToolUse` 只能追加额外上下文。
5. **生命周期覆盖范围** —— Pi 发出 `session_before_compact`，但从不在内部调用自己的 `emit_before_agent_start`；Claude SDK 没有"上下文转换"的直接对应物。

如果我们让 RoleMesh 应用在 Coworker 跑在 Pi 上时注册 Pi 形态的处理器，跑在 Claude 上时注册 Claude 形态的处理器，就会：

- 强迫每条审计 / DLP 策略都写两遍，且返回结构各不相同。
- 冒着这种风险：在另一个 backend 上 DLP 处理器静默地变成 no-op（一个返回 `modified_input` 的处理器在 Claude 上"能用"，在 Pi 上则被静默忽略——这是一条没人在测试中能注意到的数据外泄路径）。
- 把应用层耦合到每个 SDK 的版本更迭（Pi 事件名一次重命名就会级联到每个下游处理器）。

统一 hook 层夹在应用与两个 backend 之间，对外呈现一套一致的词汇。应用只写一次处理器，就能在任一 backend 上原样运行——要么在桥接层大声失败，而不是在运行时静默失败。

---

## 设计目标

1. **一个处理器，两个 backend** —— 一个 `HookHandler` 类，其方法在容器选择 Claude 还是 Pi 时都以完全相同的方式运行。
2. **backend 中立的事件词汇** —— 6 个事件覆盖两个 backend 能力的交集。任何 SDK 专属字段都不会泄露到处理器 API 中。
3. **控制路径失败即拒，观察路径失败安全** —— 在决定"阻断或放行"时崩溃的处理器必须拒绝（审计 DB 宕机不应导致未审计的工具调用通过）；在写日志行时崩溃的处理器绝不能让 agent 中断。
4. **能力面取两个 backend 的最小公分母** —— 如果一个 backend 支持"替换工具结果"而另一个只支持"追加上下文"，那就只暴露"追加"。对外宣称给处理器的能力必须在每个 backend 上都能工作，而不是在某些 backend 上静默降级。
5. **Stop 契约得到保留** —— 每个 `run_prompt` 或 `abort` 周期恰好一个 `Stop` hook，无论 backend 内部机制如何。（参见 [`backend-stop-contract.md`](backend-stop-contract.md)。）
6. **schema 漂移大声暴露** —— 如果 Pi 或 Claude SDK 重命名了某个字段，测试应当中断，而不是事件被静默地错放位置。

---

## 六个 hook 事件

| Hook | 类型 | 能力 | 失败策略 |
|---|---|---|---|
| `PreToolUse` | 控制 | 阻断一次工具调用，或修改其输入 | **失败即拒** |
| `PostToolUse` | 观察 + 追加 | 向工具结果追加额外上下文 | 失败安全 |
| `PostToolUseFailure` | 仅观察 | 观察工具错误 | 失败安全 |
| `PreCompact` | 副作用 | 在 backend 压缩对话之前运行 | 失败安全 |
| `UserPromptSubmit` | 控制 | 阻断一条传入的用户消息，或追加上下文 | **失败即拒** |
| `Stop` | 通知 | 每个 `run_prompt`/`abort` 周期触发一次 | 失败安全 |

权威结构定义位于 `src/agent_runner/hooks/events.py`。所有事件与裁决类型都是 `@dataclass(frozen=True)`。

### 为什么 PostToolUse 上只允许"追加"？

Claude SDK 的 `PostToolUse` hook 只支持 `additionalContext`（追加到 agent 看到的结果末尾的文本）。Pi 的 `tool_result` 扩展可以整体替换内容。最早的草案在 `ToolResultVerdict` 中暴露了 `modified_result`，以利用 Pi 的能力。

我们撤销了这一设计。一个 DLP 处理器若通过返回 `modified_result="<redacted>"` 来从工具结果中抹掉秘密，**在 Pi 上能工作**，但**在 Claude 上会静默地变成 no-op**。开发者在 Pi 上测试、在生产环境用 Claude 部署，结果发布了一条永远不抹除任何东西的 DLP 规则。在安全面上的跨 backend 不对称，是最糟糕的一种静默 bug。

只暴露"追加"意味着 Pi 的能力被低估，但 Claude 的能力是契约。需要真正替换的处理器必须在 `PreToolUse` 处阻断，或者在那里重写 `tool_input`——这两种做法在两个 backend 上都能工作。

### 为什么 `StopEvent` 上用 `reason: str` 而不是 `Literal`？

一个温和的折衷。`Literal["completed", "aborted", "error"]` 带来静态类型上的好处，但会强迫每个 Stop 发射点导入一个特定的 typing 值；`str` 加上 docstring 让未来的 backend 在不引起库级别重命名的前提下扩展取值。这三个值在 `StopEvent` 上有文档说明，并在测试中有断言。

---

## 架构

```
                       application-defined handlers
                ┌──────────────────────────────────────────┐
                │  TranscriptArchiveHandler  (built-in)    │
                │  ApprovalHandler            (built-in)   │
                │  SafetyHandler              (built-in)   │
                │  (future) DLPHandler                     │
                │  (future) AuditHandler                   │
                └────────────────┬─────────────────────────┘
                                 │ register()
                                 ▼
                    ┌───────────────────────────┐
                    │       HookRegistry        │
                    │  emit_pre_tool_use        │    control ← fail-close
                    │  emit_user_prompt_submit  │    control ← fail-close
                    │  emit_post_tool_use       │    observ.  ← fail-safe
                    │  emit_post_tool_use_fail  │    observ.  ← fail-safe
                    │  emit_pre_compact         │    observ.  ← fail-safe
                    │  emit_stop                │    observ.  ← fail-safe
                    └────────────┬──────────────┘
                                 │ backend-neutral
                                 │  ToolCallEvent,
                                 │  ToolResultEvent,
                                 │  CompactionEvent, ...
                                 ▼
                ┌────────────────┴───────────────┐
                ▼                                ▼
 ┌─────────────────────────┐      ┌──────────────────────────────┐
 │  Claude Bridge          │      │  Pi Bridge                   │
 │  _build_hook_callbacks  │      │  _build_bridge_extension     │
 │                         │      │                              │
 │  produces SDK-shaped    │      │  produces a Pi Extension     │
 │  HookMatcher dict       │      │  with tool_call /            │
 │  passed to              │      │  tool_result /               │
 │  ClaudeAgentOptions     │      │  session_before_compact      │
 │  .hooks                 │      │  handlers                    │
 │                         │      │                              │
 │  Stop / UserPromptSubmit│      │  Stop / UserPromptSubmit     │
 │  emitted manually from  │      │  emitted manually from       │
 │  run_prompt / abort     │      │  run_prompt / abort /        │
 │                         │      │  handle_follow_up            │
 └────────────┬────────────┘      └──────────┬───────────────────┘
              │                              │
              ▼                              ▼
 ┌─────────────────────────┐      ┌──────────────────────────────┐
 │  claude_agent_sdk       │      │  pi.coding_agent             │
 └─────────────────────────┘      └──────────────────────────────┘
```

`ApprovalHandler` 和 `SafetyHandler` 是 hook 系统的两个最大消费者——两者都通过 `PreToolUse` 对调用进行门控或阻断。两者各有独立的架构文档（[`approval-architecture.md`](approval-architecture.md)、[`safety/safety-framework.md`](safety/safety-framework.md)）；本文档讲的是它们共享的**机制**。

### 文件布局

```
src/agent_runner/
  hooks/
    events.py                    # backend-neutral dataclasses
    registry.py                  # HookRegistry + HookHandler protocol
    handlers/
      transcript_archive.py
      approval.py
  safety/                        # safety pipeline hooks (separate package)
  claude_backend.py              # owns _build_hook_callbacks + Stop emit
  pi_backend.py                  # owns _build_bridge_extension + Stop emit
  backend.py                     # AgentBackend protocol w/ hooks param
  main.py                        # constructs HookRegistry, wires handlers
```

---

## 核心抽象：HookRegistry

```python
class HookRegistry:
    def register(self, handler: object) -> None: ...

    # Control (fail-close)
    async def emit_pre_tool_use(e: ToolCallEvent) -> ToolCallVerdict | None: ...
    async def emit_user_prompt_submit(e: UserPromptEvent) -> UserPromptVerdict | None: ...

    # Observation (fail-safe)
    async def emit_post_tool_use(e: ToolResultEvent) -> ToolResultVerdict | None: ...
    async def emit_post_tool_use_failure(e: ToolResultEvent) -> None: ...
    async def emit_pre_compact(e: CompactionEvent) -> None: ...
    async def emit_stop(e: StopEvent) -> None: ...
```

### 鸭子类型，而非继承

`register()` 接受 `object`，而不是 `HookHandler`。每个 `emit_*` 方法使用 `getattr(h, "on_<event>", None)` 来查找对应的方法。一个只关心 `PreCompact` 的处理器只需定义 `on_pre_compact`——不需要抽象基类，不需要 `NotImplementedError` 桩，也不需要"这个处理器关不关心那个事件？"的簿记。

`HookHandler` Protocol 仍在 `registry.py` 中定义并导出——它是被识别方法名的权威清单，用于 API 文档与类型提示，并非运行时检查。

### 链式语义

多个处理器可以修改同一个事件。注册表定义了它们的输出如何组合：

- **PreToolUse**：第一个 `block=True` 即短路；`modified_input` 沿链向前传递——第 `N+1` 个处理器看到的是第 `N` 个处理器返回的输入。
- **UserPromptSubmit**：阻断时同样短路；多个处理器的 `appended_context` 通过 `"\n\n"` 拼接。
- **PostToolUse**：多个处理器的 `appended_context` 通过 `"\n\n"` 拼接。不短路——每个处理器都会观察到。
- **PostToolUseFailure / PreCompact / Stop**：每个处理器都被调用；不做聚合。

### 失败即拒 vs 失败安全的实际落地

注册表自身实现了这一策略：

```python
# Control — no try/except around the handler call
async def emit_pre_tool_use(self, event):
    for h in self._handlers:
        verdict = await h.on_pre_tool_use(event)  # raises propagate
        ...

# Observation — try/except per handler
async def emit_pre_compact(self, event):
    for h in self._handlers:
        try:
            await h.on_pre_compact(event)
        except Exception as exc:
            _log.warning("pre_compact handler failed: %s", exc)
```

桥接代码承担策略的另一半：当一个控制 hook 从注册表抛出异常时，桥接器把该异常翻译为 backend 原生的"阻断"响应。该异常永远不会原样到达 SDK 或 Pi Agent。

```python
# claude_backend.py
async def pre_tool_use(input_data, tool_use_id, context):
    try:
        verdict = await hooks.emit_pre_tool_use(...)
    except Exception as exc:
        return _deny(f"Hook system error: {exc}")  # fail-close
    ...
```

---

## Pi 桥接器：时序约束

Pi 的扩展系统会在 `create_agent_session()` 时刻包装工具。我们的桥接扩展只能在 session 创建**之后**构建——它需要访问 session 的 `SessionManager`、`ModelRegistry` 等。所以桥接器需要在*构造完成后*再把自己装进去，同时仍然适用于构造期间就已经解析好的内置工具。

解决方案：传入一个**可变 ref dict** 给 `create_agent_session`，并在工具包装层中惰性解析：

- `create_agent_session(..., extension_runner_ref=ref_dict)` 保存该 dict，并用一个惰性代理包装每个工具，在每次 `execute()` 调用时读取 `ref_dict["current"]`。
- 调用方（PiBackend）在 session 创建之后构建桥接扩展，并赋值 `ref_dict["current"] = runner`。
- 如果在 execute 时该 ref 仍未绑定，工具就走直通而不经过 hook——null 安全，而不是崩溃。

这个模式（可变 ref + 惰性包装）是 Pi 集成中承重的关键部件。实现位于 `src/pi/coding_agent/core/sdk.py:_wrap_tools_lazy`，由 `test_full_chain_pi_e2e.py` 端到端地演练。导向这一方案的历史——包括 `is_not_none` vs 真值检查的 bug，以及被静默吞掉的缺失 `await`——以行内注释的形式保留在 `pi_backend.py` 的相关位置，避免下一个人重复一遍调试过程。

---

## Stop 生命周期

hook 层的契约一句话：**每个 `run_prompt` / `abort` 周期 `emit_stop` 恰好触发一次**。

- `run_prompt` → 恰好一次 Stop 发射，`reason ∈ {"completed", "error", "aborted"}`。
- `abort()` → 恰好一次 Stop 发射，`reason="aborted"`。
- 在活跃运行中被 abort → 恰好一次 Stop，由 `abort()` 拥有；`run_prompt` 的 finally 必须跳过自己的发射。

### 为什么不接到 SDK 原生的 Stop hook 上？

- Claude SDK 的 `Stop` hook 在**模型**决定停止生成时触发——每个回合可能多次触发，并带有一个 `stop_hook_active` 反循环标志。语义不对（我们关心的是"run_prompt 完成了"，而不是"模型停止流式输出了"）。
- Pi 的 `agent_end` 事件在每次 `_run_loop` 退出时触发一次，对于基于 steering 的回合来说，这与用户可见的提示完成并不一对一对应。

两个 backend 都从各自的 `run_prompt` / `abort` 路径中手动发射 Stop。每个面向用户的事件触发一次。

### 与具体实现相关的反双重发射

Claude（抢占式取消）与 Pi（协作式取消）以不同的方式协调这条单次发射不变量——Claude 使用一个从 `CancelledError` 捕获到的本地 `aborted` 标志；Pi 使用一个 `_stop_emitted_by_abort` 锁存器，它在 `abort()` 内任何 await 之前同步设置。完整的取消契约——包括各事件发生的顺序、桥接器如何保证不为已 abort 的回合发出迟到事件，以及生命周期测试覆盖的七种路径排列——位于 [`backend-stop-contract.md`](backend-stop-contract.md)。本文档只拥有 hook 层这条不变量：每个周期一次发射。

---

## backend 间的不对称（以及为什么是显式记录而不是隐藏）

Claude 与 Pi 之间存在两处能力差距，我们选择**显式记录**而不是模拟。

### 1. `PreToolUse.modified_input` 在 Claude 上可用，在 Pi 上降级

- Claude：桥接器返回 `{"hookSpecificOutput": {"updatedInput": <dict>}}`，SDK 把修改后的输入喂给工具。
- Pi：`ToolCallEventResult` 没有输入修改的槽位。我们的桥接器记录一条告警，并丢弃此修改；工具按原始输入运行。

我们本可以通过在 `_wrap_tools_lazy` 内截获工具、在调用 `inner.execute(...)` 之前重写 `params` 来在 Pi 上模拟。我们选择不这么做，因为：

- 降级是可见的：一旦处理器在 Pi 上返回 `modified_input`，就会触发一条告警日志。
- 真正*需要*确保修改生效的应用可以使用可移植的替代方案：在 `PreToolUse` 处阻断，并附上解释原因，让 agent 带着修改后的意图重试。这一模式在两个 backend 上行为完全一致。
- 增加一层进程内包装就意味着两层都必须与 Pi 的工具管线保持一致——更多的静默漂移面。

`test_hook_parity.py::test_pre_tool_use_modified_input_pi_degrades` 锁定了这一行为。

### 2. Pi 内部不发射 `before_agent_start`

Pi 的 `ExtensionRunner` 定义了 `emit_before_agent_start` 和 `emit_input`，但 **Pi 核心从不调用它们**。把 `UserPromptSubmit` 路由经过它们将会静默地不触发。Pi 桥接器改为在 `PiBackend.run_prompt()` 与 `handle_follow_up()` 中、在把文本交给 `session.prompt()` 之前，手动调用 `hooks.emit_user_prompt_submit(...)`。Claude SDK 原生的 `UserPromptSubmit` hook 在每条用户消息上都可靠地触发，所以 Claude 桥接器使用原生接入。

`test_user_prompt_submit_e2e.py` 覆盖两条路径（初始提示和后续追问）。

---

## 被拒绝的备选方案

### 为什么不暴露每个 backend 的原始 hook 面？

两套并行的 hook API，每个 backend 一套。拒绝原因：

- DLP 处理器需要同时写一个 Claude 版本和一个 Pi 版本。
- 跨 backend 的断言无法实现："这个处理器在两个 backend 上都触发了吗？"需要在两个 backend 上分别测试。
- 未来每加一个 backend 就再加一套并行 API。

### 为什么不用单一 hook 可调用对象（不要事件类型）？

`on_event(event_dict)` 加一个 `"type"` 字段，像总线订阅一样。拒绝原因：

- 没有静态类型帮助；处理器靠字符串键来分发。
- 难以记录每种字段在什么时候可用。
- 容易注册一个永远静默地匹配不到任何类型的处理器。

`HookHandler` 上的六个命名方法既提供了 IDE 自动补全，也提供了一份可 grep 的"存在哪些事件"清单。

### 为什么不通过订阅式监听器而不是 hook 来发射 Stop？

`AgentBackend.subscribe()` 机制已经把 `StoppedEvent` 投递给 NATS 桥接器。我们曾考虑让 Stop 处理器经由同一份监听器列表来观察。拒绝原因：

- `subscribe` 投递的是**UI 事件**：其顺序与投递语义是为 NATS 桥接器调优的，不适合处理器。一个慢处理器会阻塞 UI 更新。
- hook 处理器在 backend `start()` 时刻注册，其生命周期长于 `main.py` 使用的单次监听器引用。
- 失败安全：`subscribe` 监听器把异常向 backend 传播；hook 处理器需要逐个 `try/except` 包裹。

一条独立的发射路径，配上自己的隔离策略，更清晰。

---

## 测试策略

hook 系统有四层测试；每一层捕获不同类别的 bug。

| 层 | 文件 | 它能捕获什么 |
|---|---|---|
| **注册表单元** | `test_hook_registry.py` | 派发顺序错误、try/except 作用域错位、默认裁决塌缩 |
| **桥接翻译奇偶** | `test_hook_parity.py`（在 Claude / Pi 上参数化） | backend 漂移——某个桥接器在失败时忘记调用注册表 |
| **生命周期 / 边缘形态** | `test_stop_lifecycle_{claude,pi}.py`、`test_mcp_tool_names.py`、`test_claude_tool_response_shapes.py`、`test_user_prompt_submit_e2e.py` | 跨注册表 + 桥接器 + 运行时的多组件契约 |
| **schema 锁定** | `test_pi_pre_compact_schema_lock.py` | Pi 重命名事件字符串或 dataclass 字段——静默错配 |
| **全链路 E2E** | `test_full_chain_pi_e2e.py`（真实的 `create_agent_session` + 伪 provider） | 只有当扩展真正穿过 Pi 的 session 机制时才会显现的 bug |

全链路那一层捕获了 `_transform_context` 中被静默吞掉的缺失 `await`——更早的层都没有演练到那条代码路径。生命周期那一层捕获了 abort-mid-run 期间 Pi 双重发射 Stop 的问题。

未来若有一个针对桩化 `ANTHROPIC_BASE_URL` 服务器的契约测试，就能捕获 Claude SDK 自身载荷的形态漂移——目前尚未实现。

---

## 如何新增一个 hook

1. 在 `src/agent_runner/hooks/events.py` 中新增事件 + 裁决 dataclass。保持它们为 frozen 且最小化。
2. 在 `HookRegistry` 上新增 `emit_<event>` 方法，套用相应的失败即拒或失败安全模式。把对应的方法名加入 `HookHandler` Protocol。
3. 从 `src/agent_runner/hooks/__init__.py` 导出新的事件/裁决。
4. 为每个 backend 接入桥接：
   - **Claude：** 在 `_build_hook_callbacks` 中新增一个回调，并通过 `HookMatcher` 在 SDK 事件名下注册它。
   - **Pi：** 在 `_build_bridge_extension` 中新增一个挂在 Pi 事件名下的处理器，或者，如果 Pi 的事件系统没覆盖该生命周期节点，则在合适的 backend 方法中手动发射。
5. 添加一个注册表单元测试、一条桥接奇偶测试（每个 backend 一次 `@parametrize`），并在全链路文件中添加一个 E2E 场景。
6. 在本文件的"六个 hook"表中记录该 hook。

`src/agent_runner/hooks/handlers/transcript_archive.py` 中的 `TranscriptArchiveHandler` 是一个权威示例：它只使用方法子集（只有 `on_pre_compact`），并对 backend 载荷形态做分支处理。

---

## 如何新增第三个 agent backend

容器级别的派发模式参见 [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md)。在此之上的 hook 专属要求：

1. **实现 `AgentBackend.start(init, tool_ctx, mcp_servers, hooks)`**。把 `hooks is None` 当作空注册表，而不是静默禁用。
2. **路由六个事件中的每一个**经过 `hooks.emit_*`。对每个事件，二选一：要么接到 SDK 原生 hook（如果 SDK 有可靠的回调点），要么从你自己的 `run_prompt` / `abort` 手动发射。
3. **把控制 hook 的异常翻译**成桥接层处 SDK 原生的"阻断"响应。来自 `emit_pre_tool_use` 的异常绝不能原样传到你的 SDK 工具派发处。
4. **遵守 Stop 契约**：每个 `run_prompt` / `abort` 周期一次发射。如果取消是协作式的（像 Pi 那样），使用同步锁存器模式。如果是抢占式的（像 Claude 那样），使用一个本地的捕获异常的标志。完整契约——包括现有两个 backend 都已测试的七种路径排列——位于 [`backend-stop-contract.md`](backend-stop-contract.md)。
5. **添加一个 backend 专属的生命周期测试文件**（`test_stop_lifecycle_<yourbackend>.py`），覆盖与现有两者相同的七种路径排列。
6. **添加奇偶测试行**：扩展 `test_hook_parity.py` 的 parametrize，使其包含 `"<yourbackend>"`，并提供一个等价于 `_build_hook_callbacks` 的提取器。

### 各 backend 间的不对称：记录它们，而非隐藏

如果你的 backend 不能支持某项特性（例如没有 `modified_input`），在某个处理器第一次尝试使用它时记录一条告警，添加一条奇偶测试断言这种降级，并在上面的"backend 间的不对称"小节加一段说明。最糟的失败模式就是看起来在测试里能用、实则静默降级。

---

## 已知缺口

- **Claude SDK 载荷形态漂移** —— 没有针对真实的 Claude CLI 子进程跑的测试。未来一个针对桩化 `ANTHROPIC_BASE_URL` 服务器的契约测试可以捕获 Anthropic API 的形态变化。
- **Pi 的 `emit_before_agent_start` / `emit_input`** —— 在 `ExtensionRunner` 上有定义，但 Pi 核心从未调用。如果 Pi 开始在内部调用它们，我们手动的 `UserPromptSubmit` 发射就会产生重复。届时请补回归覆盖。
- **并发工具调用** —— 没有测试演练同一个 `AssistantMessage` 中包含多个工具调用（并行工具使用）的情形。hook 桥接器理应对每次工具调用各触发一次，但这条不变量没有端到端断言。
- **steering 交互** —— Pi 支持在回合中段插入 steering 消息。在一条 steering 消息上 `UserPromptSubmit` 与正在进行的回合上下文之间的交互未被显式测试。

当上述任一项以真实 bug 的形式出现时，请在相应层（单元 / 奇偶 / 生命周期 / 全链路）添加一个测试，而不是无覆盖地原地修复。

---

## 相关文档

- [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md) —— `AgentBackend` 协议、为什么有两个 backend、Pi 特有的坑
- [`backend-stop-contract.md`](backend-stop-contract.md) —— 跨 backend 的完整 abort/关停语义
- [`approval-architecture.md`](approval-architecture.md) —— `ApprovalHandler`：审批模块如何使用 `PreToolUse`
- [`safety/safety-framework.md`](safety/safety-framework.md) —— 通过 `PreToolUse` 等触发的安全管线检查
