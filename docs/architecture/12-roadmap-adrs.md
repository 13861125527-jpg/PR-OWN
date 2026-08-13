# 12 — 开发路线与 ADR

> 里程碑、任务依赖、Definition of Done、风险与降级、ADR 清单、Git 规划。
> 标注：`V1 now` / `V2 later` / `V3 later` / `optional`。

---

## 1. 总体路线

```text
Phase 0   工程骨架、领域模型、Provider 抽象、Fake、基础评测集
V1        diff → 单审查 → Finding → dry-run/发布（标签 v0.1.0）
V2        条件多角色 → 上下文增强 → 静态分析 → 融合/反馈（标签 v0.2.0）
V3        只读工具 → Agent loop → 证据验证 → 会话压缩（标签 v0.3.0）
V1.0      GitHub Action、完整评测、安全加固、文档与演示（标签 v1.0.0）
```

## 2. 里程碑任务与依赖

| 里程碑 | 任务 | 依赖 | DoD 摘要（详细门槛见 `11` §8） |
|--------|------|------|-------------------------------|
| Phase 0 | 工程骨架（pyproject/Ruff/mypy/pytest）、目录、domain 模型（含 occurrence/fingerprint/cluster 三身份）、Provider 协议、Fake Provider、配置加载、SQLite 基础（findings/publish_plans 表）、评测 runner 骨架 | – | 单元测试跑通；domain 无 IO 依赖（静态检查） |
| V1-a | diff 解析器 + 行号映射 + 文件过滤（**先本地 fixture 样本，再接远程**） | Phase 0 | parser 单测全绿；重命名/删除样本通过 |
| V1-b | 上下文构建（L0–L2 + 内置规则）+ 预算（含 per-file map-reduce 契约） | V1-a | 分块 token-aware；截断标记正确 |
| V1-c | LLMProvider(openai_compat) + structured + 修复重试 | Phase 0 | DP-V4-PRO 最小实测通过（`14` OQ-1） |
| V1-d | SinglePassReviewer（per-file map-reduce）+ 统一 FindingPipeline（claimed→canonical 重定位、fingerprint 去重） | V1-b, V1-c | 评测集基础指标达标 |
| V1-e | Publishing（dry-run/正式）+ **Saga/Outbox**（prepared→published/partial；marker + remote_comment_id）+ 摘要 | V1-d | 幂等验证通过；部分失败重跑恢复通过 |
| V1-f | 运行记录/日志/成本/覆盖（CoverageItem） | V1-d | 全指标落库 |
| V2-a | 角色 registry + 门控 + 并发 barrier（只产出候选） | V1 | 门控命中率测试 |
| V2-b | 符号级检索（Tree-sitter）→ L3 | V1 | 符号缓存失效正确 |
| V2-c | 静态分析器（ruff 子集）融合 | V2-a | 转换/去重正确 |
| V2-d | 聚类/去重/Judge（仅 keep/downrank；needs_evidence 内部标记） | V2-a | 重复率指标 |
| V2-e | 反馈记忆 + watermark（Saga 发布完成） | V2-a | 撤销/重放测试 |
| V3-a | 工具 registry + 沙箱 + schema | V2 | 越界路径全拒 |
| V3-b | Agent loop + 预算（reserved finalize）/取消/重复检测 | V3-a | 状态机全覆盖（含 WAITING_TOOL 异常出口） |
| V3-c | 会话压缩 + 证据索引（保留确定性最小 L3） | V3-b | 压缩后可追溯 |
| V3-d | 跨文件评测对照（tool calling 方案 A/B 同集对比） | V3-b | 收益报告；方案以评测为准 |
| v1.0.0 | Action 入口、冒烟、文档、安全报告 | V3 | 冒烟 10 次通过 |

## 3. 任务拆解建议（按文档模块分配）

- **P0 领域**：`05` 的模型字段表 → 直接转 Pydantic 类与枚举（任务卡）。
- **P1 解析**：`04` 的 diff 流程 → parser 任务卡（含测试样本）。
- **P2 策略**：`03` 的 ReviewStrategy 接口 → 三实现任务卡。
- **P3 流水线**：`07` 的门控/去重/Judge → 独立任务卡。
- **P4 Agent**：`08` 的工具与 loop → V3 任务卡。
- 每张任务卡引用本文档对应章节，保证可追溯。

## 4. 风险与降级

| 风险 | 概率/影响 | 缓解/降级 |
|------|-----------|-----------|
| DP-V4-PRO JSON 不稳定 | 高/高 | schema_first → json_repair → fail-soft；评测监控字段漂移 |
| DP-V4-PRO 无 tool calling | 中/高 | 方案 B 受限协议（`08` §9） |
| 误报率超标 | 中/高 | 置信度门控调高、反馈记忆、评测迭代 |
| V3 收益不显著 | 中/中 | 先做跨文件样本小实验再投入；不显著则 V3 降级为"增强 V2 + 工具验证" |
| 成本失控 | 中/中 | 硬预算 + 门控 + 角色子集先上 |
| Tree-sitter 集成复杂度 | 中/低 | 用正则符号表先过渡（性能容忍），Tree-sitter 后置 |
| 发布事故（重复/越界） | 低/高 | marker + remote_comment_id + 行号校验 + dry-run 默认（`10` §7） |

## 5. Git 与协作规划

- **短分支 + PR**：`feat/v1-diff-parser` → PR → 自举：**RepoSage 审查 RepoSage 的 PR**（作为 dogfood 与演示）。
- 分支规范：`feat/*`、`fix/*`、`docs/*`；PR 必须附 dry-run 审查记录。
- 标签：`v0.1.0`（V1）、`v0.2.0`（V2）、`v0.3.0`（V3）、`v1.0.0`。
- Commit 规范：Conventional Commits（feat/fix/test/docs/refactor）。
- 目录约定：设计文档在 `docs/architecture/` 冻结为 v1 后，改动走 PR + ADR 流程。

## 6. ADR 清单

| ADR | 主题 | 状态 |
|-----|------|------|
| ADR-001 | 首期不用 LangChain/LangGraph（自研 loop） | 已定（`03` §7） |
| ADR-002 | SQLite 优先，不默认向量库（含启用信号） | 已定（`06` §6） |
| ADR-003 | 事实字段程序验证，模型只提交候选 | 已定（`05` §4） |
| ADR-004 | 角色=调用，Agent=执行单元，不混称 | 已定（`07` §2） |
| ADR-005 | tool calling 双方案（换模型 / 受限协议） | 待实测（`14` OQ-1） |
| ADR-006 | 默认 dry-run、request_changes 关闭 | 已定，待用户确认（`14` OQ-3） |
| ADR-007 | ruff 作为首发静态分析器 | 待评测（`14` OQ-5） |
| ADR-008 | 反馈记忆入口（CLI / 评论指令） | 待定（`14` OQ-4） |
| ADR-009 | head 漂移处理（拒绝 vs 自动重跑） | 待定（`14` OQ-6） |

新增 ADR 追加编号；每份 ADR 一句话记录决策背景、替代方案与代价。

## 7. 版本间不重写承诺（验收自查）

- 入口/领域/Provider/发布层代码是否跨版本复用（禁止复制粘贴重写）。
- 每次升级附带质量/成本/延迟三对照表（`11` §7）。
- 既有评测样本在新版本上回归（不允许"旧样本消失"）。
