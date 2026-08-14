# 真实模型 V1 vs 直拼 Prompt baseline 对照

> 模型 `deepseek-v4-pro`  dataset `reposage/evals/datasets/v1_demo.yaml`  repeats=1  temperature=0.1  max_output_tokens=3000

## 质量（macro 平均）

| 指标 | V1 | baseline |
|------|----|----------|
| precision | 0.825 | 0.789 |
| recall | 0.95 | 0.895 |
| f1 | 0.833 | 0.789 |
| position_accuracy | 0.938 | 0.867 |
| negative_noise | 0.0 | 0.0 |

## 成本 / 延迟

| 项 | V1 | baseline |
|----|----|----------|
| total_cost_usd | 0.0 (unknown) | 0.0 (unknown) |
| avg_cost_per_repeat_usd | 0.0 | 0.0 |
| model_calls | 21 | 19 |
| input_tokens | 16014 | 10412 |
| output_tokens | 29853 | 21196 |
| retries | 0 | 0 |
| schema_repairs | 1 | 0 |
| 端到端延迟均值 ms | 17853.55 | 13492.07 |

> execution_completed=False  quality_gate_passed=True  comparison_result=improved

## 失败样本（脱敏）

- `baseline` StructuredOutputError: 结构化输出解析失败（修复重试后仍失败）