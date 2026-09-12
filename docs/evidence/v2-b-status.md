# V2-B 状态说明

> **V2-B 里程碑状态：ACCEPTED**
>
> 对齐稿：`docs/architecture/19-v2b-alignment.md`
> 实现审查：Round 1–2 为修复轮；Round 3 `v2-b-implementation-review-round3.md` 接受核心
> 对照报告：`docs/evidence/v2-b-compare.md` / `.json`

## Round 3 结论

V2-B 核心 ACCEPTED。B1/B2/B3/S1/S2 已闭合。脚本化 Fake 对照证明 L3 进入消息且检索 TP/FN/FP=9/0/0；不宣称真实模型质量收益。

## 验收后修补（P2，非 blocker）

`ReviewService._merge_coverage()` 现在把 `skipped_coverage` 的 `truncated` 项计入 run 级 `CoverageManifest.truncated`。L3 capability-miss 路径因此同时有 warning、coverage item、以及 `truncated=true`。

`test_l3_capability_miss_is_disclosed` 断言 `run.coverage.truncated is True`。

## 明确非目标（不是遗留缺陷）

- 不实现 GitHub REST blob API
- Pipeline 不做符号 evidence 门
- 不做全仓任意同名定义
- CLI 本阶段用 FakeLLM 跑通本地 Git L3，不接真实模型账单

## 下一步

V2-C：`docs/architecture/20-v2c-alignment.md`（设计冻结，未经确认不编码）。
