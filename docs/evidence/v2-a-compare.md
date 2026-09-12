# V2-A vs V1 对照（脚本化 Fake，非真实 API）

> 由 `python -m reposage.evals.v2a_compare` 生成；原始 JSON：`docs/evidence/v2-a-compare.json`。
> dataset=`reposage/evals/datasets/v1_demo.yaml` samples=20 repeats=3 real_api=not_run
> 脱敏：不含 API Key / 源码原文 / 密钥字面量。

## 质量（Finding，macro）

| 指标 | V1 single_pass | V2-A multi_role |
|------|----------------|-----------------|
| Precision | 1.0 | 1.0 |
| Recall | 1.0 | 1.0 |
| F1 | 1.0 | 1.0 |
| 位置准确率 | 1.0 | 1.0 |
| 负样本噪声 | 0.0 | 0.0 |

脚本化候选经同一 Pipeline。不要求 V2-A 全局 Precision 超过 V1。
质量相同是脚本化候选构造结果：Fake 把相同候选放在 V1 与 V2-A 的 general 角色，其他角色返回空；本表证明执行器/Pipeline/报告可重复，不代表多角色模型效果相同。

## 成本（Fake 记账）

| 指标 | V1 single_pass | V2-A multi_role |
|------|----------------|-----------------|
| 模型调用数 | 21 | 35 |
| input tokens | 2100 | 3500 |
| output tokens | 1050 | 1750 |
| total tokens | 3150 | 5250 |
| 预算拒绝次数 | 0 | 0 |

脚本化 Fake 记账，非真实 API 账单。

## 延迟（全数据集墙钟）

| 指标 | V1 single_pass | V2-A multi_role |
|------|----------------|-----------------|
| total_ms（均值） | 3.94 | 8.33 |
| p50_ms | 3.97 | 8.3 |
| p95_ms | 4.11 | 8.5 |

全数据集墙钟，repeats=3；Fake 延迟不代表真实模型。

## 辅助：Gate Precision/Recall（不替代 Finding 质量表）

| 角色 | Precision | Recall | TP/FP/FN |
|------|-----------|--------|----------|
| security | 1.0 | 1.0 | 7/0/0 |
| correctness | 1.0 | 1.0 | 4/0/0 |
| performance | 1.0 | 1.0 | 3/0/0 |

真实 API 对照：未运行。
