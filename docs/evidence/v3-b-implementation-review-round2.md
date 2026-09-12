# V3-B 实现复验（Round 2）

> 审查日期：2026-08-16  
> 审查结论：**ACCEPTED / V3-B 可验收**  
> 基线：`docs/architecture/24-v3b-alignment.md` + `docs/evidence/v3-b-implementation-review-round1.md`

## 1. 结论

本轮已闭合 Round 1 的 4 个 P1 与建议同轮处理的 P2 项。V3-B Agent Loop 的核心契约现在具备代码约束和测试证据：

- grace 轮次有独立计数，提前进入 grace 不再突破 `grace_rounds`。
- Action JSON 同名工具调用生成唯一 call id，不再串写证据和持久化记录。
- Action JSON parse error 现在由 loop 级修复请求处理，修复请求独立占用 round / budget / wallclock。
- `GlobalBudget.settle()` 与 coordinator grace 账本具备幂等语义，取消边界不会重复入账。
- WAITING_TOOL 取消、父级取消 sibling 清理、多 tool calls attempt 计数、异常分类、共享 finalize 池并发竞争均有测试覆盖。

OQ-11 live 真实 API 探针仍是用户明确后置项：`DEFERRED_BY_USER`，不阻塞 V3-B 验收。

## 2. 复验结果

| 检查 | 结果 |
|---|---|
| 全量 `pytest -q` | PASS（1 skipped） |
| `python -m reposage.evals.v3b_compare` | PASS（14/14） |
| `ruff check .` | PASS |
| `mypy reposage` | PASS（100 source files） |
| OQ-11 live | DEFERRED_BY_USER |

说明：第一次运行 compare / Ruff / Mypy 时受沙箱写权限影响失败；提权后同一命令通过。失败原因是无法写 `docs/evidence` / `.ruff_cache` / 类型检查缓存，不是代码失败。

## 3. Round 1 问题闭环

### P1-1 grace 轮次限制

状态：**已修复**

关键实现：

- `reposage/review/agent/session.py:74` 增加 `grace_rounds_used`。
- `reposage/review/agent/session.py:130` 增加一次性 `enter_grace()`。
- `reposage/review/agent/session.py:138` 增加 `grace_rounds_exhausted()`。
- `reposage/review/agent/loop.py:156` / `186` 在发请求前检查 grace 是否耗尽。
- `reposage/review/agent/loop.py:200` 在 grace provider 请求发出后计数。

测试证据：

- `tests/test_agent.py:517` `test_early_grace_caps_provider_rounds`
- `tests/test_agent.py:541` `test_grace_rounds_zero_sends_no_grace_request`

复验判断：提前因工具上限进入 grace 时，provider 调用被限制为探索轮 + `grace_rounds`，且 `enter_grace` 日志只记录一次。

### P1-2 Action JSON call id 唯一性

状态：**已修复**

关键实现：

- `reposage/providers/llm/openai_compat.py:582` 新增 `_new_action_json_id()`。
- `reposage/providers/llm/openai_compat.py:611` / `622` 对 control action 与 tool_call 统一生成唯一 ID。

测试证据：

- `tests/test_agent.py:627` `test_action_json_same_name_tools_get_unique_ids`

复验判断：连续两次同名 `read_file` 产生不同 `aj-*` ID，`session.evidence_refs` 与 `tool_calls` 持久化记录保持独立。

### P1-3 Action JSON 修复请求

状态：**已修复**

关键实现：

- `reposage/review/agent/session.py:75` / `76` 增加修复状态。
- `reposage/review/agent/loop.py:140` 修复 pending 时跳过 grace 切换逻辑。
- `reposage/review/agent/loop.py:174` 修复请求预算拒绝时直接停止，不借 finalize。
- `reposage/review/agent/loop.py:202` 修复请求占用一次 provider round。
- `reposage/review/agent/loop.py:316` parse error 后最多安排一次修复请求。

测试证据：

- `tests/test_agent.py:559` `test_action_json_repair_success_counts_round`
- `tests/test_agent.py:578` `test_action_json_repair_still_fails`
- `tests/test_agent.py:594` `test_action_json_repair_skipped_when_max_rounds_spent`
- `tests/test_agent.py:608` `test_action_json_repair_does_not_borrow_finalize`

复验判断：修复请求没有藏在 provider 内部，而是在 loop 层重新走 round / budget / wallclock 门禁；达到 `max_rounds` 或探索预算不足时不会额外发请求。

### P1-4 预算 settle 幂等

状态：**已修复**

关键实现：

- `reposage/domain/models.py:479` 记录实际结算值。
- `reposage/domain/models.py:586` 重复 settle 直接返回。
- `reposage/domain/models.py:588` 在同一锁内设置 `settled=True`。
- `reposage/review/agent/budget.py:98` coordinator 使用 reservation 的 settled usage 更新 grace 账本。
- `reposage/review/agent/loop.py:270` 正常 / 异常路径统一 finally settle。
- `reposage/review/agent/loop.py:355` 取消补结算依赖幂等保护。

测试证据：

- `tests/test_agent.py:674` `test_settle_is_idempotent`
- `tests/test_agent.py:687` `test_coordinator_grace_settle_is_idempotent`
- `tests/test_agent.py:701` `test_cancel_at_settle_boundary_does_not_double_count`

复验判断：重复 settle 不会重复增加 `tokens_used` / `cost_used`，取消边界补结算不会二次入账。

## 4. P2 闭环

| 问题 | 状态 | 证据 |
|---|---|---|
| 多 tool calls 未执行项计入 tool attempts | 已修复 | `reposage/review/agent/loop.py:475`；`tests/test_agent.py:327` / `742` |
| 非 Timeout 异常被误标 `model_timeout` | 已修复 | `reposage/review/agent/loop.py:58`；`tests/test_agent.py:755` / `775` / `792` |
| WAITING_TOOL 取消测试不足 | 已补齐 | `tests/test_agent.py:292` |
| 父级取消 sibling 状态未断言 | 已补齐 | `tests/test_agent.py:422` |
| 并发 review workspace 串状态风险 | 已补齐 | `tests/test_agent.py:802` |
| 共享 finalize 池并发竞争 | 已补齐 | `tests/test_agent.py:726` |
| grace 重复进入日志 | 已修复 | `AgentSession.enter_grace()` 返回边界布尔值，测试断言日志只出现一次 |

## 5. 非阻塞观察

- Action JSON 修复请求目前通过 loop 的 `json_repair_used/json_repair_pending` 记录，不额外把修复轮 usage 标成 `schema_repairs=1`。这不影响本轮对齐稿的硬要求，因为 V3-B 要求的是修复占 round / budget / wallclock；若后续监控希望区分 repair usage，可在 V3-D/V3-E 指标阶段补充。
- `v3b_compare` 运行时会输出结构化日志到控制台，功能无误；如果后续报告生成需要更干净的终端输出，可以给 compare 加 quiet 参数。

## 6. 验收建议

V3-B 可以标记为 **ACCEPTED**。下一步可以进入 V3-C，但真实 API 的 OQ-11 探针仍保持后置，不应在 V3-C 默认打开 Agent 或依赖真实模型。
