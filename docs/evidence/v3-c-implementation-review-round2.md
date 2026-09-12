# V3-C 实现复验（Round 2）

> 审查日期：2026-08-16  
> 审查结论：**ACCEPTED / V3-C 可验收**  
> 基线：`docs/architecture/25-v3c-alignment.md` + `docs/evidence/v3-c-implementation-review-round1.md`

## 1. 结论

Round 1 的阻塞项已经闭合。V3-C 现在满足“程序化会话压缩 + 证据索引”的核心契约：

- 重复压缩会合并旧摘要 facts，并按 `tool_call_id` 去重。
- `json_repair_pending` 时，repair user 指令会被强制保留，即使 `compact_keep_rounds=0`。
- `v3c_compare` 已从 6 条扩到 8 条，覆盖重复压缩和 JSON repair compact 边界。
- `agent.compact` 日志补充了 `stubbed`。
- 默认 Agent 仍关闭，`user_version` 仍为 5，OQ-11 live 继续后置。

本轮可以标记为 **ACCEPTED**。

## 2. 验证结果

| 检查 | 结果 |
|---|---|
| 全量 `pytest -q` | PASS（1 skipped） |
| `python -m reposage.evals.v3c_compare` | PASS（8/8） |
| `python -m reposage.evals.v3b_compare` | PASS（14/14） |
| `ruff check .` | PASS |
| `mypy reposage` | PASS（102 source files） |
| OQ-11 live | DEFERRED_BY_USER |

说明：compare / Ruff / Mypy 在普通沙箱下仍会因写 `docs/evidence` 或缓存失败；提权后同一命令通过。失败原因是文件写权限，不是代码或断言失败。

## 3. Round 1 问题闭环

### P1-1：重复压缩保留旧 facts/tool_call_id

状态：**已修复**

关键实现：

- `reposage/review/agent/compact.py` 保留旧 `is_compressed` 消息。
- 新增 `_facts_from_compressed()` 解析最新旧摘要 facts。
- 新增 `_merge_facts()`，先保留旧 facts，再追加新折叠 facts，并按 `tool_call_id` 去重。
- 新摘要仍保持单槽替换，最终只保留一条压缩消息。

测试证据：

- `tests/test_agent_compact.py` `test_recompact_keeps_old_facts`
- `reposage/evals/v3c_compare.py` `compact-recompact-keeps-old-facts`

复验判断：第二次 compact 后，最终压缩摘要同时保留旧 `c0` 和新折叠的 `c2`，不再丢旧证据 id。

### P2-1：JSON repair pending 不被 compact 折叠

状态：**已修复**

关键实现：

- `reposage/review/agent/compact.py` 增加 `JSON_REPAIR_HINT`。
- `_peel_repair_user()` 在 `session.json_repair_pending=True` 时从可折叠 body 中剥离 repair user。
- 重建消息时把 repair user 放回末尾，保证下一次 provider 请求能看到修复指令。

测试证据：

- `tests/test_agent_compact.py` `test_compact_keeps_json_repair_instruction`
- `tests/test_agent_compact.py` `test_loop_json_repair_survives_compact`
- `reposage/evals/v3c_compare.py` `compact-json-repair-keeps-repair-instruction`

复验判断：`compact_keep_rounds=0` 下仍能保留 repair 指令；action_json 修复轮正常完成。

### P2-2 / P2-3：compare 与日志补齐

状态：**已修复**

- `reposage/evals/datasets/v3c_compact.yaml` 增加 2 个 case。
- `docs/evidence/v3-c-compare.md` 显示 `passed=8/8`。
- `reposage/review/agent/loop.py` 的 `agent.compact` 日志包含 `stubbed`。

## 4. 残余风险

- V3-C 仍是脚本化 Fake 验证，不跑真实 API；这符合用户已明确后置 OQ-11 的范围。
- 压缩摘要 facts cap 为 200，集合 cap 为 50；这是合理上限，但未来 V3-D 做质量评测时应观察是否影响长链探索。
- 真实模型是否善用压缩摘要属于 V3-D/V3-E 的质量问题，不属于本切片验收门槛。

## 5. 验收建议

V3-C 可以标记为 **ACCEPTED**。下一阶段可进入 V3-D，但不应在 V3-D 前默认打开 Agent 或恢复真实 API 依赖。
