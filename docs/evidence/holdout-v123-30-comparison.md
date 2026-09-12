# 30题留出集：BASE / V2 / V3 对比

> 主审模型 `deepseek-v4-pro`；两个独立 Judge 均为 `deepseek-v4-flash`；24 个正样本、6 个负样本。

| 指标 | BASE | V2 MultiRole + L3 | V3 Agentic |
|---|---:|---:|---:|
| Precision | 0.472 | 0.562 | 0.8 |
| Recall | 0.708 | 0.75 | 1.0 |
| F1 | 0.567 | 0.643 | 0.889 |
| 语义命中 | 17 | 18 | 24 |
| 报告缺陷 | 36 | 32 | 30 |
| 完全正确样本 | 15 | 18 | 24 |
| 双 Judge 一致样本 | 30/30 | 29/30 | 30/30 |
| 主链路模型调用 | 30 | 60 | 191 |
| 输入 Token | 23576 | 54436 | 661103 |
| 输出 Token | 66440 | 108040 | 80024 |

## 错误样本

- BASE 漏检：holdout-15-canonical-host, holdout-16-tenant-scope, holdout-17-cache-namespace, holdout-18-optional-presence, holdout-19-stream-position, holdout-22-csrf-binding, holdout-24-resource-owner
- V2 漏检：holdout-15-canonical-host, holdout-16-tenant-scope, holdout-17-cache-namespace, holdout-18-optional-presence, holdout-22-csrf-binding, holdout-24-resource-owner
- V3 漏检：无
- BASE 负样本误报：holdout-25-negative-decimal, holdout-26-negative-path, holdout-27-negative-permission, holdout-28-negative-tenant, holdout-29-negative-optional, holdout-30-negative-token
- V2 负样本误报：holdout-25-negative-decimal, holdout-26-negative-path, holdout-27-negative-permission, holdout-28-negative-tenant, holdout-29-negative-optional, holdout-30-negative-token
- V3 负样本误报：holdout-25-negative-decimal, holdout-26-negative-path, holdout-27-negative-permission, holdout-28-negative-tenant, holdout-29-negative-optional, holdout-30-negative-token