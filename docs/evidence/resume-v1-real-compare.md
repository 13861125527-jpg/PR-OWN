# 真实模型 V1 vs 直拼 Prompt baseline 对照

> 模型 `deepseek-v4-pro`  dataset `reposage/evals/datasets/resume_v123_40.yaml`  repeats=1  temperature=0.1  max_output_tokens=3000

## 质量（macro 平均）

| 指标 | V1 | baseline |
|------|----|----------|
| precision | 0.679 | 0.758 |
| recall | 0.775 | 0.9 |
| f1 | 0.687 | 0.765 |
| position_accuracy | 0.7 | 0.867 |
| negative_noise | 4.0 | 6.0 |

## 成本 / 延迟

| 项 | V1 | baseline |
|----|----|----------|
| total_cost_usd | 0.0 (unknown) | 0.0 (unknown) |
| avg_cost_per_invocation_usd | 0.0 | 0.0 |
| logical_invocations | 43 | 40 |
| provider_requests | 48 | 45 |
| failed_logical_invocations | 0 | 0 |
| input_tokens | 34978 | 25589 |
| output_tokens | 75822 | 71533 |
| retries | 0 | 0 |
| schema_repairs | 5 | 5 |
| 延迟(run 成功)均值 ms | 26709.87 | 29312.79 |
| 延迟(run 失败)总计 ms | 0.0 | 0.0 |
| 延迟(provider)均值 ms | 27313.95 | 29312.28 |

> paired-success: 40 样本（r01-sql-injection, r02-path-traversal, r03-shell-injection, r04-unsafe-yaml, r05-weak-token, r06-timing-compare, r07-open-redirect, r08-log-secret, r09-zero-page-size, r10-empty-average, r11-off-by-one, r12-wrong-sort, r13-mutable-default, r14-finally-return, r15-swallow-timeout, r16-file-leak, r17-naive-quadratic, r18-blocking-async, r19-two-defects, r20-two-defects, r22-security-and-leak, r23-contract-none, r24-unit-mismatch, r25-auth-default, r26-sentinel-conflict, r27-config-string-bool, r28-key-normalization, r29-lock-copy, r30-retry-non-idempotent, r31-negative-rename, r32-negative-context-manager, r33-negative-parameterize, r35-negative-async-sleep, r36-negative-doc-string, r37-negative-test-fixture, r38-negative-lock, r40-negative-cross-file, r34-negative-empty-guard, r21-lock-and-await, r39-negative-cache）

> execution_completed=True  quality_gate_passed=False  comparison_result=degraded