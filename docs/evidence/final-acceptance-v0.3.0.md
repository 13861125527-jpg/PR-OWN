# v0.3.0 核心阶段最终验收报告

> 验收结论：**ACCEPTED**  
> 验收日期：2026-08-16  
> 验收来源：用户授权「基于 Phase 0、V1、V2、V3-A/B/C/D 的 v0.3.0 核心阶段最终验收」  
> 标签对应：`docs/architecture/12-roadmap-adrs.md` — V3 完成即 **v0.3.0**  
> 本报告验收的是**核心阶段能力**（可运行、可测试、可对照、默认可安全关闭），不是 v1.0.0 产品化交付。

## 1. 独立门禁结果

本轮验收当日在仓库根目录执行，未新增 GitHub Action、未调用真实 GitHub API、未默认开启 Agent。

| 检查项 | 结果 |
|---|---|
| 全量 pytest | `561 passed, 1 skipped` |
| Ruff | `All checks passed` |
| mypy strict | `Success: no issues found in 104 source files` |

V3-D 对照产物：`docs/evidence/v3-d-compare.md` / `.json`（`dataset_status=ok`，`real_api=false`，`recommendation=keep_agent_experimental`）。

## 2. 里程碑状态

| 里程碑 | 状态 | 证据 |
|---|---|---|
| Phase 0 | 已完成（工程骨架进入后续里程碑） | pyproject / Ruff / mypy / pytest、domain、Provider、Fake、SQLite、评测 runner |
| V1-A–F | 核心链路已落地并被后续版本复用 | `docs/evidence/v1-*-status.md`；V1-C smoke `v1-c-dp-v4-pro-smoke.md`；V1-D 真实对照 `v1-d-real-compare.md`（历史门槛，非本轮重跑） |
| V2-A | **ACCEPTED** | `docs/evidence/v2-a-final-acceptance.md` |
| V2-B | **ACCEPTED** | `docs/evidence/v2-b-status.md` |
| V2-C | **ACCEPTED** | `docs/evidence/v2-c-status.md` |
| V2-D | **ACCEPTED** | `docs/evidence/v2-d-status.md` |
| V2-E | **ACCEPTED** | `docs/evidence/v2-e-status.md` |
| V3-A | **ACCEPTED** | `docs/evidence/v3-a-implementation-review-round2.md` |
| V3-B | **ACCEPTED** | `docs/evidence/v3-b-implementation-review-round2.md` |
| V3-C | **ACCEPTED** | `docs/evidence/v3-c-implementation-review-round2.md` |
| V3-D | **ACCEPTED**（本轮） | `docs/evidence/v3-d-status.md`；对照 `v3-d-compare.md` |

## 3. 已完成能力（v0.3.0 核心阶段）

以下为当前代码默认即可依赖、或显式开关后可跑通的能力。默认审查路径仍是 V1 `single_pass`；V2/V3 增强均需显式配置，不改变默认行为。

### 3.1 Phase 0 — 工程骨架

- Python 包、配置加载、Ruff / mypy / pytest 门禁。
- 领域模型（含 occurrence / fingerprint / cluster 三身份）。
- Provider 抽象与 Fake Git/LLM；SQLite 持久化基础。
- 评测 runner 骨架（后续各版对照均复用，不复制主流程）。

### 3.2 V1 — 固定审查管道（默认策略）

- Diff 解析、行号映射、文件过滤（本地 fixture 先行）。
- L0–L2 上下文与 token 预算；per-file map-reduce。
- OpenAI-compatible LLM + 结构化输出与修复重试（OQ-1 可用性已实测；tool calling 未做 live）。
- `SinglePassReviewer` + 统一 `FindingPipeline`（claimed→canonical、fingerprint 去重）。
- Publishing：默认 **dry-run**；Saga/Outbox（prepared→published/partial；marker + `remote_comment_id`；租约 fencing）。
- 可观测性：覆盖清单、成本、Token、耗时、`run_stages`、结构化 JSONL、配置快照哈希；secret 脱敏。

默认：`review.strategy=single_pass`，`publishing.dry_run=true`。

### 3.3 V2 — 条件化增强（显式开启）

- **V2-A**：角色 registry、确定性门控、两阶段预算、状态归并；需 `strategy=multi_role`。
- **V2-B**：符号级 L3（当前为**正则过渡实现**，非 Tree-sitter）；Pipeline 不做符号 evidence 门。
- **V2-C**：ruff 子集静态分析、转换/融合；默认 `static.enabled=false`。
- **V2-D**：聚类先于 fingerprint、Judge keep/downrank、`needs_evidence`；默认 `judge.enabled=false`；SQLite `user_version=5`。
- **V2-E**：反馈记忆（CLI `feedback mark/list/revoke`）、watermark；默认 `feedback.enabled=true`、`incremental.enabled=false`。

V2 各对照报告均为脚本化 Fake（或程序指标），**不**作为真实模型质量宣传。

### 3.4 V3 — 只读 Agent（experimental，默认关闭）

- **V3-A**：工具 registry + 路径沙箱；只读 `read_file` / `find_files` / `search_code` / `find_references` / `read_diff`；越界拒绝；审计落库。
- **V3-B**：Agent loop（native + action_json）、预算/grace/取消/重复检测、WAITING_TOOL 出口；`submit_finding` / `finish_review` 由 loop 拦截后交 Pipeline。
- **V3-C**：程序化会话压缩 + 证据索引；压缩后最小 L3 与 cited 工具 id 可追溯。
- **V3-D**：同一批跨文件合成样本上 V2 默认管道 vs 显式 Agent 的质量/成本/延迟三表 + Agent ops + native vs action_json；结论槽固定为 `keep_agent_experimental`。

默认：`agent.enabled=false`。Agent 为 **experimental**。未升 `user_version`（仍为 5）。未改 V2 语义、fingerprint、`execute` 签名。

## 4. 评测口径（禁止误读）

| 对照 | 性质 | 允许的结论 | 不允许的结论 |
|---|---|---|---|
| V1-D `v1-d-real-compare` | 历史真实 API 门槛（本轮未重跑） | 当时结构化链路可跑 | 不得外推为 v0.3.0 线上质量 |
| V2-A–E compare | 脚本化 Fake / 程序指标 | 管道、计量、门控可重复 | 不得宣传为真实模型收益 |
| V3-A/B/C compare | 脚本化 Fake 轨迹 | 沙箱/loop/压缩行为符合契约 | 不得宣传为真实模型收益 |
| V3-D compare | 脚本化 Fake；`real_api=false` | 跨文件实验**可计量**；推荐保持 experimental | 不得把 Precision/Recall/F1 差值写成线上质量或产品默认切换依据 |

V3-D 质量主表排除 spam / ungrounded 计量样本；TOOL_RESULT 未经 Pipeline 重验（置信度 0.8 折）已披露。CI **不**断言「V3 F1 全局优于 V2」为产品事实。

## 5. v1.0.0 后置能力（本轮明确不做）

对应 `12` §1–§2 的 **V1.0** 与 `11` §8 v1.0.0 门槛。下列项**不是** v0.3.0 缺口，而是下一标签的工作：

| 后置项 | 说明 |
|---|---|
| 产品 GitHub Action | 不新增 Action 作为审查入口；现有 `.github/workflows/ci.yml` 仅为 lint/test，不是产品交付 |
| 真实 GitHub API / GitHub App | 发布路径保持 Fake + dry-run；不接线上 PR 评论 |
| 线上冒烟 10 次无失败发布 | `11` §8 v1.0.0；需专用演示仓库与显式开启 |
| OQ-11 live / `real_compare` | tool calling 真实模型对照仍 `DEFERRED_BY_USER`；ADR-005 待实测 |
| 将 Agent 升为默认 | 保持 `agent.enabled=false`、`strategy=single_pass` |
| MCP、写工具、ruff-as-tool | V3 明确非目标 |
| Tree-sitter 符号索引 | V2-B 正则 L3 为过渡；`12` §4 已允许后置 |
| SQLite schema bump | `user_version` 保持 5 |
| v1.0.0 文档 / 完整评测 / 安全加固报告 | 产品化配套，非本核心阶段 |
| 评论指令反馈（`/reposage-wontfix`） | OQ-4 待定；当前仅 CLI |
| 向量库、LangChain/LangGraph | ADR-002 / ADR-001 已排除为首期选择 |

## 6. 冻结项（验收后不得悄然改动）

- 默认 `review.strategy=single_pass`。
- 默认 `agent.enabled=false`；Agent 保持 experimental。
- 默认 `publishing.dry_run=true`。
- SQLite `user_version=5`。
- 不把 Fake 对照写成真实模型线上收益。
- 不在本阶段新增产品 GitHub Action、不接真实 GitHub API。
- OQ-11 live 仍后置。

## 7. 最终决定

Phase 0 骨架、V1 默认审查管道、V2 条件化增强、V3 只读 Agent（含跨文件 Fake 对照）已达到 `12` 对 **v0.3.0** 的核心阶段定义：每一版可运行、可测试、可对照，且默认关闭高风险能力。

**v0.3.0 核心阶段 — ACCEPTED**

V3-D 同步标记为 **ACCEPTED**（见 `docs/evidence/v3-d-status.md`）。进入 v1.0.0 之前，不需要对本核心阶段做功能返工；下一步是产品化（Action、真实发布冒烟、文档与安全报告），而不是继续扩展 Agent 默认行为。
