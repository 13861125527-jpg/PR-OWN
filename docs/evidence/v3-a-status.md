# V3-A 状态说明

> **V3-A 里程碑状态：ACCEPTED**
>
> 实现验收：`docs/evidence/v3-a-implementation-review-round2.md`
>
> 对齐稿：`docs/architecture/23-v3a-alignment.md`
> 对照报告：`docs/evidence/v3-a-compare.md` / `.json`

## 本轮实现

| 卡 | 落点 |
|---|---|
| T1 | `reposage/tools/sandbox.py` + `ToolSnapshot` / `GitToolSnapshot` / `MemoryToolSnapshot`；`ContextSourceKind.TOOL`；`ToolCall.task_id` |
| T2 | `ToolRegistry` + Pydantic args（extra forbid）+ `invoke` 信封（超时/截断/未知/取消重抛） |
| T3 | `read_file` / `find_files` / 字面量 `search_code`（扫描硬上限） |
| T4 | `find_references`（复用 V2-B 抽取）+ `read_diff`（已解析 diff） |
| T5 | `submit_finding` / `finish_review` stub=`not_bound`；`strategy=agentic` 构造失败 |
| T6 | `record_tool_invocation` 同事务、同值幂等、冲突拒绝、脱敏；`user_version` 仍为 5 |
| T7 | `v3a_sandbox.yaml` + `v3a_compare`；A–E 钉死 `agent.enabled=false` |
| T8 | pytest / Ruff / mypy strict 全绿 |
| P1 | find_references 单文件/总量字符硬上限；审计 args/data 限长且 JSON 可解析 |

## Round 1 后补丁

- P1-1：`find_references` 在 AST/逐行扫描前裁剪，`MAX_CHARS_PER_FILE=16k`、`MAX_TOTAL_CHARS_SCANNED=200k`，超限 `truncated=true`。
- P1-2：`canonicalize_args_json` / `canonicalize_data_json` 在 Storage 边界强制 8k/32k；超限写带 sha256 的结构化摘要，幂等用规范化后的值。
- P2-1：`ToolDefinition.max_result_chars >= 512`；信封仍装不下则 `result_too_large`。
- P2-2：取消在开启 trace 时落库 `cancelled`；审计失败仍传播 `CancelledError`。

## 对照（程序指标）

沙箱对照 passed=10/10（越界拒绝、合法读快照、行截断、字面量搜索、regex 不当正则、两件 stub、未知工具、默认 review 不写 tool_calls）。无真实模型，不宣称质量收益。

## 明确非目标

- Agent loop / `tool_loop` / AgenticReviewer / 会话压缩 / MCP
- ruff 作为模型可调工具
- 改 V2-C/D/E 或 `cross_run_match_key`
- 默认打开 `agent.enabled`

## 门禁

pytest / Ruff / mypy strict 全绿。未提交。
