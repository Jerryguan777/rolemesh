# 评测模块架构

本文档介绍 RoleMesh 的评测模块——一种让 coworker 作者针对垂直领域任务数据集，量化测量、对比、并迭代 AI coworker 质量的机制。

文档涵盖了为什么评测数据放在业务数据库之外、为什么把 Inspect AI 作为 library 复用而非整体采用、rolemesh 专属 runner 与借用的 scoring 层之间的职责拆分，以及让首版保持精简的分阶段发布策略。

目标读者：添加新 scorer、构建 CLI 命令、调试某个 case 分数为何异常、接入新数据集源、或扩展分析工具链的开发者。

---

## 背景：为什么需要一个评测模块

RoleMesh 中的 coworker 由 system prompt、一组 MCP 工具绑定、一份 skill 清单、agent backend、以及一份权限 profile 组合而成。当这五者全部接通之后，对作者唯一诚实的问题是：**这玩意儿到底能不能干活？**

如今没有量化的答案。coworker 设计者只能凭手感迭代：重写一个 skill、在 WebUI 里和 agent 聊一会、肉眼判断回复、重复。这种循环有三个失效模式：

- **没有回归信号。** 改好一个任务往往会弄坏另一个。如果没有 baseline 可供 diff，破坏在抵达生产之前完全不可见。
- **没有共同词汇。** "感觉变好了"不是一个能挺过 sprint review 或模型升级的数字。
- **没有 CI 关口。** 一次模型替换、一次 skill 编辑、一次 MCP server 改动——任何一项都可能悄无声息地降级行为。如果没有一份固定数据集上的自动检查，回归会直接发版。

需要的是这样一种能力：让一个 coworker 跑过一组精选任务，在重要的维度上为结果打分（是否答对、是否调对了工具、消耗多少），并随时间对比不同的 run。这就是本模块所提供的。

适合 RoleMesh 的形态：

- 复用现有 `ContainerAgentExecutor`，确保评测的 run 与生产的 run 按位一致。
- 订阅现有的 `BackendEvent` 事件流——不引入新 IPC。
- 每个 run 启动时对 coworker 做快照，保证历史 run 可复现。
- 把结果存在业务数据库之外——评测是开发期关注点，不是租户的业务产物。
- 复用 Inspect AI 的打分原语与日志格式，免费获得一个成熟的 web viewer。

---

## 设计目标

1. **量化最重要的两件事。** 主要指标是*结果准确率*（agent 的回复是否满足任务）和*工具调用准确率*（是否以正确的参数、正确的顺序调用了正确的 MCP 工具）。延迟、token、成本是次要但始终记录的指标。
2. **可复现的 run。** 用同一个数据集版本、同一份 coworker 快照重跑，必须得到可比较的分数。数据集不可变 + 版本化；coworker 配置在 run 启动时快照进 run 目录。
3. **对比 run。** 迭代循环要求 diff："我改了 skill 后，`crud` 任务有没有变好，同时 `query` 任务有没有变坏？"。聚合 delta 加 per-case regression 高亮是一等公民。
4. **CI 友好。** `--fail-if "result_accuracy<0.7"` flag 返回非零退出码，让回归套件可以 gate 一次合并。
5. **架构层面就多租户。** 所有路径按 `tenant_id` 分区。评测数据从不跨租户。
6. **不污染业务数据库。** 评测是开发产物。仅使用文件系统，直到未来确有需求证明必要。
7. **复用，不重建。** Inspect AI 已经有成熟方案的地方（scorer 函数、log 格式、web viewer），作为 library 依赖使用。只构建 rolemesh 专属的东西（容器编排、MCP 集成、多租户路径、面向 agent 开发者的 CLI 体验）。

---

## 非目标

- **本次迭代不做 WebUI。** 所有交互通过 CLI；深度查看委托给 `inspect view`（Inspect AI 自带的 web viewer）。rolemesh 原生 dashboard 后续可能会做，但不在本次范围。
- **不做生产监控。** 评测服务于设计循环，不服务于运行时 SLO 跟踪。没有"对线上流量持续评测"的路径。
- **不做自动 prompt 调优。** 本模块测量，不优化。
- **不做数据集管理 UI。** 数据集存在独立 git 仓库，以 JSONL + YAML manifest 形式，按普通 git 工作流编辑（PR review、tag 做版本）。详见下文 `Datasets`。
- **不做跨租户分析。** 每个租户在自己的隔离空间里分析自己的 run。

---

## 已考虑的备选方案

### 框架选择：采用 vs 复用 vs 自建

#### 方案 A —— 整体采用 Inspect AI

将 rolemesh 评测定义成 Inspect AI 的 task（`@task`、`@solver`）。编排归他们；我们插入一个调容器的 `solver`。

**优点**

- 继承他们的整个生态（scorer、viewer、log、eval-as-code 模式）。
- 维护面更小。

**缺点**

- 他们的 `solver` 模型假定是一个调模型的函数；我们的 solver 需要启动 Docker 容器、装配 MCP 绑定、接入 credential proxy、驱动 NATS 事件流。在最关键的编排层，阻抗严重不匹配。
- 他们的 CLI（`inspect eval task.py`）面向 safety 研究员——每次评测写一个 Python task。我们的用户是 agent 开发者，想要声明式数据集和一行 `eval run` 命令。
- 没有多租户路径、coworker 快照、MCP override 的一等概念。

**否决。** 在最关键的编排层契合度差。

#### 方案 B —— 采用 LangSmith / Braintrust

商业评测平台，自带数据集管理、run viewer、diff 支持。

**优点**

- 体验成熟、托管基础设施、无需自建。

**缺点**

- 仅 SaaS——会把 coworker prompt、工具调用、（间接地）租户数据发给第三方。与 RoleMesh 面向的私有化部署形态不兼容。
- 数据集与 run 格式的 vendor lock-in。
- 难以与基于容器的执行模型集成——这些平台假定它们的 SDK 在 agent 进程内。

**否决。** 与项目自托管、AGPL 许可的姿态冲突。

#### 方案 C —— 全自建

scorer、log 格式、viewer 全部在 in-tree 实现。

**优点**

- 对 schema 和 UX 完全可控。

**缺点**

- 打分原语（exact 匹配、includes、LLM-as-judge with model-graded fact）已经被充分理解，重写不值得。
- 自建 web log viewer 本身就是一个数周的项目。
- 与更广的评测工具生态无互操作性。

**否决。** Inspect AI 已经提供了扎实的原语，在那里重写是浪费。

#### 方案 D —— 把 Inspect AI 作为 library 复用，自建 rolemesh 专属层（选中）

- 引入 `inspect_ai.scorer` 作为打分函数。
- 把 Inspect AI 的 `EvalLog` 格式作为次要输出写出来，这样我们的 run 可以直接被 `inspect view` 打开。
- 其余一切（orchestrator、recorder、CLI、数据集 loader、diff、doctor）在 in-tree 围绕 rolemesh 的执行模型自建。

**优点**

- 通过 `inspect view <run>/run.eval` 免费获得 web viewer。
- 经过实战验证的打分原语。
- 在我们的模型与他们不同的编排层，完全可控。
- 不依赖 SaaS。

**缺点**

- 需要同步两种日志格式（rolemesh 规范化 JSON 与派生的 Inspect `.eval`）。
- 依赖一个第三方 library 的演进。

**选中。** 在我们的形态下，buy 与 build 之间的最优切分。

### 结果存储：数据库 vs 文件系统

业务数据库存的是租户产物：coworker、conversation、scheduled task、审计日志。把 evaluation run 塞进去会混合两个无关的生命周期——运行时业务状态 vs 开发期实验产物。两者访问模式也不同：run 是写多读少，且（对大 transcript 而言）体量较大。

业界实践（MLflow、W&B、Langfuse、Inspect AI）一致地把 metadata 与 artefact 分离，并都不放业务 OLTP 数据库里。具体而言：

- **MLflow**——metadata 在专门的 backing store，artefact 在文件系统或对象存储。
- **Inspect AI**——每次 run 一个自描述 `.eval` 文件，纯文件系统。
- **Langfuse**——独立 Postgres + ClickHouse，从不用业务 DB。

在我们的规模下（每个 coworker 每月数十到数百次 run、每个数据集数十个 case），纯文件系统布局已足够。未来若有跨 run 聚合需求，可以加一个 side index（DuckDB 或 SQLite over JSON 文件），完全不触碰业务 Postgres。

**决策**：`~/.rolemesh/eval/{tenant_id}/runs/{run_id}/` 是规范化存储。一个 run 一个目录；归档、分享、清理都按目录操作。不新增 DB schema。

### 数据集存储：库内 vs 外部

数据集是一份由专家精选的任务，独立于代码演进。把它内嵌进 rolemesh 仓库会耦合两个本该分离的生命周期：数据集编辑不应要求 rolemesh 发版；rolemesh 改动也不应让一个数据集版本失效。

生态里所有成熟的 agent benchmark（GAIA、SWE-bench、τ-bench、HumanEval、AgentBench）都把数据集放在独立 repo 或 registry——通常是纯 JSONL 加 manifest，偶尔是 HuggingFace Hub。没有一个把数据集内嵌进框架代码。

**决策**：数据集放在外部 git 仓库（例如 `rolemesh-eval-datasets`）。rolemesh loader 解析 `file://path/`、`<local-path>` 或 `git+https://repo#subpath@version` 形式的 URI。版本化通过 git tag 或 manifest `version` 字段。未来加 HuggingFace Hub loader 不需要改协议。

### Judge 执行位置：容器内 vs 宿主进程

LLM-as-judge（启用时）是一个宿主侧的分析步骤，不是被测 agent 的一部分。它运行在 rolemesh CLI 进程内，发生在 agent 的 run 完成之后。credential proxy 存在的目的是把 API key 隔离在 agent 容器之外——它与宿主进程代码无关。

**决策**：judge 直接使用官方 Anthropic SDK，从环境变量或 rolemesh 宿主配置读取 `ANTHROPIC_API_KEY`。**不**经过 credential proxy。

---

## 架构

### 模块布局

```
src/rolemesh/eval/
├── types.py              # 纯 dataclass（Case、Run、CaseResult、Snapshot）
├── paths.py              # run/case 文件的路径约定
├── loader/               # DatasetLoader，按 URI scheme 派发
│   ├── local.py          # file:// 和本地路径
│   └── git.py            # git+https://
├── orchestrator.py       # EvalOrchestrator：per-case 分派、并发、retry
├── case_executor.py      # EvalCaseExecutor：包装 ContainerAgentExecutor
├── recorder.py           # EvalEventRecorder：订阅 BackendEvent，写 case 文件
├── scorers/              # Scorer Protocol + 实现
│   ├── result.py         # exact / contains / regex / json_match / llm_judge
│   ├── tool_calls.py     # precision / recall / order / args 对齐
│   └── perf.py           # latency / tokens / cost
├── judge.py              # LLM-as-judge 客户端（宿主侧，直连 Anthropic SDK）
├── aggregator.py         # Run 级聚合、groupby tag、failure mode
├── failure_mode.py       # 失败分类
├── inspect_export.py     # 转 Inspect AI EvalLog 格式
├── doctor.py             # 预检
├── validator.py          # 数据集 schema 校验
└── cli.py                # `rolemesh eval` 子命令派发
```

对现有代码只有一处改动：在 `src/agent_runner/backend.py` 的事件 union 里加入 `UsageEvent`，两个 backend 都 emit 它。其余全部是增量。

### 数据流

```
  rolemesh eval run --coworker <id> --dataset <uri>
            │
            ▼
  ┌──────────────────────────────────────────────────────────┐
  │  EvalOrchestrator                                         │
  │   1. 快照 Coworker（system_prompt、MCP servers、         │
  │      skill 文件内容、permissions）→ run 目录              │
  │   2. DatasetLoader.load(uri) → EvalDataset（不可变）      │
  │   3. asyncio.Semaphore(N) 控制 per-case 并发              │
  └────────────┬─────────────────────────────────────────────┘
               │ per case
               ▼
  ┌──────────────────────────────────────────────────────────┐
  │  EvalCaseExecutor                                         │
  │   构造 AgentInput、实例化 ContainerAgentExecutor          │
  │  （和生产同一条代码路径）、注册 EventRecorder、           │
  │   等待 terminal status                                    │
  └────────────┬─────────────────────────────────────────────┘
               │ BackendEvent 事件流
               ▼
  ┌──────────────────────────────────────────────────────────┐
  │  EvalEventRecorder                                        │
  │   ToolUseEvent   → actual_tool_calls（含 ts offset）      │
  │   ResultEvent    → actual_result                          │
  │   UsageEvent     → tokens、cost                           │
  │   monotonic 时间 → latency_ms、ttft_ms                    │
  │   case 完成时：写 cases/<id>.result.json、                │
  │   cases/<id>.trace.jsonl                                  │
  └────────────┬─────────────────────────────────────────────┘
               │
               ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Scorers（per case）                                      │
  │   ResultScorer        （mode 由 case.expected_result 决定） │
  │   ToolCallScorer      （precision / recall / order / args）│
  │   PerformanceScorer   （latency / tokens / cost）          │
  │   FailureModeClassifier                                   │
  └────────────┬─────────────────────────────────────────────┘
               │
               ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Aggregator                                               │
  │   组装 run.json（聚合指标、per-tag 切片）                 │
  │   写 Inspect 兼容的 run.eval（次要输出）                  │
  └──────────────────────────────────────────────────────────┘
```

### 复用现有基础设施

Runner 不发明任何新的执行路径。具体：

- **`ContainerAgentExecutor`** 原封不动地实例化。评测的 run 用与生产相同的 Docker 镜像、相同的 MCP credential proxy、相同的 NATS 桥。
- **`BackendEvent`** 是唯一使用的事件流；recorder 只是一个新订阅者。
- **`structlog`** 是唯一的 logger。
- **租户作用域**复用 `AgentInput` 中现有的 `tenant_id` 传递。

唯一新增的事件类型 `UsageEvent` 携带每条 assistant 消息的 token 数与成本。两个 backend 都 emit 它（Pi 从 `AssistantMessageEvent.usage` 读；Claude 从 SDK result-message metadata 读）。已有订阅者忽略它（`BackendEvent` union 是扩展而非替换）。

### Run 目录布局

```
~/.rolemesh/eval/{tenant_id}/runs/{run_id}/
├── run.json                   # Run 元信息 + 聚合指标
├── coworker_snapshot.json     # Coworker 配置快照（含 skill 内容）
├── dataset_snapshot.json      # Manifest + cases.jsonl 的 sha256
├── cases/
│   ├── <case_id>.result.json  # 单 case 分数、性能、actual vs expected
│   └── <case_id>.trace.jsonl  # 原始 BackendEvent 事件流
├── judge/
│   └── <case_id>.judge.json   # LLM judge 的 prompt + response + rationale
├── run.eval                   # Inspect AI 兼容格式（派生产物）
└── logs/
    └── orchestrator.log
```

一个 run 一个目录。分享 run 即打包目录；归档即移动目录；清理即 `rm -rf`。

---

## 数据集

一个数据集是一个外部 git 仓库中的目录：

```
datasets/jira-ops/
├── manifest.yaml
├── cases.jsonl
└── attachments/   # 可选辅助文件
```

Manifest 声明数据集元信息、judge 模型（可选覆盖）、tags、以及一个 `requires_sandbox` 标志，表明对生产（非 staging）MCP 跑该数据集会改动生产数据。`cases.jsonl` 每一行是一个 case，包含：

- `prompt`——给 agent 的任务输入
- `expected_result`——一个判别式 union，覆盖各种打分模式（`exact`、`contains`、`regex`、`json_match`、`llm_judge`）
- `expected_tool_calls`——期望的 MCP 工具调用列表，含参数 matcher（`args_contains`、`args_matches_regex`）和 `order` 索引
- `tags`、`weight`、`metadata`

数据集按版本不可变。编辑一个 case 要求 manifest `version` 升号（并最好打新 git tag）。loader 把 `cases.jsonl` 的 sha256 写入 run 快照，使可复现性可以事后验证。

---

## 打分

### Result Accuracy

模式按 case 在 `expected_result.mode` 中选。实现是轻封装——`exact`、`contains`、`regex` 是小段 in-tree 函数；`json_match` 用 `jsonschema`；`llm_judge` 委托给 `judge.py`，后者用一份固定 prompt 模板（rubric + expected + actual）通过官方 SDK 调 Claude，返回 0-1 分数加自由文本理由。

Judge 审计（prompt、原始响应、解析分数、理由）写入 `judge/<case_id>.judge.json` 供检查。

### Tool Call Accuracy

四个子指标，通过把记录到的 `actual_tool_calls` 与 case 的 `expected_tool_calls` 对齐计算：

| 指标 | 计算方式 |
|---|---|
| `tool_precision` | `\|hits\| / \|actual\|`——被调用的工具中属于期望的占比 |
| `tool_recall` | `\|hits\| / \|expected_required\|`——必需期望工具中被调到的占比 |
| `tool_order` | LCS 长度 / `\|expected_required\|`——对期望顺序的遵守程度 |
| `tool_args_match` | per-call 参数命中率的均值，每次调用的得分是 `args_contains` / `args_matches_regex` 子句的命中比例 |

对齐对非必需的额外工具是宽容的（除非违反 `required=true`，否则不扣 precision），并使用参数 matcher 而非字面值相等，以容忍非确定性输出。

### Failure Mode 分类

对每个 failed case，aggregator 输出零个或多个失败 tag（`missing_tool:<name>`、`extra_tool:<name>`、`wrong_args:<tool>:<key>`、`wrong_order`、`wrong_result`、`timeout`、`error:<prefix>`）。aggregator 在 run 内计数这些 tag，使得分析步骤（"哪种失败模式占主导？"）是直接的，而不是手翻 transcript。

### 性能

逐字记录，不打分：`latency_ms`（总耗时）、`ttft_ms`（首个 `ResultEvent`）、token 计数（按 input/output/cache-read/cache-write 拆分）、用 `src/pi/ai/models.py` 现有的 per-model 价格表算出的 `cost_usd`。

---

## 迭代循环

预期使用方式是一个五步分析循环：

1. **聚合视图**——看 `run.json` 找最弱维度（对说明不充分的 skill，往往是 `tool_recall`）。
2. **Tag 切片**——按 `case.tags` 分组找哪个任务族最弱。
3. **Failure mode**——在该族内看失败模式直方图，找主导模式。
4. **单 case transcript**——对一个代表性失败 case 打开 `inspect view <run>/run.eval` 找根因。
5. **改、再跑、diff**——修改 skill / system prompt / MCP 接线，在同一数据集版本上重跑，然后 `rolemesh eval diff <before> <after>` 验证修复未引入回归。

整个循环设计成可以在一次 CLI 会话内完成。

---

## 为什么不直接用 Inspect Viewer

Inspect AI 的 web viewer 对深度查看单个 run 非常出色：完整 transcript、工具调用时间线、分数、两份 log 的并排视图。这正是我们用它的方式。它**不**提供、因而我们仍然需要 rolemesh 侧 CLI 的部分：

- **没有 regression 高亮。** 并排 ≠ diff。Inspect 能给你看两份 transcript；不会告诉你"case `jira-031` 从 0.85 回归到 0.65"。
- **没有预检。** Inspect 不知道 MCP server、Docker 镜像、credential proxy、coworker 配置。缺失的 API key 或离线的 MCP，在 case 开始失败之前都不会浮现。
- **CLI 不做 tag 聚合。** Inspect 的 viewer 能分组；它的 CLI 不能。agent 开发者想在终端要一行汇总，不想开浏览器。
- **没有规约化的工作流。** Inspect 哲学是"我们给你 log，你在 notebook 里分析"。rolemesh 用户想要分析能力被内建——`eval diff`、`eval show --groupby tag`、`eval list`。

因此切分是：Inspect viewer 用于单 run 深度查看（transcript 浏览、工具时间线、judge 理由），rolemesh CLI 用于 run 管理、预检、聚合、对比。

---

## 分阶段发布

模块按三阶段发布。每个阶段独立可用，每个阶段独立 commit 与测试，用户可以在任何阶段停下而不会留下半建成的模块。

### Phase 1 —— 最小可用评测

目标：端到端管道——对本地数据集跑一个 coworker，写出完整结果文件。

范围内：所有数据类；本地数据集 loader；串行 orchestrator（无并发、无 retry）；事件 recorder；`contains` result scorer；完整 tool-call scorer；性能记录；基础 aggregator；在两个 backend 中加入 `UsageEvent`；唯一一个 CLI 命令——`rolemesh eval run`。输出到 JSON 文件；用户用 `cat` 和 `jq` 检查。

范围外：其他一切。

含测试约 1,600 LOC。要点是早点落地执行路径，让用户在分析工具还在建设期就能开始产出数据。

### Phase 2a —— 分析工具

目标：把 JSON 输出变成工作流。

加入 `eval doctor`、`eval validate`、`eval show`（含 `--case`、`--groupby`）、`eval list`、`eval diff`；failure mode 分类器；git 数据集 loader；带 semaphore 的并发；retry；result scorer 扩展（`exact`、`regex`、`json_match`）；`--fail-if` 表达式；`--mcp-override` 和 `requires_sandbox` 护栏；rich 终端输出。

约 2,450 LOC。

### Phase 2b —— LLM Judge 与 Inspect 集成

目标：主观打分与生态互操作。

加入 LLM-as-judge 实现（直连 Anthropic SDK、宿主侧）；`llm_judge` result mode；用于 `run.eval` 文件的 `inspect_export.py`；`eval rescore`（不重跑模型，只重新评分）；`eval bundle`（可离线查看的 zip 包）。

约 1,150 LOC。

**合计**：三阶段共约 5,200 LOC。

---

## 陷阱

### Run 之间的 Coworker 漂移

如果在两个被比较的 run 之间编辑了 coworker，diff 会把"我改了 skill"和"底层配置变了"混在一起。快照对可复现性有缓解作用（你能验证某次 run 用了什么配置），但比较本身只有在两次 run 共享同一 coworker baseline 或差异是刻意且有记录时才有意义。

约定是：迭代时，除被测变量外冻结 coworker。快照随之成为审计线索，记录变量改动是什么。

### 非确定性

LLM 输出是非确定的。同一个 coworker 对同一个数据集，两次 run 不会得到完全一样的分数。聚合指标预期会有 5–10% 的噪声。单 case 分数波动可能更大。这意味着：

- diff 中的微小分数差异不显著；regression 检测阈值是可配置的。
- 用 CI 卡极小分数变化会产生大量假阳；`--fail-if` 应设成现实的阈值。
- 对高重要性的对比，每侧多跑几次取平均。

### MCP 副作用

调 `update_issue`、`send_message`、`create_user` 等的数据集会改动 coworker 绑定的 MCP server。对生产跑此类数据集是破坏性的。`requires_sandbox: true` manifest 标志加上 `--mcp-override` CLI flag 正是为此存在；只要数据集有任何写操作就应使用。Phase 2a 的默认行为是拒绝在没有 override 的情况下启动一个 `requires_sandbox` 数据集的 run，对极少数刻意对生产跑的场景留一个 opt-out。

### Inspect AI Log 格式耦合

我们把 Inspect AI 的 `EvalLog` 格式作为次要输出写出来以兼容 viewer。该格式归一个外部项目所有；主版本变化可能打破我们的 exporter。缓解措施是：我们的规范化格式是 rolemesh JSON 布局——`.eval` 文件是派生的、可重新生成的，跳过它不会丢数据。`inspect_export.py` 边界很小（约 200 LOC），针对新 Inspect AI release 更新很容易。

### Judge 成本与确定性

每次 LLM-as-judge 调用都是一次 Claude API 请求。200 个 case 的 run 加上 judge 打分，按典型价格约增加 2–5 美元 API 成本和 2–5 分钟挂钟时间。judge 在 `temperature=0.0` 下运行，但模型本身仍然非确定；对噪声大的边界 case，judge 分数可能在不同 run 之间翻转。`eval rescore`（Phase 2b）允许在不重跑模型的前提下重评，当 rubric 演进时部分抵消成本。

---

## 指针

- 分阶段工作在分支 `claude/add-rolemesh-evaluation-m7d2L` 上。
- Coworker 模型与执行路径：`src/rolemesh/core/types.py`、`src/rolemesh/agent/executor.py`、`src/rolemesh/agent/container_executor.py`。
- recorder 订阅的 backend 事件协议：`src/agent_runner/backend.py`。
- recorder 复用的现有 token / 成本机制：`src/pi/ai/models.py`、`src/pi/ai/types.py`。
- 端到端测试使用的 mock MCP server：`tests/mock_mcp_server.py`。
- Inspect AI 文档：[https://inspect.aisi.org.uk/](https://inspect.aisi.org.uk/)——特别是 Log API 与 Scorer 参考。

per-coworker 鉴权模型与 credential proxy 行为见 [`6-auth-architecture.md`](6-auth-architecture.md) 与 [`7-external-mcp-architecture.md`](7-external-mcp-architecture.md)；评测模块不直接与 proxy 交互（judge 在宿主侧运行；agent 的 MCP 调用按生产时同样的方式流经它）。
