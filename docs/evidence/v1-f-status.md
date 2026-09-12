# V1-f 状态说明（第二轮返工：可观测性闭环，2026-08-15）

> **V1-f 里程碑状态：阶段记录/结构化日志/配置快照哈希已闭环，待最终复验**
>
> V1 最后一个里程碑。依赖 V1-d（审查主链路）与 V1-e（发布/Saga）。DoD：**全指标落库**
> （`12` §2），对应 `11` §8 V1 门槛「覆盖清单、成本、Token、耗时落库」+ `11` §2 结构化日志。
> 第一轮完成 tasks/usages/coverage 落库；本轮按第一轮验收报告补齐三个可观测性缺口：
> StageResult 落库、结构化 JSONL 日志、config_snapshot_hash 生成。

## 1. 本轮补齐内容（对应第一轮验收报告 §A/B/C/D）

| 项 | 状态 |
|---|---|
| A. run_stages 表 | 新增 `run_stages` 表（run_id 外键 + sequence 顺序 + stage/status/required/duration_ms/tokens/cost_usd/error/detail）；`record_run` 幂等落库（DELETE+INSERT，同 run 重写不膨胀，同 stage 可多次，顺序可还原）；迁移 v2→v3 |
| B. 结构化 JSONL 日志 | `observability/logging.py`：`StructuredLogger`（固定字段 ts/level/run_id/stage/task_id/tool_call_id/finding_occurrence_id/fingerprint/event/detail）+ `redact_secrets`（key/token/secret/authorization/PEM 脱敏）；失败隔离（写失败不抛）；记录 run_start/阶段完成/阶段失败/run_completed/run_failed 事件 |
| C. 配置快照哈希 | `Settings.snapshot_payload()` 剥离 secret 值（只存 `xxx_env` 名），`snapshot_hash()` 基于脱敏 payload；`ReviewService` 在 run 开始时写入 `run.config_snapshot_hash` |
| D. 失败路径验收 | fetch/review 失败后失败阶段 status/error 仍落库；同 run 重写不膨胀；同 stage 多次顺序可还原；JSONL 可解析且脱敏；snapshot 不含密钥 |

## 2. 验证结果

| 门禁 | 结果 |
|---|---|
| Pytest | **342 passed** |
| Ruff | All checks passed |
| mypy strict | Success: 46 source files |

新增/强化测试：
- `test_logging.py`：JSONL 字段完整、detail 脱敏（sk-/ghp_）、写失败不抛、常见 secret 形态脱敏；
- `test_config.py`：snapshot 相同配置哈希稳定、行为配置变化哈希变化、快照不含 secret；
- `test_storage.py`：run_stages 落库（含 tokens/cost/error/detail）、幂等不膨胀、同 stage 顺序还原；
- `test_service.py`：端到端 config_hash 落库（64 hex）、失败 run 的失败阶段落库可查询。

## 3. 口径说明

- 耗时落库：run 墙钟（runs.started_at/finished_at）+ 模型延迟（usages.latency_ms）+ 各阶段 duration_ms（run_stages 表，本轮补齐）。
- 结构化日志默认写 stderr（本地最小实现），不接入 OpenTelemetry/Prometheus；不记录 prompt/源码/API 响应正文/raw CoT。

## 4. V1 完成情况

| 里程碑 | 状态 |
|---|---|
| Phase 0 | ✅ 工程骨架/领域模型/Provider 抽象/Fake/SQLite 基础 |
| V1-a | ✅ diff 解析 + 行号映射 + 文件过滤 |
| V1-b | ✅ 上下文构建（L0–L2 + 内置规则）+ 预算 |
| V1-c | ✅ openai_compat + structured + 修复重试 |
| V1-d | ✅ SinglePassReviewer + 统一 FindingPipeline |
| V1-e | ✅ Publishing（dry-run/正式）+ Saga/Outbox |
| **V1-f** | ✅ **运行记录/日志/成本/覆盖（CoverageItem）全指标落库** |

## 5. 后续（v1.0.0 前，非 V1 范围）

1. Typer CLI 的 `--publish` / `--dry-run`；
2. 真实 GitHub Provider（`github_api`）实现 `publish_comments` / `delete_comment`；
3. GitHub Action 接入、完整评测、安全加固（`v1.0.0`）。
