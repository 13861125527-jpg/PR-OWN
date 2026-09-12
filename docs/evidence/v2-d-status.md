# V2-D 状态说明

> **V2-D 里程碑状态：ACCEPTED**（Round 1：`docs/evidence/v2-d-implementation-review-round1.md`）
>
> 对齐稿：`docs/architecture/21-v2d-alignment.md`
> 对照报告：`docs/evidence/v2-d-compare.md` / `.json`

## 本轮实现

| 卡 | 落点 |
|---|---|
| T1 | 聚类先于 fingerprint 单测；`compute_duplicate_survival_rate`；V2-C 融合回归保持 |
| T2 | `Finding.needs_evidence`；SQLite `user_version` 4→5；Judge 开启时无 verified 证据 → `body_only`，关闭时只打标走原门槛 |
| T3 | `FindingAdjudicator` / Fake；`PipelineResult`；`process` 改为 async；keep/downrank 禁写 canonical |
| T4 | `LlmAdjudicator`：`complete`+`JudgeBatchOutput`、预算、model semaphore、超时/取消、tasks/usages |
| T5 | `review.judge` 默认 False；Service 注入；PIPELINE stage 写 survival 口径；关则零 Judge 任务 |
| T6 | `v2d_dedup.yaml` + `v2d_compare`；V2-A/B/C 钉死 `judge.enabled=false` |
| T7 | pytest / Ruff / mypy strict 全绿 |

## 对照（程序指标）

确定性去重 passed=2/2（五条重复候选 survival=0.2 < 0.25；不同 trigger 不合并）。
Judge Fake passed=3/3（downrank / keep / fail-open）。不宣称真实模型质量收益。

## 明确非目标

- Agent / 工具 / 反馈记忆 / watermark
- Judge 拆簇或改 V2-C 融合（含 B2）
- 默认打开 `judge.enabled`（仍为 False）
- 把 `needs_evidence` 做成模型输出

## 遗留（非阻断）

P2：`judge_adjudicate` 日志在 Pipeline 应用决策前打印 `n_keep=pending`。不重开 V2-D；不纳入 V2-E DoD。

## 门禁

pytest / Ruff / mypy strict 全绿。未提交。

下一阶段：`docs/architecture/22-v2e-alignment.md`（DRAFT）。
