# 真实模型 V1 vs 直拼 Prompt baseline 对照

> 模型 `deepseek-v4-pro`  dataset `reposage/evals/datasets/resume_v123_40.yaml`  repeats=1  temperature=0.1  max_output_tokens=3000

## 质量（macro 平均）

| 指标 | V1 | baseline |
|------|----|----------|
| precision | 1.0 | 1.0 |
| recall | 1.0 | 1.0 |
| f1 | 1.0 | 1.0 |
| position_accuracy | 1.0 | 1.0 |
| negative_noise | 0.0 | 0.0 |

## 成本 / 延迟

| 项 | V1 | baseline |
|----|----|----------|
| total_cost_usd | 0.0 (unknown) | 0.0 (unknown) |
| avg_cost_per_invocation_usd | 0.0 | 0.0 |
| logical_invocations | 1 | 1 |
| provider_requests | 1 | 1 |
| failed_logical_invocations | 0 | 0 |
| input_tokens | 756 | 557 |
| output_tokens | 937 | 647 |
| retries | 0 | 0 |
| schema_repairs | 0 | 0 |
| 延迟(run 成功)均值 ms | 15123.57 | 10920.9 |
| 延迟(run 失败)总计 ms | 0.0 | 0.0 |
| 延迟(provider)均值 ms | 15122.0 | 10920.0 |

> paired-success: 1 样本（r01-sql-injection）

> execution_completed=True  quality_gate_passed=True  comparison_result=equal