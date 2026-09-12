# V3-D 状态说明

> **V3-D 里程碑状态：ACCEPTED**
>
> 验收来源：用户于 2026-08-16 授权「v0.3.0 核心阶段最终验收」；正式记录见 `docs/evidence/final-acceptance-v0.3.0.md`。
> 对齐稿：`docs/architecture/26-v3d-alignment.md`（按 §12 DP 实现）
> 前置：V3-C **ACCEPTED**（`docs/evidence/v3-c-implementation-review-round2.md`）
> 对照报告：`docs/evidence/v3-d-compare.md` / `.json`
> Round 5–6 P1（YAML notes 源数据、`validate_dataset`、证据链绑定 cited `tool_call_id`）已闭合。

## 本轮实现

| 卡 | 落点 |
|---|---|
| T1 | `reposage/evals/datasets/v3d_cross_file.yaml`：7 条跨文件样本（l3-hit / l3-miss / search / negative / grounded=`find_references` / spam / ungrounded） |
| T2 | `reposage/evals/agent_ops.py`：groundedness / 有效率 / 重复率 / 失败率纯函数；0 条 accepted → `None` |
| T3 | `reposage/evals/v3d_compare.py`：V2 默认管道 vs 进程内 Agent；质量/成本/延迟三表 |
| T4 | Agent 运行表 + native vs action_json A/B（真实解析器 + parse_error repair）；逐样本 `tool_call_id` / `needs_evidence` |
| T5 | `tests/test_evals.py::test_v3d_compare_*` + `tests/test_agent_ops.py`；保留 A–E / V3-A/B/C |
| T6 | pytest / Ruff / mypy |

## 对照口径补丁

Round 1：

- 质量主表排除 `xf-ungrounded`（无 tool id 的计量样本，不进「V3 优于 V2」叙事）。
- 报告同时给出 `groundedness`（对齐稿：tool **或** diff 行）与 `tool_groundedness`（只认 `tool_call_id`）。
- 披露：带 TOOL_RESULT 的 V3 候选未经 Pipeline 验证，置信度打 0.8 折；脚本用 1.0 才能过默认 0.75。不改 Pipeline。
- `user_version` 从对照用的 SQLite 读取，不再写死。

Round 2：

- `xf-grounded` 改为 `find_references`（对齐稿三种只读工具都有脚本样本）。
- `action_json` 臂经真实 Action JSON 解析器；submit 的 `evidence_tool_call_ids` 映射到解析器分配的 `aj-*`；`xf-l3-miss` 另含 parse_error→repair。
- 成本表补 `cost_usd`（Fake `ModelUsage` 累计）。
- 延迟 `repeats=3`，质量取第 1 次；避免 p50=p95 的单次墙钟。
- 报告 `needs_evidence`（TOOL_RESULT 未验证）。
- repeat 观察按 JSON 解析（剥 `[UNTRUSTED_CONTENT]`），不再靠子串。
- CI 钉死「不代表真实模型质量」；**不**断言 V3 F1 > V2。

Round 3：

- V2 臂改为 `Settings()`（DP-2：默认管道，不再手写一份碰巧相同的配置）。
- 质量主表排除 `xf-spam-tools`（有效率分母样本，不进「V3 优于 V2」叙事）。
- A/B 表补 hit / `action_json_tool_call_ids`；命中样本须为解析器分配的 `aj-*`；`hits_preserved`。
- 披露 V3 对照 `file_tasks=1`（Fake 脚本顺序，非产品默认 3）。
- Agent 运行表标明数据集合计 ≠ 0.7/0.3 门槛。

Round 4：

- V3 臂只覆盖 `strategy` / `agent.enabled` / `tool_protocol`，`file_tasks` 沿用默认 3（样本仅一个变更文件）。
- 质量主表 `excluded` 写入 JSON。
- A/B 增加 `groundedness_preserved`；命中样本 `tool_groundedness` 两侧均为 1.0。
- 逐样本 `ids_in_db`：cited `tool_call_id` 能在 `tool_calls` 表对上。
- 附录：`v1_demo` 默认关闭仍零 `tool_calls`。

Round 5（P1）：

- YAML notes 增加 `anchor` / `evidence_path` / `quality`；删除 `_ANCHORS` / `_QUALITY_EXCLUDE` 主数据依赖。
- `validate_dataset`：l3-miss 的 anchor 出现在变更文件 → `dataset_invalid`，CLI 非 0。
- 逐样本 `v3_evidence_path_ok` / `v3_anchor_observed`。

Round 6（P1）：

- `validate_dataset` 检查 `pr_title` / `pr_description` / `expected.note` 泄露。
- 证据链只认 accepted Finding 引用的 `tool_call_id`，不再扫整个 session。

## 对照（程序指标）

脚本化 Fake。V2 仅当 L3 prompt 含锚点才吐 Finding。`l3-miss`：V2 miss、V3 工具命中且 `evidence.tool_call_id` 非空。高效脚本有效率 ≥ 0.7；spam < 0.7。`recommendation=keep_agent_experimental`。`real_api=false`。不宣称真实模型质量收益。

## 明确非目标

- 默认 `agent.enabled=true` / 改默认 `review.strategy`
- 用 Fake 差值宣传线上质量
- OQ-11 live（仍 `DEFERRED_BY_USER`）；不接 `real_compare`
- MCP、写工具、升 SQLite `user_version`（仍为 5）
- 改 V2 语义 / fingerprint / `execute` 签名 / Agent 状态机

## 门禁

| 门禁 | 结果 |
|---|---|
| 全量 pytest | PASS（561 passed, 1 skipped） |
| Ruff | PASS |
| MyPy strict | PASS（104 source files） |
| V3-D compare | PASS（notes 源数据 + validate_dataset + 证据链；dataset_status=ok） |
| V3-C compare | PASS（8/8） |
| V3-B compare | PASS（14/14） |
| OQ-11 live | DEFERRED_BY_USER |

未提交。Agent 为 **experimental**。默认 `agent.enabled=false`。`user_version` 仍为 5。OQ-11 live 仍后置。V3-D 随 v0.3.0 核心阶段最终验收一并 **ACCEPTED**；Fake 对照证明实验可计量，不代表真实模型线上收益。
