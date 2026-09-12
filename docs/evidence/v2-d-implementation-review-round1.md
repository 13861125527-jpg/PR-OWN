# V2-D Implementation Review Round 1

日期：2026-08-16  
对象：V2-D 聚类/去重冻结 + Judge keep/downrank 实现  
结论：**ACCEPTED / 可验收通过**

## 总结

V2-D 实现基本对齐 `docs/architecture/21-v2d-alignment.md`：

- `FindingPipeline.process` 已改为 async，并返回 `PipelineResult`；
- Pipeline 不直接写 SQLite，仍由 `ReviewService` 统一落库；
- Judge 是 Pipeline 阶段的可插拔裁决器，不是 Role；
- Judge 只应用 `keep/downrank`，不能改 canonical、verified、fingerprint、新增 Finding；
- `needs_evidence` 是程序内部字段，并已持久化；
- `duplicate_survival_rate` / `dedup_collapse_rate` 已进入 metrics 和 V2-D compare；
- V2-A/B/C compare 未被 Judge 污染。

本轮没有发现阻断问题。

## 关键审查点

### 1. PipelineResult 边界

状态：通过。

证据：

- `reposage/review/pipeline.py` 定义 `PipelineResult` / `PipelineMetrics`。
- `FindingPipeline.process(...)` 返回 findings/tasks/usages/coverage/warnings/metrics。
- `reposage/review/service.py` 接收 `pipe` 后统一调用 Storage 落库。

判断：

符合 V2-D 对齐稿要求：Pipeline 是 Finding 生命周期所有者，但不是第二个编排器/存储层。

### 2. Judge 协议与应用限制

状态：通过。

证据：

- `reposage/review/judge.py` 定义 `JudgeDecision`、`JudgeBatchOutput`、`JudgeAdjudicationResult`、`FindingAdjudicator`。
- `apply_judge_decisions()` 只处理 `keep/downrank`。
- 未知 id、重复 id、缺席 id 都按 fail-open / keep 处理。
- 模型夹带 path、verified、needs_evidence、新 findings 不会写入 Finding。

判断：

符合“Judge 只能裁决，不能改事实”的边界。

### 3. LlmAdjudicator

状态：通过。

证据：

- `reposage/review/adjudicator.py` 使用 `LLMProvider.complete(..., schema=JudgeBatchOutput)`。
- 未复用 `structured()` Finding 信封。
- 每个 Judge 分块生成 `ReviewTaskKind.JUDGE_ADJUDICATE`。
- usage 记录 `role="judge"`，并写入 `prompt_hash` / `schema_hash`。
- 预算不足、timeout、schema 失败走 fail-open，并返回 warning/coverage/partial。

额外端到端探针结果：

```text
completed ['accepted']
tasks: file_review + judge_adjudicate
usages: test + judge
pipeline detail: judge_enabled=true, judge_keep=1, judge_downrank=0
```

### 4. `needs_evidence`

状态：通过。

证据：

- `Finding.needs_evidence` 已加入 domain model。
- SQLite `findings.needs_evidence` 已加入 schema。
- 迁移版本 v4→v5 已实现。
- Judge 开启时，`needs_evidence=True` 的 MERGED finding 会转为 `BODY_ONLY`，且不会送入 Adjudicator。
- Judge 关闭时，只打标，不改变 V2-C 发布兼容行为。

判断：

符合 V2-D “V2 只降级，不触发 V3 补证”的约束。

### 5. 重复存活率指标

状态：通过。

证据：

- `compute_duplicate_survival_rate(n_raw, n_merged)` 已实现。
- `PipelineMetrics` 使用 `duplicate_survival_rate` / `dedup_collapse_rate`，没有继续使用误导性的 `duplicate_rate` 字段。
- `v2d_compare` 报告中明确写明该指标不是“真实重复占比”。

V2-D compare 结果：

```text
确定性去重 passed=2/2
Judge passed=3/3
全部通过：True
```

### 6. V2-A/B/C 兼容

状态：通过。

证据：

- V2-A compare 显式关闭 `judge.enabled=false`。
- V2-B / V2-C compare 仍使用 `FindingPipeline` 默认 Judge 关闭。
- V2-A/B/C compare 均命令成功。
- V2-C compare：转换 3/3，融合/去重 5/5。

## 测试门禁

| 门禁 | 结果 |
|---|---|
| Focused tests：`test_judge.py test_pipeline.py test_service.py test_storage.py` | PASS |
| Full pytest | PASS，456 passed |
| Ruff | PASS |
| MyPy strict | PASS，74 source files |
| V2-D compare | PASS，dedup 2/2，Judge 3/3 |
| V2-A compare | PASS |
| V2-B compare | PASS |
| V2-C compare | PASS |

## 非阻断建议

### P2：`judge_adjudicate` 日志事件的 keep/downrank 仍显示 pending

位置：

- `reposage/review/adjudicator.py`

现状：

`LlmAdjudicator.adjudicate()` 在返回给 Pipeline 前打日志：

```text
n_in=1 n_keep=pending n_downrank=pending
```

而实际 keep/downrank 是 Pipeline 调用 `apply_judge_decisions()` 后才计算出来的，因此 PIPELINE stage detail 是准确的，但 `judge_adjudicate` 结构化日志不够准确。

建议：

后续可把 `judge_adjudicate` 的最终 n_keep/n_downrank 日志挪到 Pipeline 应用决策之后，或增加一个 `judge_apply` 事件。

这不阻断 V2-D，因为：

- task/usage/coverage/stage detail 已准确落库；
- 审查结果和发布集合不受影响；
- 只是观测日志细节。

## 最终判断

V2-D Round 1：**通过验收**。

建议进入下一阶段前只允许 bugfix，不建议继续在 V2-D 加真实 API 质量评测、GitHub 接入、Agent 工具循环、反馈记忆或发布逻辑。
