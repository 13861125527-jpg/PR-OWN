# V2-A 实现复验 Round 4（修复报告）

> 对照：`docs/evidence/v2-a-review-round3.md`  
> 结论：**B2、B3 与 should-fix 预算计数已关闭**，待最终验收。B1、B4、S1 保持关闭。

## 1. Blocking 关闭情况

| 编号 | 处理 | 测试 |
|------|------|------|
| B2 | 每个 hunk 用新文件侧视图（context + added，忽略 deleted）维护 docstring / 循环缩进栈 / except 结构；仅 added 行贡献 keyword/import；状态不跨 hunk | `test_docstring_opener_in_context_does_not_enable_security`（复现 A：不启用 security）；`test_nested_loop_outer_in_context_enables_performance`（复现 B：`loop_nested`）；门控集新增 `g-docstring-context`、`g-nested-context` |
| B3 | `gate_manifest()["scan"]` 收录全部扫描正则的 pattern+flags；runtime 与 manifest 共用 `_SCAN_PATTERNS` / `_KEYWORD_PATTERNS` | `test_gate_version_is_manifest_content_hash`：变异 `scan` 中 loop regex 后 `GATE_VERSION` 变化 |
| 预算计数 | `_budget_rejects()` 只统计预算失败的 task，不再叠加 `TRUNCATED` coverage | `test_budget_rejects_counts_once_when_task_and_truncated`：1 个 failed task + 1 条 truncated → 计 1 |
| 对照说明 | 生成 Markdown 补一句：质量同为 1.0 是脚本化 Fake 构造，不代表多角色模型效果 | `test_v2a_compare_emits_three_tables` 断言该句存在 |

未重开：B1（Phase 1 结构异常不启动 Phase 2）、B4（三表 runner）、S1（占位角色不可启用）。

## 2. 门禁

| 检查 | 结果 |
|------|------|
| pytest | 391 passed |
| ruff | `All checks passed`（`--no-cache`） |
| mypy strict | `Success: no issues found in 54 source files`（缓存放到可写目录） |
| git diff --check | 无空白错误（仅 LF/CRLF 提示） |

## 3. 对照三表（脚本化 Fake，真实 API 未运行）

见 `docs/evidence/v2-a-compare.md`（由 runner 重新生成，勿手改数字）：

- 质量：V1 与 V2-A Finding Precision/Recall/F1/位置准确率均为 1.0，负样本噪声 0；报告已标明这是脚本化候选构造，不代表模型效果相同
- 成本：调用 21 vs 35；total tokens 3150 vs 5250；预算拒绝 0/0
- 延迟：全数据集 Fake 墙钟（随机器波动，不代表真实模型）

Gate 辅助表：security 7/0/0、correctness 4/0/0、performance 3/0/0（新增 context 嵌套循环正例），均为 P=R=1.0。
