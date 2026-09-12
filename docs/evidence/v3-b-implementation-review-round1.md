# V3-B 实现审查（Round 1）

> 审查日期：2026-08-16  
> 审查结论：**CHANGES REQUIRED / 暂不验收**  
> 审查基线：`docs/architecture/24-v3b-alignment.md`（APPROVED）

## 1. 总结

V3-B 主体骨架已经完成：Agent 状态机、逐文件并发、共享 finalize 协调器、工具回灌、Candidate 接入、默认关闭 Agent、native 多调用配对等基础路径均已落地；现有全量测试与静态检查也能通过。

但本轮仍存在 **4 个必须一次性修复的 P1 契约问题**。其中三个会直接破坏有界循环、Action JSON 证据身份或预算账本，另一个缺失了对齐稿明确要求的 JSON 修复请求。因此当前不能标记 V3-B ACCEPTED。

## 2. 验证结果

| 检查 | 结果 |
|---|---|
| 全量 `pytest -q` | PASS（1 skipped） |
| `ruff check .` | PASS |
| `mypy reposage` | PASS（100 source files） |
| OQ-11 真实协议探针 | `DEFERRED_BY_USER`，符合用户明确后置决定，不作为本轮阻塞 |

说明：现有测试通过不等于契约通过；下列失败点目前没有对应的反例测试。

## 3. 必须修复（P1）

### P1-1：提前进入 grace 后没有限制 `grace_rounds`

位置：

- `reposage/review/agent/session.py:83-92`
- `reposage/review/agent/loop.py:120-131`

当前实现仅用 `max_rounds - grace_rounds` 决定“最晚何时进入 grace”，却没有记录 grace 已实际使用多少轮。若会话因为 `max_tool_calls` 或 `max_tool_attempts` 提前进入 grace，它可以继续消耗直到 `max_rounds`，实际 grace 轮数可能远大于配置值。

例：`max_rounds=8, grace_rounds=2`，第 1 轮即达到工具上限；当前实现允许第 2～8 轮共 7 个 grace 请求，而契约只允许 2 轮。

修复要求：

1. 会话显式记录 grace 起点或 `grace_rounds_used`；只在真正进入 grace 的状态边界初始化。
2. 每个 grace provider 逻辑请求计一次；达到 `grace_rounds` 后在发请求前停止为 `PARTIAL / budget`。
3. `grace_rounds=0` 时不得发 grace 请求。
4. 增加“因成功工具上限提前进入 grace”的反例测试，断言 provider 调用总数。

### P1-2：Action JSON 工具调用 ID 固定，重复调用会串写

位置：`reposage/providers/llm/openai_compat.py:593-622`

当前 ID 使用 `aj-{name}`。同一会话两次调用 `read_file` 会得到相同 `aj-read_file`：

- `session.evidence_refs` 后一次覆盖前一次；
- `submit_finding` 无法准确引用具体证据；
- ToolCall 持久化可能覆盖或冲突；
- 轨迹中不同逻辑调用失去唯一身份。

修复要求：每个 Action JSON 工具动作生成会话内唯一且稳定的 call ID（例如 provider 响应 ID + 序号，缺失时 UUID/单调序号），控制动作同样不能依赖固定名称 ID。新增连续两次同名工具、不同参数的测试，断言两个 ID、两条持久化记录和两份 evidence ref 均独立。

### P1-3：Action JSON 解析失败没有执行约定的一次修复请求

位置：

- `reposage/providers/llm/openai_compat.py:581-624`
- `reposage/review/agent/loop.py:242-274`

当前 `_action_json_response` 只返回 `parse_error`，loop 随后把它当作一次 idle；没有发出对齐稿 §5.3 要求的“最多一次修复请求”。同时也就没有实现“修复请求独立占 round、GlobalBudget、wallclock，探索期修复不得借 finalize”的计数语义。

修复要求：

1. 把修复编排放在能够逐次 reserve/settle 和增加 round 的层级，不能在一次 `tool_loop` 内偷偷重试。
2. 首次坏 JSON 后最多修复一次；修复前重新检查 round、当前模式对应预算和墙钟。
3. 探索期坏 JSON 的修复仍使用探索额度；不得因首次请求失败切入 grace 后借 finalize 修复。
4. 修复仍失败时回灌 `invalid_action` 并计 no-progress。
5. 增加：修复成功、修复仍失败、首轮已耗尽 max_rounds、探索额度不足四类测试。

### P1-4：预算结算非幂等，取消与 settle 竞态可重复入账

位置：

- `reposage/domain/models.py:573-591`
- `reposage/review/agent/budget.py:76-102`
- `reposage/review/agent/loop.py:166-239`

`BudgetReservation` 已有 `settled` 字段，但 `GlobalBudget.settle()` 没有检查它。loop 正常路径 settle 后，`CancelledError` 分支会再次调用 settle；尤其取消发生在 coordinator/global settle 的 await 边界时，存在一次结算已经修改全局账本、异常分支又结算一次的窗口。第二次会再次增加 `tokens_used/cost_used`。

修复要求：

1. 在 GlobalBudget 的同一把锁内检查并设置 settled，使 settle 真正幂等；重复 settle 不得再次释放或入账。
2. Coordinator 的 grace 账本也需保持同样的原子/幂等语义，避免全局已结算但 grace `_open` 尚未清理的不一致。
3. loop 使用单一的结算出口或明确的 settled guard；取消仍必须重新抛出。
4. 增加直接双 settle 测试，以及“取消发生在 settle 边界”的受控并发测试。

## 4. 同轮应补齐（P2，不建议再拆下一轮）

### P2-1：多 tool calls 的未执行动作没有计入 tool attempts

`reposage/review/agent/loop.py:418-425` 只为其余调用补 observation，没有增加 `tool_attempts`。对齐稿 §3.3 明确要求“每个模型要求的工具动作都 +1”。应对每个只读工具动作计 attempt；控制动作是否计数按对齐稿统一并用测试钉死。

### P2-2：异常被统一标成 `model_timeout`

`reposage/review/agent/loop.py:224-240` 将认证失败、400、协议错误等所有普通异常都停止为 `model_timeout`，会污染审计与评测。至少区分真实 timeout 与 `model_error`/`protocol_error`，并保留脱敏后的错误摘要。

### P2-3：验收测试与对齐稿清单仍未完全对应

当前取消测试名为 WAITING_TOOL，但实际主要取消在等待 LLM 响应阶段；父级取消测试也没有从存储中断言所有 sibling AgentTask 最终为 CANCELLED。请补齐：

- 真正阻塞在 `invoke` 内、状态已落库为 WAITING_TOOL 后取消；
- 父任务取消后查询存储，所有已启动 sibling 均清理完成且为 CANCELLED；
- 两个并发 review 使用不同 head SHA / diff，不串 workspace；
- 远程 snapshot 的 `sandbox_root=None` 端到端；
- 共享 finalize 池的并发竞争，而不只是顺序 reserve。

### P2-4：grace 进入日志重复输出

`should_enter_grace()` 在 mode 已为 grace 时永远返回 True，loop 每轮都会记录 `enter_grace`。请把进入 grace 做成一次状态边界事件，后续轮仅维持该模式。

## 5. 复验门槛

下一轮请一次性提供：

1. 上述 P1/P2 的代码与针对性测试；
2. 全量 pytest、Ruff、Mypy 结果；
3. 更新 `docs/evidence/v3-b-status.md`，但在复验前仍保持 `NOT ACCEPTED`；
4. 更新 V3-B compare，使其至少覆盖 grace 提前进入、JSON repair 计轮、同名工具唯一 ID、settle 幂等、WAITING_TOOL 取消和共享池并发。

复验通过前，不建议开始 V3-C 实现。
