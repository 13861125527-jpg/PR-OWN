# V2-C Implementation Review Round 2

日期：2026-08-16  
范围：复验 V2-C 静态分析接入、Ruff 适配、LLM+Static 融合、评测与测试门禁。  
结论：ACCEPTED。

## 验收结论

V2-C 本轮可以通过验收。上一轮两个阻断问题均已修复，并且新增了对应回归测试/评测样本。

## 上轮 blocker 复验

### B1：Ruff 缺失或不可用时被误判为“零诊断”

状态：已关闭。

证据：

- `reposage/review/static/ruff.py:147` 增加 `_ruff_json_payload()`，统一解释 Ruff 退出码与 stdout。
- `reposage/review/static/ruff.py:154` 对空 stdout 直接判失败，不再把 `""` 解析成 `[]`。
- `reposage/review/static/ruff.py:143` 对 `No module named ruff` / `ModuleNotFoundError` 映射为 `capability miss: ruff`。
- `tests/test_static.py:176` 新增 `test_ruff_exit_1_empty_stdout_is_capability_miss`，覆盖 `exit 1 + empty stdout` 的真实风险场景。

判断：

现在 `python -m ruff` 不存在、启动失败、stdout 为空、stdout 非 JSON 这些情况不会被静默当作“无诊断”。这符合 V2-C fail-soft 设计：静态分析失败不会中断整体 review，但必须记录 capability miss / warning。

### B2：一个 LLM finding 间接融合多个不同静态 rule_id

状态：已关闭。

证据：

- `reposage/review/pipeline.py:260` 的 `_clusters_fuseable()` 在融合前先计算左右簇的静态 rule_id 并集。
- `reposage/review/pipeline.py:262` 当静态 rule_id 数量超过 1 时直接拒绝融合。
- `reposage/review/pipeline.py:396` / `reposage/review/pipeline.py:403` 保留静态 rule_id 作为 fingerprint/cross-run key 的优先 key。
- `tests/test_pipeline.py:288` 新增 `test_llm_does_not_transitively_fuse_distinct_static_rules`。
- `reposage/evals/datasets/v2c_static.yaml` 新增/覆盖 `no-fuse-two-static-via-llm` 类样本，评测通过。

复现实测输出：

```text
2
[('llm', 'ruff:S307', ['llm_general', 'static_analyzer']), ('ruff S110', 'ruff:S110', ['static_analyzer'])]
```

判断：

现在一条 LLM finding 最多只能与一个静态 rule_id 融合，不会再把 `ruff:S307` 和 `ruff:S110` 这类不同规则压成一个 model key。这个修复符合 V2-C 的 DP-8/DP-9 边界。

## 门禁结果

| 门禁 | 结果 |
|---|---|
| Focused tests：`tests/test_pipeline.py tests/test_static.py` | PASS，27 passed |
| Full pytest | PASS，439 passed |
| Ruff | PASS，All checks passed |
| MyPy strict | PASS，71 source files |
| V2-C compare | PASS，转换 3/3，融合/去重 5/5 |

V2-C compare 生成结果：

- JSON/MD 因项目 evidence 写入权限在本次 Codex 环境中被拦截，改写到 `C:\Users\Admin\Documents\Codex\2026-08-09\new-chat\v2-c-compare-r2.json` 和 `C:\Users\Admin\Documents\Codex\2026-08-09\new-chat\v2-c-compare-r2.md`。
- 业务执行结果通过，不是脚本逻辑失败。

## 非阻断说明

这些不是 V2-C 当前验收缺陷：

- V2-C compare 仍是脚本化 Fake / 数据集评测，未跑真实 API。
- GitHub API、GitHub marker 查询/更新、真实发布链路仍属于后续阶段，不纳入 V2-C 阻断。
- 项目当前工作区存在大量 V1/V2 历史改动与未跟踪文件，本次只审查 V2-C 相关改动，不评价整体 git 清洁度。

## 最终判断

V2-C Round 2：通过。

可以进入下一阶段。建议后续不要继续在 V2-C 上扩需求，只保留 bugfix；下一阶段再处理真实 GitHub 接入、发布、异步/多轮协作等能力。
