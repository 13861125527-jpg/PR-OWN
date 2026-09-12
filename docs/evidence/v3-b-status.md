# V3-B 状态说明

> **V3-B 里程碑状态：IMPLEMENTED / ACCEPTED**
>
> 对齐稿：`docs/architecture/24-v3b-alignment.md`（Round 1 已冻结）
> OQ-11：`docs/evidence/v3-b-oq11-protocol.md`（DEFERRED_BY_USER）
> 对照报告：`docs/evidence/v3-b-compare.md` / `.json`
> Round 1 审查：`docs/evidence/v3-b-implementation-review-round1.md`（CHANGES REQUIRED）
> Round 2 复验：`docs/evidence/v3-b-implementation-review-round2.md`（ACCEPTED）

## 本轮实现

| 卡 | 落点 |
|---|---|
| T1 | 协议探针脚本 + DEFERRED_BY_USER 证据；不读 Key、不调 API |
| T2 | `AgentSessionView` / `AgentToolRequest`；状态转换；共享 `FinalizeBudgetCoordinator` |
| T3 | `openai_compat.tool_loop` native + action_json；Fake 脚本化轨迹 |
| T4 | `agent_loop`：transcript 配对、invoke、attempt/round、grace/cancel/wallclock；task upsert |
| T5 | loop 拦截 submit/finish；Pipeline 认 `TOOL_AGENT`；handler 仍 `not_bound` |
| T6 | `AgentWorkspaceFactory` + `AgenticReviewer` + 每 run 局部选择器（enabled 双闸） |
| T7 | `prompts/tasks/agent.md` + `v3b_compare` |
| T8 | pytest / Ruff / mypy |

## Round 1 审查修复（已复验）

| ID | 修复 |
|---|---|
| P1-1 / P2-4 | `grace_rounds_used`；`enter_grace()` 只在状态边界记一次；达到 `grace_rounds` 后发请求前 `PARTIAL/budget` |
| P1-2 | Action JSON call ID 改为 `aj-{name}-{uuid}`，同名连续调用不再串写 |
| P1-3 | 解析失败最多一次 loop 级修复请求（占 round / 预算 / 墙钟）；探索期不借 finalize |
| P1-4 | `GlobalBudget.settle` 与 coordinator grace 账本幂等；loop 单一 finally 结算 + 取消补结算 |
| P2-1 | 多 tool_call 中未执行的只读动作计入 `tool_attempts`；控制动作不计 |
| P2-2 | `TimeoutError` → `model_timeout`；其余 `model_error` / `protocol_error` + 脱敏摘要 |
| P2-3 | WAITING_TOOL 取消、sibling CANCELLED 落库、并发 review 不串 workspace、远程 `sandbox_root=None`、共享池并发竞争 |

Agent 为 **experimental**。默认 `agent.enabled=false`。未提交。V3-B 已可验收，V3-C 可开始；真实 API OQ-11 仍后置。

## 门禁

| 门禁 | 结果 |
|---|---|
| 全量 pytest | PASS（1 个 symlink 环境性 skip） |
| Ruff | PASS |
| MyPy strict | PASS（100 source files） |
| V3-B compare | PASS（14/14） |
| OQ-11 live | DEFERRED_BY_USER |
