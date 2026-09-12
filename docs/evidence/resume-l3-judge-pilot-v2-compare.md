# L3 + Semantic Judge Pilot 对照

## 范围

同一 `deepseek-v4-pro`、同一宽门控 MultiRole + L3 配置，选择3个正样本和6个负样本。对照仅切换扩展后的 Semantic Judge。

## 结果

| 指标 | 无 Judge | Semantic Judge | 变化 |
|---|---:|---:|---:|
| Macro Precision | 38.9% | 66.7% | +27.8个百分点 |
| Macro Recall | 100% | 100% | 持平 |
| Macro F1 | 44.4% | 66.7% | +22.3个百分点 |
| 严格缺陷命中 | 3/3 | 3/3 | 持平 |
| 负样本误报 | 6 | 3 | -50% |
| Accepted Findings | 12 | 6 | -50% |

三个正样本 `inclusive-bound`、`tuple-order`、`redaction` 均从“两条不同类别/表述的报告”合并为一条严格正确 Finding，单题 F1 从 0.667 提升到 1.0，没有损失召回。

负样本 `negative-escape` 的两条误报被全部压制；`negative-await` 从两条误报压为一条。`negative-none` 与 `negative-iterator` 两组均保持零误报。

## 可靠性限制

- 最终9个样本中7个样本完整执行。
- `negative-ms` 与 `negative-lock` 仍同时存在一个 role_review 和一个 judge_adjudicate 结构化输出失败；Pipeline按 fail-open 保留可用候选。
- Judge实际任务为5次 completed、2次 failed；两个无候选样本不需要Judge调用。
- 无Judge对照中的 `negative-ms` 本身也有角色失败，单次抽样结果存在波动。
- 检查点报告只保存每题最终一次运行，不包含此前失败重试的全部历史费用。

## 实现变化

- JudgeDecision新增 `canonical_category`、`duplicate_of`、`semantic_match`、`contract_violation_verified`。
- Judge可将同位置、语义等价但类别不同的候选归一成一个主Finding。
- 被判重复的Finding进入 suppressed，证据与角色来源合并到主Finding。
- Pipeline在Judge改写category后重算fingerprint与cross-run key。
- 程序只允许相同路径且行区间重叠/相邻的Finding按`duplicate_of`合并，防止Judge错误合并不同位置。
- 扁平非nullable Judge Schema用于兼容OpenAI-compatible Provider；旧keep/downrank响应仍可解析。
