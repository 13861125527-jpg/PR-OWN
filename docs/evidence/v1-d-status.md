# V1-D 状态说明（Round 14 返工后，2026-08-14）

> **V1-D 里程碑状态：代码回归通过；V1 真实质量门槛已通过，整轮执行完整性未过 → 暂不验收**
>
> 第十四轮指出计量实现的三个聚合语义错误，本轮全部修复：
> - **P1 延迟聚合公式**：拆分 `run_latency`（端到端 repeat 单位，加权平均）与
>   `provider_latency`（usage 单位），修复 repeats>1 平均值未乘回次数、单位混用、
>   失败耗时重复计算三个错误；
> - **P1 失败成本回写**：失败定价回写 `usage.cost_usd` 与 `ReviewTask.cost_usd`，
>   预算账 = usage 明细账 = task 账三者一致；
> - **P2 全失败 pricing**：失败分支同步 pricing_status（known/unknown），全失败
>   不再保持 uninitialized。

## 1. 已通过项

| 项 | 状态 |
|---|---|
| Pytest | **291 passed** |
| Ruff | 通过 |
| mypy strict | 通过（44 源文件） |
| 脚本化评测（Pipeline 确定性回归） | 20 样本门槛通过 |
| V1 真实质量门槛 | **已通过**（Precision 0.825 / Recall 0.950 / Position 0.938） |
| P1 延迟口径 | **已修复**：run_latency / provider_latency 分组、加权平均、不双计 |
| P1 失败成本回写 | **已修复**：预算账 = usage 账 = task 账一致 |
| P2 全失败 pricing | **已修复**：全失败也同步 known/unknown，不再 uninitialized |

### 前轮已确认有效项

- 失败成本进总账 + 请求数口径 + 防覆盖（十三轮）；paired-success + 失败 usage 保留（十二轮）；
- file_path 契约/安全边界（九/十轮）；诊断轨迹 + retries/schema_repairs（八轮）；
- 定价状态分侧（六轮）；overrun 熔断（四轮）；证据门控；确定性 baseline 标注。

## 2. 剩余阻断

baseline 的 `s08-multi-defect` 结构化输出失败，导致 `execution_completed=false`。

**下一步**：
1. `python -m reposage.evals.real_compare --sample-ids s08-multi-defect`
   （自动写 `v1-d-debug-s08-multi-defect.{json,md}`）重试确认是否偶发；
2. 完整重跑 20 样本真实对照，要求 `execution_completed=true` 且 `quality_gate_passed=true`；
3. 复跑代码门禁并提交，记录最终 SHA。

## 3. 结论

V1-D：**V1 真实质量门槛通过、代码门禁通过、评测计量口径已完整修正；
整轮执行完整性（baseline s08 失败）未通过 → 暂不最终验收**。待单样本确认 + 完整重跑后归档。
