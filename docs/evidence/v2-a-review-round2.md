# V2-A 实现复验 Round 2（修复报告）

> 对照：`docs/evidence/v2-a-review-round1.md`  
> 结论：**B1–B4 与 S1 已关闭**，待复验。S2 已排除 `.tmp-pytest`。

## 1. Blocking 关闭情况

| 编号 | 处理 | 测试 |
|------|------|------|
| B1 | Phase 1 批次出现非取消异常时 **不创建** Phase 2；转换成 required failure 后结束 Strategy | `test_phase1_structural_error_does_not_start_optional`：optional LLM 调用数 = 0 |
| B2 | 多行 docstring 状态机；相邻行 `except`+`pass`；缩进判断嵌套循环；`itertools.product` | 正/负例 + 门控集 ≥20；分角色 P/R 仍过门槛 |
| B3 | `GATE_VERSION` = 规则 manifest content hash（regex/flags/路径/组合/角色映射/algo） | `test_gate_version_is_manifest_content_hash` |
| B4 | `python -m reposage.evals.v2a_compare` 同集跑 V1/V2-A，写出三表 | JSON `docs/evidence/v2-a-compare.json`；MD 由 runner 生成 |
| S1 | 占位角色 `implemented=False`，配置启用即 Registry 失败 | `test_unimplemented_role_cannot_be_enabled` |
| S2 | `pyproject.toml` ruff `extend-exclude = [".tmp-pytest"]` | — |

## 2. 门禁

| 检查 | 结果 |
|------|------|
| pytest | 388 passed |
| ruff | 通过 |
| mypy strict | 54 source files，无 issues |
| git diff --check | 无空白错误（仅 LF/CRLF 提示） |

## 3. 对照三表（脚本化 Fake，真实 API 未运行）

见 `docs/evidence/v2-a-compare.md`（由 runner 生成，勿手改数字）：

- 质量：V1 与 V2-A Finding Precision/Recall/F1/位置准确率均为 1.0，负样本噪声 0
- 成本：调用 21 vs 35；total tokens 3150 vs 5250；预算拒绝 0/0
- 延迟：全数据集均值 5.22ms vs 9.56ms（Fake 墙钟，不代表真实模型）

Gate 辅助表：security 7/0/0、correctness 4/0/0、performance 2/0/0，均为 P=R=1.0。
