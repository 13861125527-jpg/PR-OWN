# V3 真实模型 30 题：Base 对照

> 主模型 `deepseek-v4-pro`；两个独立语义 Judge 均为 `deepseek-v4-flash`。Pipeline 产品 Judge 已关闭，避免它在看不到标准答案时提前过滤候选。

> 数据集为固定代码快照的跨文件上下文评测，共 30 题（24 个正样本、6 个负样本）；本轮不是线上真实 PR 外部验证集。

| 指标 | Base | V3 |
|---|---:|---:|
| Judge 1 Precision | 0.767 | 0.95 |
| Judge 1 Recall | 0.958 | 0.792 |
| Judge 1 F1 | 0.852 | 0.864 |
| Judge 2 Precision | 0.767 | 0.95 |
| Judge 2 Recall | 0.958 | 0.792 |
| Judge 2 F1 | 0.852 | 0.864 |
| Judge 1 完全正确样本 | 23 | 24 |
| Judge 2 完全正确样本 | 23 | 24 |
| 双 Judge 一致样本 | 30/30 | 30/30 |
| 平均延迟 ms | 49871.44 | 49024.25 |
| 中位延迟 ms | 32755.85 | 35422.4 |
| 精确行定位召回 | 1.0 | 0.792 |
| ±5 行定位召回 | 1.0 | 0.792 |
| 任务正常完成样本 | 30/30 | 30/30 |
| 主链路模型调用 | 30 | 160 |
| 输入 token | 28381 | 542158 |
| 输出 token | 66502 | 84837 |
| 工具调用 | 0 | 88 |
| 工具有效率 | n/a | 0.727 |
| 重复调用率 | n/a | 0.0 |

> V3-Base: Precision +0.183，Recall -0.166，F1 +0.012。

## 严格错误明细

- Base 负样本误报：l3-14-sql-identifier, l3-25-negative-none, l3-26-negative-ms, l3-27-negative-escape, l3-30-negative-lock
- Base 正样本漏检：l3-24-retry-safety
- V3 负样本误报：l3-30-negative-lock
- V3 正样本漏检：l3-02-milliseconds, l3-07-raises-missing, l3-08-empty-rejected, l3-09-one-based, l3-10-negative-invalid

V3 的 5 个漏检都完成了跨文件读取，但把 helper 当前行为理解成有意的接口变更；严格标准答案要求 `process` 保持旧行为。因此它们按漏检计入，不能因解释合理就改成正确。V3 唯一负样本误报是 `l3-30-negative-lock`，忽略了调用方已经持锁的上下文。

## 开销与口径

V3 主链路调用量是 Base 的 5.33 倍，输入 token 是 19.10 倍，输出 token 是 1.28 倍。未配置模型单价，因此报告 token 用量，不虚构美元成本。

合并报告使用同一配置下失败样本的定向重跑结果；延迟受重试批次和模型波动影响，适合看量级，不适合作为严格的配对性能结论。

> Pipeline 只执行结构、位置、证据和去重校验；最终语义由两个能看到标准答案的 Judge 独立裁决。
> execution_completed=true