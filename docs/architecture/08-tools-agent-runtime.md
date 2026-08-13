# 08 — 工具与 Agent Runtime

> 工具 Schema、registry、Agent loop 完整伪代码、预算、取消、会话压缩、终局验证、tool calling 双方案。
> 标注：`V1 now` / `V2 later` / `V3 later` / `optional`。

---

## 1. 工具清单（全部只读，V3）

| 工具 | 用途 | 版本 |
|------|------|------|
| `read_diff` | 读取指定文件的 diff hunk（含行号映射） | V3 |
| `read_file` | 读文件指定行区间（沙箱+截断） | V3 |
| `find_files` | 按名称/glob 找文件（仓库根内） | V3 |
| `search_code` | 按符号名/正则搜索代码（FTS/正则索引） | V3 |
| `find_references` | 找符号引用点（Tree-sitter/正则） | V3 |
| `submit_finding` | 提交一条候选 Finding（= 流水线入口） | V3 |
| `finish_review` | 结束本任务审查，进入终局验证 | V3 |

> 工具是**受控应用能力**；MCP 只作可降级扩展（§8）；Skill/规则文档用于按语言/路径动态加载规则，**不等于工具**（术语见附录 A）。

## 2. 工具定义与 JSON Schema 示例（契约级）

### read_file
```json
{
  "name": "read_file",
  "description": "读取仓库内文件指定行区间，用于验证上下文。",
  "parameters": {
    "type": "object",
    "properties": {
      "path": {"type": "string", "description": "仓库相对路径"},
      "start_line": {"type": "integer", "minimum": 1},
      "max_lines": {"type": "integer", "maximum": 200}
    },
    "required": ["path"]
  },
  "permissions": ["read_only"],
  "result_limit": "200 行 / 4000 tokens",
  "timeout_s": 10
}
```

### find_references
```json
{
  "name": "find_references",
  "description": "查找符号在仓库中的引用位置（结果含文件+行+截断片段）。",
  "parameters": {
    "type": "object",
    "properties": {
      "symbol": {"type": "string"},
      "scope": {"type": "string", "enum": ["repo", "file"], "default": "repo"}
    },
    "required": ["symbol"]
  },
  "permissions": ["read_only"],
  "result_limit": "50 条",
  "timeout_s": 15
}
```

其余工具遵循同构：`name / description / parameters(JSON Schema) / permissions / result_limit / timeout_s`。

## 3. 工具 Registry

- 注册：声明式（装饰器/元数据），`name` 唯一；schema 由 registry 生成并注入模型调用。
- 校验：参数用 JSON Schema validator（jsonschema 库）；非法 → `error=invalid_args` 回喂，**计入 attempt_count 与轮数预算**（P1-5）；只有成功执行才计入 successful_tool_calls。
- 沙箱：所有路径解析 `(repo_root / path).resolve()` 后必须 `is_relative_to(repo_root)`；拒绝绝对路径、`..`、符号链接逃逸（见 `10` §4）。
- 结果截断：`result_limit` 截断 + 来源标记；截断明文告知模型。
- 错误协议：`{status: ok|error|invalid_args|timeout|cancelled, data?, error?, truncated?, source?}`。
- 审计字段：tool_call_id、task_id、run_id、耗时、tokens、repeat_of。

## 4. Agent Loop 完整伪代码（V3）

```text
async def agent_loop(task: ReviewTask, ctx: ReviewContext, budget: AgentBudget) -> AgentOutcome:
    session = AgentSession(task_id=task.id, tools=registry.enabled())
    session.add(system=L0 治理 + 工具协议 + 预算声明)
    session.add(user=任务上下文 L1/L2 分块 + 目标)
    terminal = False
    while not terminal:
        # 安全点 1：预算与外部事件
        check_budget(session, budget)            # 超限 → grace 模式
        check_cancel()                            # 外部取消 → 安全点处理
        # 等待型 IO：模型调用
        try:
            resp = await llm.tool_loop(
                messages=session.messages,
                tools=registry.schemas(),
                budget_left=budget.remaining(),
            )
        except ModelTimeout:
            session.record(model_timeout)
            # P0-6：网络/限流超时走有限 retry policy，不借用 grace
            if retry_count < max_retries: retry_count += 1; continue
            return AgentOutcome(status=PARTIAL, reason="model_timeout")
        budget.consume(resp.usage)

        match resp.action:
            case ToolCall(name, args):
                if not registry.has(name):
                    session.observe(error=unknown_tool); continue
                if is_repeat(name, args, session):   # 重复调用检测
                    session.observe(error=repeat, hint=previous_result_ref)
                    repeat_count += 1
                    if repeat_count > budget.repeat_threshold:
                        return AgentOutcome(status=PARTIAL, reason="repeat_loop")
                    continue
                budget.consume_tool()
                try:
                    result = await sandbox_execute(name, args)   # 校验→沙箱→执行→截断
                except ToolError as e:
                    session.observe(error=str(e)); continue
                session.add(tool_observation(result))            # 观察回灌
                session.evidence_index.update(result.evidence)
            case SubmitFinding(f):
                candidate = normalize_candidate(f)              # 程序归一
                if validate_basic(candidate):
                    pipeline.enqueue(candidate)                 # 进入统一流水线
                    session.add(ack(candidate.id))
                else:
                    session.observe(error=invalid_finding)
            case FinishReview(reason):
                terminal = True
            case None:
                # 既无工具调用也无完成动作
                if resp.text 无新增信息（与上轮重复）:
                    idle_count += 1
                    if idle_count > 2: return AgentOutcome(status=PARTIAL, reason="no_progress")
                else:
                    # P1-6：只保存结构化决策摘要，不保存/不要求隐含思维链
                    session.add(decision_summary(resp.structured_summary))
            case _:  # 未知动作
                session.observe(error=unknown_action); continue

        session.maybe_compact(budget)              # 上下文超限 → 压缩（见 §8）
        # 安全点 2：模型/工具调用完成后再处理新事件与取消
    # 终局统一验证（§7）
    return finalize(session, pipeline.pending())
```

### 关键分支处理

| 场景 | 处理 |
|------|------|
| 多工具调用并行 | **不支持**：单 Agent 轨迹内顺序执行（默认）；并行仅存在于任务级。若模型一次返回多个 tool_call，取第一个执行并回灌，其余记入待办提示（V3 初版策略） |
| 无工具调用也无完成 | idle 检测 → 计数超阈值强制终止（PARTIAL + reason） |
| 重复/无效工具调用 | repeat 检测 + invalid_args 回喂；**计入 attempt_count 与轮数预算**（P1-5），超阈值终止 |
| 工具异常/模型网络超时 | 错误协议回喂；走有限 retry policy，不消耗 finalize 预留 |
| Context 超限 | maybe_compact 触发压缩（§8） |
| 外部取消 | 安全点检查；CANCELLED，已提交 findings 保留 |
| 探索额度耗尽 | 进入 grace：只允许 submit_finding（已有证据）与 finish_review；消耗预留额度，不可新开工具 |
| submit_finding / finish_review | 见上；终局后统一验证 |

## 5. 预算体系（AgentBudget）

### 硬预算内预留收尾额度（P0-6）

```text
total_hard_budget
├── exploration_budget      # 探索/取证可用
└── reserved_finalize_budget  # 预先保留，仅终局/grace 可用
```

- 探索额度耗尽才进入 finalize/grace 模式；finalize 只能使用预留额度，**任何路径都不会突破 total_hard_budget（Token 维度）**。
- 墙钟硬超时与外部取消通常**不能再调用模型**（直接进入收尾）；已在途请求取消，**迟到响应不进入审查结果，但 usage/cost 仍记账为 late_cancelled**（成本统计不低于真实账单，P0-R2-3 补充）。
- 网络超时/限流走有限 retry policy，**不借用 grace 概念**（grace 只对应"探索额度耗尽"，不是"超时重试"）。
- **费用为保守估算（P0-R2-3）**：发送前按 `input_tokens + max_output_tokens` 做最坏预留，超预留则拒绝（admission control）；retry 先预留再重试；拿不到价格/usage 时成本状态标记 `unknown/unverified`，不假装精确。

| 维度 | 初值 | 语义 |
|------|------|------|
| max_rounds | 8 | 模型调用轮数上限（含 finalize 轮） |
| max_tool_calls | 12 | 成功工具调用上限；`attempt_count`（含无效/重复）单独计入轮数与无进展预算（P1-5） |
| max_tokens | 继承 run 预算 | Token 硬上限（发送前按 input+max_output 预留） |
| max_cost_usd | 继承 run 预算 | 费用**估算上限**（admission control，非事后精确） |
| reserved_finalize_tokens / _usd | 总预算 10% | 预先保留，探索不可动用 |
| max_wallclock_s | 300（任务级） | 墙钟硬上限，到时不再调用模型 |
| grace_rounds | 2 | 探索额度耗尽后的受限轮（仅用预留额度） |
| repeat_threshold | 3 | 连续重复调用终止 |

超预算后的 **grace round** 规则：不可发起新工具调用、不可加载新上下文；仅允许 `submit_finding(已有证据)` 与 `finish_review`，消耗 `reserved_finalize_budget`。

## 6. 会话压缩（V3）

- 触发：`session.messages` 估算 token > 预算 60% 或单条消息超长。
- 压缩：已消费 tool 观察聚合为摘要（保留 `tool_call_id ↔ 结论`），`checked/excluded/pending` 集合序列化，`evidence_index` 完整保留（可追溯性优先）。
- 回灌：压缩后的摘要作为 user 消息插入，标记 `is_compressed=true`；模型被明确告知"以下为已压缩历史摘要"。

## 7. 终局统一验证（运行结束后）

finish_review 之后、状态置 COMPLETED 之前，程序执行：

1. 全部候选 Finding 重跑 schema/location/evidence 校验（与 V1 同一 Pipeline）。
2. 验证证据可追溯：每条保留 Finding 必须能定位到 diff 行或工具结果；否则降置信度或 suppressed。
3. 校验无"未消费的工具调用"残留（WAITING_TOOL 非法终局）。
4. 生成 AgentOutcome：status、findings、轨迹摘要、budget 使用、失败原因。

## 8. MCP 与 Skill 的定位（可降级扩展）

- **MCP 工具**：optional；仅作为外部工具源的协议适配层，接入同样走 registry 校验 + 沙箱 + 截断 + 来源标记；MCP 服务器不可用时自动降级为内置工具集，不影响核心审查。
- **Skill/规则文档**：按语言/路径动态加载到 L4 上下文（如 `rules/python/security.md`），是知识不是工具；不具备执行能力。
- 明确区分：应用内部模块（代码）≠ 模型 tool calling（schema 驱动）≠ MCP（协议）≠ Skill（知识）。

## 9. DP-V4-PRO 不支持 tool calling 时的双方案（V3）

### 方案 A：更换支持工具调用的模型（推荐路径）
- 通过 `LLMProvider` 抽象热切换（如 OpenAI-compatible 且支持 native tool calling 的模型）。
- 代价：模型行为差异需重新评测；成本可能变化。Agent 层代码不变（协议一致）。

### 方案 B：受限 Action JSON 协议（保守路径）
- 不依赖 native tool calling：模型输出 `{"action": "tool_call", "name": "read_file", "args": {...}}` 的结构化 JSON；程序解析、执行、回灌为普通 user 消息。
- 风险：模型可能"扮演工具"或输出格式漂移 → 程序严格校验 + 单次修复重试；幻觉路径由沙箱拦截。
- 代价：可靠性依赖模型 JSON 能力；多轮一致性弱于 native tool calling；需实测评估。

### 比较

| 维度 | A 换模型 | B 受限协议 |
|------|----------|-----------|
| 依赖 | 需模型支持 tool calling | 仅需稳定 JSON |
| 实现量 | 少（协议一致） | 中（解析/回灌/修复） |
| 可靠性 | 高 | 中（依赖 JSON 稳定度） |
| 与现有栈兼容 | 需切换模型 | 可继续 DP-V4-PRO |
| 风险 | 模型切换引入评测回归 | 空转/重复调用防护需更强 |

**决策触发（P0-6/版本安排）**：V3 启动前完成 `14` OQ-1 的 DP-V4-PRO tool calling 实测，并在**同一评测集**上对比原生工具模型（方案 A）与 DP-V4-PRO 的 Action JSON 协议（方案 B）；若方案 B 的 JSON 可靠性不足，允许 V3 暂缓，而不是为保留首选模型牺牲 Agent 稳定性。方案选择以评测证据为准，不预设默认。
