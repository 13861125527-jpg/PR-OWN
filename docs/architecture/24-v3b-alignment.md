# V3-B 设计对齐（Agent Loop）

> 状态：**APPROVED DESIGN / READY FOR IMPLEMENTATION**（Round 1 审查修订已合并）
> 审查记录：`docs/evidence/v3-b-alignment-review-round1.md`
> 前置：V3-A **ACCEPTED**（`docs/evidence/v3-a-implementation-review-round2.md`）
> 用户授权：V3-A 验收后进入 V3-B 设计对齐（本对齐稿）
> 契约来源：`01` FR-23、`03` §4–§5 `AgenticReviewer`、`04` §4/§5 AgentTask、`05` §2 AgentSession/AgentBudget、`08` §4–§5/§7/§9、`09` §1/§6 `agent`、`10` §3/§6–§7、`11` §3–§4/§8 V3 子集、`12` V3-b / ADR-001/005、`14` OQ-11、交接 `docs/evidence/cursor-handoff-v1-complete-v2-v3-next.md` §8 V3-B、V3-A `23` DP-2
> DoD 摘要：**Agent 状态机全覆盖（含 WAITING_TOOL 异常出口）；reserved finalize / grace / 取消 / 重复 / 空转终止；submit_finding 只产候选、finish_review 只结束会话；tool_loop 双协议共用同一信封；默认 strategy 仍是 single_pass、agent.enabled 仍 False；不实现会话压缩；V1/V2/V3-A 回归通过**

---

## 0. 一句话与边界

V3-B 交付 **受控 Agent 事件循环**：每文件一个 `AgentSession`，轨迹内严格顺序；模型动作经 `LLMProvider.tool_loop` 归一成 `tool_call | submit_finding | finish_review | none`；只读工具走 V3-A `invoke` 信封；控制工具由 loop 拦截，不进 `FindingPipeline`。`ReviewStrategy.execute` 主签名不变。默认审查路径仍是 V2。

**本里程碑禁止**：会话压缩 / 把模型摘要当 L3（V3-C）、跨文件 V2 vs V3 质量收益报告（V3-D）、MCP、写工具、把 ruff/Judge/反馈做成工具、改 V2-C 融合 / V2-D Judge / V2-E 反馈、改 `cross_run_match_key`、默认打开 `agent.enabled`、复制一套审查主流程、在 loop 内推进 Finding 正式状态。

OQ-11 / ADR-005 仍需提供探针脚本和证据模板，但用户已明确决定当前阶段不再运行真实模型。V3-B 以 Fake/Provider 单测完成工程验收，live 探针登记为 **DEFERRED_BY_USER**，不阻塞本切片；`strategy=agentic` 仍是显式开启的实验路径，默认关闭，不得宣称已验证特定真实模型兼容性。

---

## 1. 现有可复用组件盘点

| 层 | 组件 | V3-B 用法 | 缺口 |
|---|---|---|---|
| 工具 | `builtin_registry` / `invoke` / `is_repeat` / `ToolWorkspace` / `GitToolSnapshot` | loop 只通过 `invoke` 执行只读工具 | `submit_finding` / `finish_review` 仍 `not_bound` |
| 领域 | `AgentMessage` / `AgentBudget` / `ToolCall` / `ToolResult` | 直接用；Session 落地为类 | 无 `AgentSession`；`AgentMessage` 缺 `name` / `tool_call_id` |
| 领域 | `ReviewTaskStatus.WAITING_TOOL`、`ReviewTaskKind.AGENT_TASK`、`FindingSourceKind.TOOL_AGENT`、`Evidence.tool_call_id` | 状态机与盖戳 | Pipeline `_source_from_candidate` 不会盖 `TOOL_AGENT` |
| 协议 | `LLMProvider.tool_loop(session_view, tools, budget)`；`ModelResponse.action` | 实现 native + action_json | domain 定义 `AgentSessionView` Protocol，provider 不反向依赖 review 层 |
| Fake | `FakeLLMProvider.tool_loop` 固定 `finish_review` | 改为脚本化轨迹 | 无逐步 tool/submit/finish 剧本 |
| 策略 | `ReviewStrategy.execute(units, run, budget)` | **主签名不变**；新增 `AgenticReviewer` | Service 对 `strategy=agentic` 失败快 |
| 编排 | `ReviewService`：preflight→fetch→context→strategy→pipeline→publish | 选择器接 Agentic；Pipeline 仍唯一生命周期 | 无 Agent 装配（snapshot / registry / workspace） |
| 预算 | `GlobalBudget.reserve/settle` + `reserved_finalize_ratio` | 每次模型调用仍走全局预留；Agent 叠加轮数/工具/墙钟 | 无探索耗尽 → grace 的会话态 |
| 上下文 | `ReviewUnit` + `unit_to_messages` | 初始 user 消息用已装配 L1/L2（及 L3 若 V2-B 开） | 无 agent 治理/工具协议 prompt |
| 配置 | `Settings.agent`（enabled/预算/`tool_protocol`） | **开始读取**；默认不改 | Service 未把 agent 预算叠进 session |
| 存储 | `tasks` / `tool_calls` / `tool_results`；`user_version=5` | AgentTask 首次 invoke 前必须落库，状态转换 upsert | 不升版本；不新增 AgentSession 表 |
| 评测 | v2a–v2e / v3a compare | 钉死 `agent.enabled=false` | 无 loop 轨迹对照集 |
| 安全 | 路径沙箱、脱敏、取消重抛 | 保持 | loop 必须在安全点处理取消，禁止 `WAITING_TOOL → COMPLETED` |

**明确不复用为 V3-B 生产路径**：LangChain/LangGraph（ADR-001）、MCP、`os.walk`、读脏工作区、在 handler 里直接 `pipeline.process`。

---

## 2. 产品目标与非目标

### 2.1 目标（FR-23 的本切片；`12` V3-b）

1. **状态机**：`PENDING → RUNNING ⇄ WAITING_TOOL → COMPLETED|PARTIAL|FAILED|CANCELLED`，含 WAITING_TOOL 的 error / FAILED / CANCELLED 出口；禁止未回灌终局。
2. **有界循环**：`max_rounds`（每次实际发给 provider 的请求都计数，含 JSON 修复）/ `max_tool_calls` / tool attempts / token / cost / `max_wallclock_s`；探索耗尽进入 grace（只允许已有证据的 `submit_finding` 与 `finish_review`），所有文件共享 run 级 finalize 预留池，不突破 `GlobalBudget` 硬顶。
3. **取消 / 超时 / 重复 / 空转**：外部取消在安全点生效；模型网络超时有限重试且不借用 grace；连续重复超阈值 → `PARTIAL` + `repeat_loop`；无进展超阈值 → `PARTIAL` + `no_progress`。
4. **控制工具语义**：`submit_finding` 归一为 `FindingCandidate` 并在会话内累积；`finish_review` 结束循环并做本切片终局检查。二者 **不** 调用 `FindingPipeline.process`。
5. **协议归一**：native tool calling 与 Action JSON 都产出同一 `ModelResponse.action`；只读工具仍走 V3-A 信封。
6. **可测**：Fake 脚本化轨迹覆盖状态机与预算，不依赖真实 LLM。Live 协议探针单独出证据（OQ-11）。
7. **不破坏 V1/V2/V3-A**：默认 `single_pass` + `agent.enabled=false`；V3-A 沙箱对照与 A–E compare 行为不变。

### 2.2 非目标（本里程碑硬裁）

| 不做 | 归属 | 理由 |
|------|------|------|
| 会话压缩 / `maybe_compact` 摘要 / 完整 `evidence_index` 压缩 | V3-C | `12` V3-c |
| 跨文件质量对照、工具有效率门槛 0.7 | V3-D / `11` §8 全量 V3 | 无压缩与稳定协议裁决前不宣称收益 |
| MCP / Skill 当工具 | `08` §8 optional | 降级扩展 |
| 正则/FTS/向量 `search_code` | 后续 | V3-A DP-18 字面量保持 |
| ruff 工具、写文件、任意 subprocess | — | 程序事实与沙箱 |
| 改 `execute` 主签名 | — | 与 V2 同一铁律 |
| 默认 `agent.enabled=true` 或自动把用户升到 `agentic` | — | 无 live 协议证据时危险 |
| 新增 SQLite `user_version` | — | 现表够用 |
| 单轨迹内并行多工具 | `08` §4 | 初版取第一个，其余记待办提示 |

### 2.3 与架构原文的已知差异（必须显式）

| 架构原文 | V3-B 裁决 | 处理 |
|----------|-----------|------|
| `08` loop 内 `pipeline.enqueue(candidate)` | **不在 loop 调 Pipeline** | **DP-3**；Strategy 只返回候选，与 P0-1 一致 |
| `08` §6 会话压缩 | 本切片不做摘要压缩 | **DP-4**；超限 → `PARTIAL` + `context_overflow` |
| `08` §7 终局「重跑 Pipeline」 | Service 在 `execute` 之后已经跑 Pipeline | loop 终局只做会话不变量（无 WAITING_TOOL、证据 ref 存在） |
| `11` §8 V3 含压缩与跨文件收益 | 全量 V3 门槛拆到 C/D | 本切片 DoD = `12` V3-b |
| `08` §9「V3 启动前」完成 A/B 实测 | 用户书面后置；T1 交付可运行探针与 deferred 证据 | **DP-2**；不阻塞 Fake 工程验收，默认 Agent 继续关闭 |
| `04` 伪代码 `llm.tool_loop(session.messages, tools)` | provider 吃 domain 层 `AgentSessionView`，review 层 Session 结构化实现该协议 | **DP-6**；避免 domain→review 反向依赖 |
| 交接「reasoning/waiting_tool/observing/…」状态名 | 采用已落地的 `ReviewTaskStatus` | **DP-1**；observing 不是独立持久状态 |
| `05` `AgentSession.evidence_index: dict[str, Evidence]` | V3-B 只保留 `tool_call_id → 信封摘要` 的轻量索引 | 完整 Evidence 对象与压缩属 V3-C |

---

## 3. 领域：Session、状态机、预算叠加

### 3.1 AgentSession（本切片落地）

新建 `reposage/review/agent/session.py`。可变状态与转换逻辑放 review 层；`domain/protocols.py` 只新增只读结构协议 `AgentSessionView`，供 LLMProvider 类型标注，禁止 domain 导入 `reposage.review.agent.session`。

```text
AgentSession
  session_id: str          # = task_id
  task_id: str
  run_id: str
  file_path: str
  status: ReviewTaskStatus
  messages: list[AgentMessage]
  calls: list[ToolCall]    # 本会话已尝试（含 invalid/repeat）
  candidates: list[FindingCandidate]
  evidence_refs: dict[str, str]   # tool_call_id → source.ref（轻量，非 V3-C 索引）
  budget: AgentBudget
  mode: exploration | grace
  idle_count / repeat_count / model_retries: int
  started_at: datetime
```

`AgentSession` 结构化实现 `AgentSessionView`。View 仅暴露 provider 所需的不可变快照：`session_id`、`messages: Sequence[AgentMessage]`、`mode`、剩余轮数/预算摘要；不得向 provider 暴露可变 candidates/calls，Provider 也不得修改 Session。

`AgentMessage` 最小增量（不升 SQLite）：

- `name: str | None = None`（tool 消息的工具名）
- `tool_call_id: str | None = None`
- `tool_calls: list[AgentToolRequest]`（assistant native 消息的完整调用清单，默认空；`AgentToolRequest` 放 domain models）

不在本切片把 `checked/excluded/pending` 做成完整会话记忆产品；loop 内部可用集合防重复提示，不落新表。

### 3.2 AgentTask 状态机（`04` §5，任务 = ReviewTask）

合法转换：

| 从 \ 到 | PENDING | RUNNING | WAITING_TOOL | COMPLETED | PARTIAL | FAILED | CANCELLED |
|---------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| PENDING | – | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ |
| RUNNING | ❌ | – | ✅ | ✅ | ✅ | ✅ | ✅ |
| WAITING_TOOL | ❌ | ✅ | – | ❌ | ❌ | ✅ | ✅ |
| 终态 | ❌ | ❌ | ❌ | – | – | – | – |

- `RUNNING → WAITING_TOOL`：模型给出只读 `tool_call`，即将 `invoke`。
- `WAITING_TOOL → RUNNING`：观察回灌（ok / invalid_args / timeout / error 协议）。不可恢复异常（invoke 之外的崩溃）→ `FAILED`。
- `WAITING_TOOL → CANCELLED`：等待期外部取消；工具侧仍按 V3-A DP-19 尽力审计 `cancelled` 再重抛。
- `RUNNING → COMPLETED`：仅 `finish_review` 且终局检查通过（无未回灌工具）。
- `RUNNING → PARTIAL`：grace 结束后仍有未完成探索，或 repeat/idle/budget/wallclock/context_overflow；**已提交候选保留**。
- 非法：`WAITING_TOOL → COMPLETED`、`PENDING → WAITING_TOOL`。实现用显式转换函数断言。

handoff 的 `reasoning/observing/finalizing` 是 loop 内阶段注释，不新增枚举。

任务必须在进入 RUNNING 时通过 `Storage.record_tasks([task])` 写入，之后每次转换都 upsert 当前状态；这既提供 WAITING_TOOL 审计，也保证 V3-A `record_tool_invocation` 的 task FK 在首次 invoke 前存在。`record_tasks` 对同一 task_id 必须是状态更新而非重复插入。Session 消息与 candidates 不落库，崩溃后只留下任务终态/最后安全状态，不恢复半截会话。

### 3.3 预算叠加

两层预算与一个共享协调器，缺一不可：

1. **Run 级 `GlobalBudget`**：每次模型调用 `reserve(input, max_output, est_cost)` → 调用 → `settle`。墙钟到期 / overrun / 费用未知标记语义与 V1 相同。迟到取消的 usage 仍记 `late_cancelled`（`08` P0-R2-3）。
2. **会话级 `AgentBudget`**：从 `Settings.agent` 派生，限制单文件轮数/工具/墙钟。
3. **run 级 `FinalizeBudgetCoordinator`**：由一个 AgenticReviewer run 内所有文件共享。它只做 admission control，不复制 GlobalBudget 账本：探索请求必须给整个 run 保留一次 `reserved_finalize_ratio` 池；grace 请求从该共享池竞争。不得为每个文件各自预留 10%（否则 N 文件会虚构 N 份 finalize 额度）。GlobalBudget 仍是最终原子硬顶。

| 维度 | 初值（配置） | 计数规则 |
|------|----------------|----------|
| max_rounds | 8 | 每次实际发给 provider 的逻辑请求都计数（含 action JSON 修复请求与 grace）；不能因解析失败免费多发请求 |
| max_tool_calls | 12 | 仅 `status=ok` 的只读工具 |
| tool_attempt_count | — | 每个模型要求的工具动作都 +1；invalid_args / unknown / repeat / timeout / error 不计 `max_tool_calls`，但计无进展；另设硬上限 `max_tool_attempts`，默认 `max_tool_calls * 3` |
| max_tokens / max_cost_usd | 继承 run 剩余 | 发送前预留；探索池 = 总额 × (1 - `reserved_finalize_ratio`) |
| reserved_finalize_* | 总额 10% | 仅 grace / 收尾轮可消耗 |
| max_wallclock_s | 300（任务）且受 run `max_runtime_seconds` | 先到先停；到时 **不再发模型请求** |
| grace_rounds | 2 | 进入 grace 后的受限轮 |
| repeat_threshold | 3 | 连续 `is_repeat` 命中 |

探索耗尽条件（任一）：成功工具数达上限、tool attempts 达上限、轮数达 `max_rounds - grace_rounds`、探索 token/cost admission 被共享协调器拒绝。进入 grace 后禁止新只读工具、禁止加载新文件上下文。

网络/429 超时：Provider 内部有限 HTTP retry 沿用现策略，每次真实响应 usage 都按既有规则记账；loop 不额外无限重试。Provider 最终失败后按一次已占用逻辑轮处理，**不**进入 grace，**不**挪用 finalize 预留。`max_rounds`、`max_tool_attempts`、GlobalBudget、wallclock 四者共同保证任何错误路径有限。

`Settings.agent` 新增 `max_tool_attempts: int | None = None`（显式值必须 >0）；为 None 时运行期派生为 `max_tool_calls * 3`。配置快照要包含最终生效值；默认配置行为仍不开 Agent。

---

## 4. Agent Loop

伪代码对齐 `08` §4，按本切片裁决改写：

```text
async def agent_loop(task, units, workspace, budget) -> AgentOutcome:
    session = AgentSession(...)
    session.add(system=L0 治理 + 工具协议 + 预算声明)  # 不含 CoT 要求
    session.add(user=unit_to_messages 的 L1/L2 块 + 本文件目标)
    while True:
        # 安全点 1
        if cancelled: → CANCELLED（已提交候选保留）
        if wallclock/global 拒绝新请求: → 不调模型；有候选则 PARTIAL else FAILED
        if exploration_exhausted: session.mode = grace
        if grace 轮次用尽: → PARTIAL reason=budget

        rounds_started++                  # 发请求前计数，修复请求也必须计
        resp = await llm.tool_loop(session.view(), registry.schemas(), budget=global)
        # 模型超时：有限 retry；耗尽 → PARTIAL model_timeout
        consume usage（含 late_cancelled 记账）

        match resp.action:
          tool_call:
            session.add(assistant action message，包括完整 tool_calls 与 provider call ids)
            if grace: observe(error=grace_no_new_tools); continue
            if name in {submit_finding, finish_review}: 走控制分支（§6）
            if not registry.has: observe(unknown_tool); attempt++; continue
            if is_repeat: observe(repeat, hint=prev); repeat++; 超阈 → PARTIAL
            transition RUNNING → WAITING_TOOL
            call, result = await invoke(...)   # V3-A 信封
            transition WAITING_TOOL → RUNNING（或 FAILED/CANCELLED）
            session.add(tool observation = result.data 信封；tool_call_id 必须与 assistant request 配对)
            if ok: successful_tools++
          submit_finding: 见 §6
          finish_review: 终局检查 → COMPLETED 或 PARTIAL
          none:
            if 无新增结构化信息: idle++; 超 2 → PARTIAL no_progress
            else: session.add(decision_summary)  # 禁止存 raw CoT
        if messages 估算超会话上限: → PARTIAL context_overflow   # 不压缩
```

多 tool_call：assistant 原始消息必须完整保留全部 provider call id。只执行第一个；对其余每个 call id 各追加一条配对 tool observation，内容为 `not_executed_multiple_calls`。不能丢弃未执行 call，也不能用一条无 call id 的普通 user 消息代替，否则 native API 下一轮会因 assistant/tool 配对不完整而拒绝请求。

`tool_loop` 返回后处理新的取消（安全点 2）；若取消发生在 provider 请求中，立即取消在途协程并按既有 late-cancelled usage 规则结算。与 V3-A 工具取消规则兼容。

---

## 5. `tool_loop` 双协议与 OQ-11 门禁

### 5.1 统一动作载荷

扩展 `ModelResponse`（不改 `complete`/`structured` 签名）：

```text
action: None | "tool_call" | "submit_finding" | "finish_review"
data: {
  "name": str,            # tool_call / 控制工具名
  "arguments": dict,      # 已是 JSON 对象，不是字符串
  "reason": str | None    # finish_review
}
```

Provider **不得**执行工具。Loop 才 `invoke` 或拦截控制工具。

`LLMProvider.tool_loop` 的 `session` 类型从 `object` 换成 domain 层 `AgentSessionView` Protocol，不允许 domain 依赖 review 层具体类。`tools` 为 `registry.schemas()` 的 OpenAI-style 列表。

归一响应还必须携带 transcript 元数据：`provider_message_id`（可空）、完整 `tool_requests[{id,name,arguments}]`、本轮 assistant 可见 content。Loop 负责先把 assistant action 写入 Session，再执行/拒绝工具并写入逐 call-id observation；Provider 不执行工具。

### 5.2 方案 A：native

`openai_compat.tool_loop`：

- 请求带 `tools` / `tool_choice=auto`（具体字段名跟现有 OpenAI-compatible 客户端一致）。
- 响应保留完整 `tool_calls`；归一动作以第一件为当前执行目标，但不得丢掉其余 id；控制工具名同样走 tool_calls。
- 无 tool_calls 且无约定 finish JSON → `action=None`（idle 路径）。
- native 不可用（HTTP 400/明确 unsupported）：**本轮请求**记协议失败，不在同一请求里偷偷改成 Action JSON（避免评测污染）。会话级是否切换由配置 `tool_protocol` 决定，不自动热切换。

### 5.3 方案 B：Action JSON

不传 native `tools`。模型输出严格 JSON：

```json
{"action": "tool_call", "name": "read_file", "args": {"path": "src/a.py", "start_line": 1}}
{"action": "submit_finding", "args": {…SubmitFindingArgs…}}
{"action": "finish_review", "args": {"reason": "done"}}
{"action": "none", "summary": "…"}
```

解析失败最多修复一次；修复是一次新的 provider 逻辑请求，必须占用 round、GlobalBudget 与 wallclock，且不能使用 finalize 预留去修复探索期坏 JSON。仍失败 → 观察 `invalid_action` 并计入 no-progress。禁止模型在 JSON 里内嵌「伪造的工具结果」。

### 5.4 OQ-11 探针（T1，非 V3-D）

独立脚本 `reposage/providers/llm/tool_protocol_smoke.py`，风格对齐 V1-C `smoke`：

- 固定一工具：`read_file` schema；固定指令「只调用该工具，path=`src/a.py`」。
- 每协议 N 轮（默认 10）；指标：parse_ok、name 匹配、args 过 Pydantic、extra forbid、latency、tokens。
- **不**跑完整 AgenticReviewer，**不**宣称 Precision/Recall。
- 无 `MODEL_API_KEY`：脚本退出码区分「跳过 live」；CI 默认跳过。
- 有 Key：写 `docs/evidence/v3-b-oq11-protocol.md/.json`（脱敏规则同 V1-C）。

裁决矩阵（写入证据后填 DP-2 决议）：

| native ≥0.8 | action_json ≥0.8 | 决议 |
|:--:|:--:|------|
| 是 | * | 默认 `tool_protocol=native` |
| 否 | 是 | 默认 `tool_protocol=action_json` |
| 否 | 否 | **暂缓产品 Agent**；Fake loop 仍可合并，Service 保持 `agent.enabled=false` |

本项目当前裁决：用户已书面选择“真实模型暂不做”。T1 仍实现脚本和 `DEFERRED_BY_USER` 报告，但不调用 API、不消耗 Key；未来恢复时再填 native/action_json 实测矩阵。

配置默认值本切片 **不改**：`tool_protocol` 仍为 `native`；由于未做 live 证据，文档与 CLI 必须标注 agentic 为 experimental。产品开关仍是 `agent.enabled=false`。

---

## 6. 控制工具绑定

### 6.1 拦截点

Loop 在 `invoke` **之前**识别 `submit_finding` / `finish_review`。V3-A handler 保持 `not_bound`，防止任何绕过 loop 的直接 `invoke` 写入候选。

控制工具仍出现在 `registry.schemas()` 里，供模型点名。

### 6.2 `submit_finding`

1. 用现有 `SubmitFindingArgs`（可在本切片 **加可选字段**，`extra=forbid` 不变）：`claimed_end_line`、`explanation`、`suggestion`、`trigger_condition`、`evidence_tool_call_ids: list[str]`（上限 8，每项必须是本会话已成功的 `tool_call_id`）。
2. 程序构造 `FindingCandidate`：`sanitize` 掉模型夹带的 `source_kind/rule_id/analyzer_id`，再盖 `source_kind=TOOL_AGENT`；`claimed_path` 缺省为本任务 `file_path`。
3. 证据：对每个合法 `evidence_tool_call_id` 附加 `Evidence(kind=TOOL_RESULT, tool_call_id=..., verified=True)`；未知 id → 本条提交 `invalid_args`，不入候选。模型自报 `verified` 无效。
4. grace 模式：拒绝没有任何本会话成功工具证据的提交。
5. 观察回灌：`{"ack": true, "candidate_index": n}` 或错误码。
6. **不**调用 `FindingPipeline`。

Pipeline 增量：`_source_from_candidate` 在 Strategy 已盖 `TOOL_AGENT` 时保持该 kind（与 static 盖戳同级的程序事实）。不改聚类键、不改 `cross_run_match_key`。

### 6.3 `finish_review`

1. 合法 `FinishReviewArgs.reason`。
2. 若当前是 `WAITING_TOOL`：视为非法动作，回喂「先等待工具观察」，不终局。
3. 终局检查：无未回灌调用；会话 status 不是 WAITING_TOOL。
4. 然后 `COMPLETED`（即使候选为空：空审查合法，Pipeline 会得到空列表）。
5. 不在 loop 内重跑 schema/location——那是 Service 已有 Pipeline。

---

## 7. 取消、重复、空转、并发

| 场景 | 处理 |
|------|------|
| 外部取消 | 安全点 1/2；尽力持久化任务 `CANCELLED` 后重新抛出 `CancelledError`。取消路径不会返回 `StrategyResult`，会话内 Candidate 尚不是正式 Finding，因此不进入 Pipeline、也不承诺保留；已在更早阶段正式落库的 Finding 不受影响 |
| 工具等待中取消 | `WAITING_TOOL → CANCELLED`；invoke 按 DP-19 |
| 重复只读调用 | `is_repeat`（V3-A 已有）；回喂 hint；连续命中 > `repeat_threshold` → PARTIAL |
| 空转 | `action=None` 且无新决策摘要；idle>2 → PARTIAL `no_progress` |
| 文件并发 | 与 SinglePass 相同：`file_tasks` 跨文件；每文件内顺序 loop；`model_requests` 限制在途 `tool_loop` |
| 注入 | 工具结果与 PR 文本进 user/tool 消息，包 `[UNTRUSTED_CONTENT]`；疑似注入记遥测，默认不当 Finding（`09` §7 / V2 已有） |

禁止保存 raw CoT（`observability.save_raw_chain_of_thought=false` 已存在）。

`asyncio.gather` 的取消语义必须区别于普通文件失败：某文件普通异常可形成 required failure 并保留其他文件结果；父级 `CancelledError` 必须取消所有 sibling Agent task、等待清理完成后继续向 Service 传播，不能被 `return_exceptions=True` 吞成 PARTIAL。

---

## 8. ReviewService / AgenticReviewer 接线

### 8.1 选择器

```text
strategy=agentic 且 agent.enabled=true  → AgenticReviewer
strategy=agentic 且 agent.enabled=false → 失败快（配置错误，不是静默 SinglePass）
strategy 缺省 / single_pass / multi_role → 现逻辑；忽略 agent.enabled
```

默认配置不变：`review.strategy=single_pass`，`agent.enabled=false`。

Service 构造期只做双闸校验：agentic + disabled 立即失败；agentic + enabled 不再沿用 V3-A 的“尚未实现”失败，而是把实际 Strategy 构造延迟到本次 review 的 file_map/snapshot 已就绪后。进入 Strategy 前必须先 `record_run(run)`，否则 AgentTask FK 无法落库。

### 8.2 AgenticReviewer

路径：`reposage/review/agent/reviewer.py`。

- `name = "agentic"`
- `execute(units, run, budget)`：按 `file_path` 分组（同 SinglePass）；每文件一个 `ReviewTask(kind=AGENT_TASK)`；只从构造时注入的 `AgentWorkspaceFactory` 取得 workspace，不从 prompt/L2 文本反向解析 diff。
- 产出 `StrategyResult(candidates, SourceRunResult)`：`health` 规则与 V2 相同——文件级 Agent 失败 = required 失败。
- `ModelUsage.role = "agent"`。

不改 Publisher、不改 `execute` 主签名、不在 Service 里执行工具。

### 8.3 每次运行的 WorkspaceFactory（新增明确接口）

现有 `execute(units, run, budget)` 拿不到 `file_map`、snapshot provider 和 sandbox root，不能凭空构造 `read_diff` 所需的 `ChangedFile`。因此 Service 在 FETCH/CONTEXT 已完成、`file_map` 已生成后，为**本次 review 调用**构造局部 `active_strategy`：

```text
AgentWorkspaceFactory
  snapshot: ToolSnapshot              # 绑定 run.head_sha
  diff_files: dict[str, ChangedFile]   # 来自 parse_unified_diff，不解析 prompt
  sandbox_root: Path | None
  build(file_path, task_id, run_id) -> ToolWorkspace
```

- 本地 Git：`sandbox_root=repo_root`，继续做 symlink resolve 防护。
- 远程 Git/Fake：`sandbox_root=None`，以锁定 SHA 的 `ToolSnapshot.list_paths()` 作为路径权威；仍执行字符串级绝对路径/`..`/盘符/UNC 校验。V3-A `ToolWorkspace.repo_root` 与 path resolver 需允许该显式远程模式，不能伪造不存在目录来绕过语义。
- `diff_files` 使用 Service 已解析的结构化对象；`read_diff` 不重新执行 git、不解析 prompt 文本。
- `active_strategy` 是 review 方法内局部变量，禁止覆盖共享 `self.strategy`，避免同一 Service 并发 review 时跨 run 污染 workspace/head SHA。
- AgenticReviewer 构造注入：LLM、registry、workspace_factory、Storage、共享 GlobalBudget/FinalizeBudgetCoordinator、并发 semaphore 与 settings。

默认 SinglePass/MultiRole 继续使用现有 `self.strategy`；只有显式 agentic 双闸在每次 run 的 context 完成后构造局部 AgenticReviewer。Service 只负责装配，不执行工具。

### 8.4 快照与 diff

`read_file` / `find_files` / `search_code` / `find_references` 继续读锁定 SHA。`read_diff` 继续读本 run 已解析 diff（V3-A）。跨文件取证靠工具，不靠再装配一遍全仓库进 prompt。

---

## 9. Prompt、可观测、存储

### 9.1 Prompt

新增 `reposage/prompts/tasks/agent.md`：工具清单说明、一次只调一个工具、用 `submit_finding`/`finish_review` 收尾、禁止编造工具结果、预算将尽时必须 finish。治理层复用现有 L0（若仓库尚无独立 `prompts/governance/`，agent 任务文件内嵌边界标记说明，不新造 CoT 模板）。

`unit_to_messages` 的不可信块规则保持。Agent system 块额外声明工具协议（native 与 action_json 各一条短指令，按 `tool_protocol` 选择）。

### 9.2 可观测

- 每轮 / 每工具：现有 `StructuredLogger` + V3-A `record_tool_invocation`（`save_tool_trace`）。
- 事件建议：`agent.round`、`agent.grace`、`agent.stop`（reason）、`agent.cancel`。
- 指标（本切片采集，不设 V3-D 质量门槛）：成功工具 / 总 attempt、重复率、停止原因、是否突破硬预算（断言应为否）。

### 9.3 存储

`user_version` 保持 **5**。任务 status 写入已有 `tasks.status`（可存 `waiting_tool`）。不新增 AgentSession 表；崩溃后不恢复半截 loop（本切片不做跨进程会话续跑）。

---

## 10. 测试与对照

### 10.1 必须有的 Fake 轨迹

- 只读 `read_file` → `submit_finding`（引用该 tool_call_id）→ `finish_review` → 候选 `source_kind=TOOL_AGENT`，Pipeline 后有正式 Finding 或按现规则 body_only/suppress
- 未知工具 / invalid_args 回喂且计入 attempt，不计入 successful tool
- 连续 repeat → PARTIAL `repeat_loop`
- 连续 idle → PARTIAL `no_progress`
- `max_tool_calls` 耗尽后只读调用被拒绝，submit/finish 仍可
- grace 无证据 submit → invalid
- 取消在 WAITING_TOOL：任务 CANCELLED，CancelledError 传播
- 父级取消会取消并等待所有 sibling 文件 Agent；不把 CancelledError 降级为普通 required failure
- 禁止 WAITING_TOOL 直接 COMPLETED（单测转换表）
- task 在首次 invoke 前已落库，WAITING_TOOL/RUNNING/终态按同一 task_id upsert，tool_calls FK 成立
- 墙钟耗尽不再调模型
- native 多 tool_calls 保留完整 assistant 请求，并为每个 provider call id 产生配对 observation；下一轮 transcript 合法
- action JSON 修复请求占 round/预算；达到 max_rounds 后不能再修复
- 多文件并发只共享一份 run finalize 池，探索 admission 不侵占该池，grace 竞争也不突破 GlobalBudget
- WorkspaceFactory 使用结构化 file_map；并发两次 review 不串 head SHA/diff；远程模式不依赖伪造本地路径
- `strategy=agentic` 且 `enabled=false` 仍失败快
- 默认 `single_pass` review 零 tool_calls（V3-A 隔离回归）
- 直接 `invoke(submit_finding)` 仍 `not_bound`

### 10.2 对照 `v3b_compare`

数据集 `reposage/evals/datasets/v3b_loop.yaml`：脚本化轨迹，无真实模型。主表不是 P/R/F1。

| 表 | 内容 |
|----|------|
| 轨迹 | 上述停机原因与终态 |
| 控制工具 | 候选只从 loop 出，stub invoke 不进 Pipeline |
| 隔离 | 默认 review 无 Agent |
| 预算 | 硬顶不被 grace 突破 |

V2-A～E 与 V3-A compare 继续钉死 `agent.enabled=false`。

Live OQ-11 报告单独存放，不并入 compare 及格条件（CI 无 Key）。

---

## 11. 分步实施任务卡（审查通过后才编码）

| 卡 | 内容 | 验收 | 依赖 |
|----|------|------|------|
| T0 | 本对齐稿审查与 Round 1 修订 | 本文冻结 | — |
| T1 | OQ-11 协议探针 + `DEFERRED_BY_USER` 证据（本轮不调用真实 API） | `docs/evidence/v3-b-oq11-protocol.md` | T0 |
| T2 | domain `AgentSessionView`/tool request DTO；review `AgentSession` + 状态转换；共享 FinalizeBudgetCoordinator | 依赖方向、非法转换与并发预算单测 | T0 |
| T3 | `openai_compat.tool_loop` + Fake 脚本化轨迹（双协议） | Provider 单测；无 Key | T2 |
| T4 | `agent_loop`：完整 transcript、invoke 只读、attempt/round、repeat/idle/grace/cancel/wallclock；task 状态 upsert | loop 与 FK 单测 | T2 T3 |
| T5 | 拦截 `submit_finding` / `finish_review`；Pipeline 认 `TOOL_AGENT` | 候选盖戳与 stub 隔离 | T4 |
| T6 | `AgentWorkspaceFactory` + AgenticReviewer + Service 每 run 局部选择器（enabled 双闸） | 本地/远程、diff、并发 run 装配测 | T5 |
| T7 | agent prompt；`v3b_loop` compare；钉死 A–E / V3-A agent off | `docs/evidence/v3-b-compare.md` | T6 |
| T8 | 全量 pytest / Ruff / mypy / `git diff --check` | CI | T7 |

T1 脚本/模板是交付项；真实调用已由用户书面后置，不是 V3-B 本轮 ACCEPTED 依赖。恢复 live 验证前，Agent 默认关闭且标记 experimental。

目录：

```text
reposage/review/agent/session.py
reposage/review/agent/loop.py
reposage/review/agent/reviewer.py
reposage/review/agent/budget.py
reposage/review/agent/workspace.py
reposage/prompts/tasks/agent.md
reposage/providers/llm/tool_protocol_smoke.py
reposage/evals/datasets/v3b_loop.yaml
reposage/evals/v3b_compare.py
```

不引入 LangGraph，不新建 MCP 包，不升 `user_version`。

---

## 12. DoD 与审查清单

### 12.1 交接 DoD（V3-B 子集）

| # | 条目 | 落点 |
|---|------|------|
| 1 | 状态机全覆盖，含 WAITING_TOOL 异常出口 | T2 T4 |
| 2 | reserved finalize + grace；硬顶不被突破 | T4 |
| 3 | 取消 / 重复 / 空转 / 墙钟 | T4 |
| 4 | submit 只产候选；finish 只结束会话 | T5 |
| 5 | tool_loop A/B 同一动作信封 | T1 T3 |
| 6 | 默认不启用 Agent；V1/V2/V3-A 回归 | T6 T7 T8 |
| 7 | 不实现压缩 / MCP / 质量 A/B | 全程 |
| 8 | native assistant/tool transcript 按 provider call id 完整配对 | T3 T4 |
| 9 | workspace 使用锁定 snapshot + Service 结构化 file_map，支持远程无本地根 | T6 |
| 10 | AgentTask 先落库再 invoke；多文件共享一份 finalize 池 | T2 T4 |

### 12.2 架构自查

- [ ] 未改 `execute` 主签名
- [ ] 未在 loop 内调用 `FindingPipeline.process`
- [ ] 未默认打开 `agent.enabled`
- [ ] `strategy=agentic` 且 enabled=false 仍失败快
- [ ] 未实现会话压缩
- [ ] 未把 ruff/Judge/反馈做成工具
- [ ] 未升 `user_version`
- [ ] 未引入 MCP / LangChain
- [ ] 默认 dry-run；不提交、不 push
- [ ] 不保存 raw CoT

### 12.3 实现期测试清单

见 §10.1；外加：V3-A 沙箱 10/10、A–E compare 绿、直接 invoke 控制工具仍 `not_bound`。

---

## 13. 决策点（未遭反对则按此实现）

| ID | 结论 | 理由 |
|----|------|------|
| **DP-1** | 持久状态用已有 `ReviewTaskStatus`（含 `WAITING_TOOL`），不新增 reasoning/observing 枚举 | 与 `04`/`05` 已实现枚举一致 |
| **DP-2** | 用户已书面后置真实模型；T1 交付探针和 `DEFERRED_BY_USER` 证据，Fake/Provider 单测可完成 V3-B 工程验收 | 不消耗真实 API；默认 Agent 继续关闭并标 experimental |
| **DP-3** | `submit_finding` 只累积候选；Pipeline 仍只在 Service 的 `execute` 之后跑 | P0-1 / ADR-003 |
| **DP-4** | V3-B 不做会话压缩；上下文超限 → PARTIAL `context_overflow` | 压缩是 V3-C |
| **DP-5** | 双协议都实现；运行时只读 `agent.tool_protocol`，不自动热切换 | 评测可复现 |
| **DP-6** | `tool_loop(session: AgentSessionView, tools, *, budget: GlobalBudget)`；View/DTO 在 domain，具体 Session 在 review | 保持依赖方向 |
| **DP-7** | 多 tool_call 只执行第一个，但完整保存 assistant tool_calls，并为其余 id 回配 `not_executed` tool observation | 保证 native transcript 合法 |
| **DP-8** | 控制工具 loop 拦截；handler 保持 `not_bound` | 防止绕过 |
| **DP-9** | `strategy=agentic` 需要 `agent.enabled=true`；默认两者都不开 | 双闸 |
| **DP-10** | 每 changed file 一个 AgentSession；文件间 `file_tasks` 并发，轨迹内顺序 | `04` 任务级并发 |
| **DP-11** | Pipeline 承认程序盖戳的 `TOOL_AGENT`；不改 fingerprint / cross_run_match_key | 来源可追溯 |
| **DP-12** | 成功工具才计入 `max_tool_calls`；所有工具动作计 `tool_attempt_count`；每次产生动作的 provider 请求本身已计 round | `08` P1-5，避免重复/错误无限循环 |
| **DP-13** | grace 禁止新只读工具；submit 必须引用本会话成功 `tool_call_id` | 无证据收尾 |
| **DP-14** | 墙钟/外部取消不再发模型；迟到 usage 仍记账 | `08` P0-R2-3 |
| **DP-15** | `user_version` 保持 5；不持久化 AgentSession | 范围控制 |
| **DP-16** | 不改 V2-C/D/E 语义，不改默认 strategy | 回归铁律 |
| **DP-17** | 不把 V3-D 质量门槛塞进本切片 compare | compare 只测 loop 契约 |
| **DP-18** | `search_code` 保持字面量 | V3-A DP-18 |
| **DP-19** | 取消规则沿用工具层 DP-19 | 不吞 `CancelledError` |
| **DP-20** | 无 LangGraph / MCP / ruff-as-tool | ADR-001 与 V3-A DP-3/15 |
| **DP-21** | 取消重新抛出时不返回 StrategyResult；会话 Candidate 未正式入 Pipeline，取消路径不承诺保留 | 消除“抛异常且返回候选”的不可能语义 |
| **DP-22** | AgentTask 在首次 invoke 前落库，每次状态转换 upsert；Session 本身不持久化 | 满足 tool_calls FK 与 WAITING_TOOL 审计 |
| **DP-23** | Service 用每 run 局部 AgentWorkspaceFactory 注入锁定 snapshot + 结构化 file_map；不修改共享 strategy | 不改 execute 签名且避免并发 run 串状态 |
| **DP-24** | 多文件共享一个 run 级 FinalizeBudgetCoordinator；不按文件复制预留池 | 并发下仍只有一份真实 GlobalBudget |
| **DP-25** | 每次 provider 逻辑请求（含 JSON 修复）都占 round；tool attempts 另有硬上限 | 所有错误路径有界 |

若需推翻某条，只改本节与对应章节。

---

## 14. 冻结声明

**本文已完成 Round 1 审查修订并冻结，可按 T1 → T8 实施。** 仍禁止：

1. 不实现 V3-C 压缩、V3-D 质量对照、MCP。
2. 不改 `execute`，不改 V2-C/D/E，不改 `cross_run_match_key`。
3. 不把 Agent 默认打开。
4. 不让 stub `invoke(submit_finding)` 写入 Finding。
5. 不提交、不 push、不打 tag（除非用户明确要求）。

T1 本轮只交付可运行探针与 `DEFERRED_BY_USER` 证据，不读取或调用真实 `MODEL_API_KEY`。未来用户恢复 live 验证前，不得宣称 OQ-11 已由真实模型关闭，也不得默认打开 Agent。
