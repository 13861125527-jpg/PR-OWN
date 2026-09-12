# V3-C 状态说明

> **V3-C 里程碑状态：IMPLEMENTED / ACCEPTED**
>
> 对齐稿：`docs/architecture/25-v3c-alignment.md`（按 §12 DP 实现）
> 前置：V3-B **ACCEPTED**（`docs/evidence/v3-b-implementation-review-round2.md`）
> 对照报告：`docs/evidence/v3-c-compare.md` / `.json`
> Round 1 审查：`docs/evidence/v3-c-implementation-review-round1.md`（CHANGES REQUIRED）
> Round 2 复验：`docs/evidence/v3-c-implementation-review-round2.md`（ACCEPTED）

## 本轮实现

| 卡 | 落点 |
|---|---|
| T1 | `AgentSession.evidence_index` / `checked` / `excluded` / `pending`；`evidence_refs` 为派生视图；`submit_finding` 只认索引中的成功 id |
| T2 | `reposage/review/agent/compact.py`：`maybe_compact` 程序摘要、整轮折叠、stub、native 配对、替换单条压缩槽 |
| T3 | loop 在 reserve 前与观察回灌后 compact；仍超硬顶 → `PARTIAL / context_overflow`；WAITING_TOOL 不压 |
| T4 | `AgentConfig`：`compact_threshold_ratio=0.60`、`compact_keep_rounds=2`、`max_session_chars=200000`；prompt 一句；日志 `agent.compact` 只计数字段 |
| T5 | `v3c_compare` 8/8；保留 `v3b_compare` 14/14 与 A–E / V3-A |
| T6 | pytest / Ruff / mypy |

## 对照（程序指标）

压缩对照 passed=8/8（最小 L3 保留、证据 id 可提交、compact 不计 round、native 配对、重复压缩保留旧 facts、JSON repair 指令不被折叠、硬顶 overflow、默认 agent 关闭）。无真实模型，不宣称质量收益。

## Round 1 审查修复（已复验）

| ID | 修复 |
|---|---|
| P1-1 | 重复压缩合并旧摘要 `facts`（按 `tool_call_id` 去重，保留旧条目再追加新折叠） |
| P2-1 | `json_repair_pending` 时强制保留 repair user 指令，即使 `compact_keep_rounds=0` |
| P2-2 | `v3c_compare` 增 `compact-recompact-keeps-old-facts` / `compact-json-repair-keeps-repair-instruction` |
| P2-3 | `agent.compact` 日志补 `stubbed` |

## 明确非目标

- LLM summarizer / CoT 压缩
- 跨文件 V2 vs V3 质量对照（V3-D）
- MCP、写工具、默认 `agent.enabled=true`
- 升 SQLite `user_version`（仍为 5）
- 压缩摘要落库
- OQ-11 live（仍 `DEFERRED_BY_USER`）

## 门禁

| 门禁 | 结果 |
|---|---|
| 全量 pytest | PASS（553 passed, 1 skipped） |
| Ruff | PASS |
| MyPy strict | PASS（102 source files） |
| V3-C compare | PASS（8/8） |
| V3-B compare | PASS（14/14，未改数据集） |
| OQ-11 live | DEFERRED_BY_USER |

未提交。Agent 为 **experimental**。默认 `agent.enabled=false`。`user_version` 仍为 5。V3-C 已可验收，V3-D 可开始；OQ-11 live 仍后置。
