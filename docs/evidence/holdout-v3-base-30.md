# V3 真实模型 30 题：Base 对照

> 主模型 `deepseek-v4-pro`；两个独立语义 Judge 均为 `deepseek-v4-flash`。Pipeline 产品 Judge 已关闭，避免它在看不到标准答案时提前过滤候选。

> 数据集为固定代码快照的跨文件上下文评测，本报告包含 30 题；本轮不是线上真实 PR 外部验证集。

| 指标 | Base | V3 |
|---|---:|---:|
| Judge 1 Precision | 0.472 | 0.8 |
| Judge 1 Recall | 0.708 | 1.0 |
| Judge 1 F1 | 0.567 | 0.889 |
| Judge 2 Precision | 0.472 | 0.8 |
| Judge 2 Recall | 0.708 | 1.0 |
| Judge 2 F1 | 0.567 | 0.889 |
| Judge 1 完全正确样本 | 15 | 24 |
| Judge 2 完全正确样本 | 15 | 24 |
| 双 Judge 一致样本 | 30/30 | 30/30 |
| 平均延迟 ms | 38620.19 | 34619.12 |
| 中位延迟 ms | 26980.35 | 26700.62 |
| 精确行定位召回 | 1.0 | 1.0 |
| ±5 行定位召回 | 1.0 | 1.0 |
| 任务正常完成样本 | 30/30 | 30/30 |
| 主链路模型调用 | 30 | 191 |
| 输入 token | 23576 | 661103 |
| 输出 token | 66440 | 80024 |
| 工具调用 | 0 | 106 |
| 工具有效率 | n/a | 0.716 |
| 重复调用率 | n/a | 0.0 |

> V3-Base: Precision +0.328，Recall +0.292，F1 +0.322。

## 严格错误明细

- Base 负样本误报：holdout-25-negative-decimal, holdout-26-negative-path, holdout-27-negative-permission, holdout-28-negative-tenant, holdout-29-negative-optional, holdout-30-negative-token
- Base 正样本漏检：holdout-15-canonical-host, holdout-16-tenant-scope, holdout-17-cache-namespace, holdout-18-optional-presence, holdout-19-stream-position, holdout-22-csrf-binding, holdout-24-resource-owner
- V3 负样本误报：holdout-25-negative-decimal, holdout-26-negative-path, holdout-27-negative-permission, holdout-28-negative-tenant, holdout-29-negative-optional, holdout-30-negative-token
- V3 正样本漏检：无

V3 本轮漏检 0 个正样本，负样本误报 6 个；样本 ID 以上述严格错误明细为准。

## 开销与口径

V3 主链路调用量是 Base 的 6.37 倍，输入 token 是 28.04 倍，输出 token 是 1.20 倍。未配置模型单价，因此报告 token 用量，不虚构美元成本。

合并报告使用同一配置下失败样本的定向重跑结果；延迟受重试批次和模型波动影响，适合看量级，不适合作为严格的配对性能结论。

> Pipeline 只执行结构、位置、证据和去重校验；最终语义由两个能看到标准答案的 Judge 独立裁决。
> execution_completed=true