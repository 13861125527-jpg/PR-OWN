# RepoSage V2 真实模型 40 题评测

> `deepseek-v4-pro`；MultiRole（general/security/correctness/performance）+ L3；40 题；Pipeline 阈值 0.0。

## 结果

| 指标 | V2 | 相对 V1 |
|---|---:|---:|
| Precision (macro) | 0.621 | -0.058 |
| Recall (macro) | 0.688 | -0.087 |
| F1 (macro) | 0.644 | -0.043 |
| 位置准确率（正样本 macro） | 0.6 | -0.1 |
| 负样本噪声 | 0 | -4.0 |

## 更可解释的缺陷级指标

| 指标 | V2 |
|---|---:|
| category + path + exact line | 0.588 |
| path + exact line（忽略 category） | 0.735 |
| path + ±5 lines（忽略 category） | 0.794 |

## 调用与延迟

| 项 | 数值 |
|---|---:|
| 成功样本 | 40/40 |
| 逻辑模型调用 | 53 |
| Provider 请求 | 55 |
| 输入 Token | 45281 |
| 输出 Token | 56649 |
| 平均端到端延迟 | 25745.45 ms |
| 中位延迟 | 22009.06 ms |
| L3 命中样本 | 0/40 |

> execution_completed=true

## 结论与限制

- 40/40 样本完成，最终报告中没有 fatal error 或角色任务失败。
- V2 的严格缺陷召回为 20/34（58.8%）；忽略 category 后，精确行号命中为 25/34（73.5%），±5 行命中为 27/34（79.4%）。差值说明仍有 5 个“位置正确、分类错误”的缺陷。
- 10 个负样本均未产生 Finding，negative noise 从 V1 的 4 降为 0；代价是 6 个正样本也没有产生 Finding，召回下降。
- 本轮角色实际调用为 general=43、security=8、correctness=2、performance=0。门控较保守，多角色没有覆盖所有缺陷类别。
- 虽然配置开启了 L3 symbol retrieval，但实际 L3 命中为 0/40，因此本轮只能作为 MultiRole 效果评测，不能据此判断 L3 上下文收益。需要补充能被符号引用检索命中的跨文件样本后单独复测 L3。
- macro recall 会把无误报的负样本记为 1.0，不能单独代表找错能力；简历和结论应优先使用缺陷级严格召回 20/34。
