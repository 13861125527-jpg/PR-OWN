# V2-D Alignment Review Round 1

日期：2026-08-16  
对象：`docs/architecture/21-v2d-alignment.md`  
结论：已直接修改对齐稿；当前状态为 **REVISED DRAFT / PENDING CONFIRMATION**。确认后可按 T1→T7 开始实现。

## 审查结论

V2-D 的总体方向成立：先冻结确定性聚类/指纹/去重，再在 merge 之后、置信度门槛之前加入可插拔 Judge。Judge 仍不是角色，不进 MultiRole，不改 Strategy 主签名，不改 V2-C 融合。

本轮没有要求进入编码，只修改设计对齐稿。

## 已修改的关键点

### 1. 修正前置状态

原稿写 V2-C “Round 1 B1/B2”，已改为：

- V2-C **ACCEPTED**
- 依据：`docs/evidence/v2-c-implementation-review-round2.md`

原因：V2-C 是 Round 2 才关闭 B1/B2 并验收。

### 2. 明确 Pipeline 不直接落库

原稿要求 Pipeline 记 task/usage，但没有说清楚谁写 SQLite，容易让实现方把 `FindingPipeline` 做成第二个编排器。

已改为：

- `FindingPipeline.process` 改为 async；
- 返回 `PipelineResult`；
- `PipelineResult` 包含 findings/tasks/usages/coverage_items/warnings/metrics；
- `ReviewService` 负责把结果合入 `ReviewRun` 并落库。

这保持了既有边界：Pipeline 负责 Finding 生命周期，Service 负责端到端编排和存储。

### 3. 补齐 Judge 遥测结果协议

原稿 `FindingAdjudicator` 只返回 `JudgeBatchOutput`，但后文又要求 task、usage、coverage、warning 落库，协议对不上。

已增加：

```text
JudgeAdjudicationResult
  output
  tasks
  usages
  coverage_items
  warnings
  partial
```

并要求 `LlmAdjudicator` 必须返回这些遥测信息。Fake 测试路径可以为空，但 LLM 路径不能省。

### 4. 修正 `needs_evidence` 计算时机

原稿说 evidence 校验后、进入 MERGED 前即可确定，这不严谨。

已改为：

- 在 cluster merge 之后、Judge 之前计算；
- 基于合并后的最终 `Finding.evidence` 判断；
- 如果某个候选自身无 verified 证据，但与静态结果/其它候选合并后获得 verified evidence，则最终 `needs_evidence=False`。

这避免误把已补证的合并结果降为 body_only。

### 5. 修正“重复率”口径

原稿公式：

```text
duplicate_rate = n_merged / n_raw
```

这个名字容易误导。`n_merged / n_raw` 不是“真实重复占比”，而是“重复候选经过聚合后还剩多少”的存活比例。

已改为：

```text
duplicate_survival_rate = n_merged / n_raw
dedup_collapse_rate = 1 - duplicate_survival_rate
```

并明确：

- 不再使用 `duplicate_rate` 作为字段名；
- V2 DoD `< 0.25` 若沿用，指 `duplicate_survival_rate < 0.25`；
- 该门槛只用于 V2-D 故意堆重复的标注样本，不用于 V1 demo 或普通 single_pass 全局数据。

## 当前仍保留的设计决定

- Judge 默认关闭：`review.judge.enabled=false`。
- Judge 只输出并应用 `keep/downrank`。
- Judge 不能新增 Finding、不能拆簇/并簇、不能改 canonical path/line、不能改 verified、不能改 fingerprint。
- `needs_evidence` 是程序内部字段，不进 LLM schema，不进 Judge schema。
- `judge.enabled=false` 时发布集合必须与 V2-C 兼容。
- V2-A/B/C compare 必须显式钉死 `judge.enabled=false`。
- V2-D 不做 Agent、工具循环、反馈记忆、watermark、GitHub blob API。

## 给后续实现方的执行重点

1. 先做 T1：冻结现有聚类/指纹/去重测试，不要先接 LLM。
2. 再做 T2：`needs_evidence` + SQLite v4→v5，必须保证 Judge 关闭时兼容 V2-C。
3. 再做 T3/T4：Fake Adjudicator 和 LlmAdjudicator，重点测“不能改事实”和 fail-open。
4. 最后做 T6/T7：`v2d_compare`、A/B/C compare 回归、pytest/ruff/mypy。

## 最终判断

V2-D 对齐稿已从“方向可行但有实现歧义”修改为“可交给实现方按任务卡执行”的状态。

建议下一步：用户确认后，按 T1→T7 编码；不要在 V2-D 中临时加入发布、GitHub、Agent 或真实模型质量评测。
