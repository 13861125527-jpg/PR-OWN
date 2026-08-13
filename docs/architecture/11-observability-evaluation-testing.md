# 11 — 可观测性、评测与测试

> 结构化日志、trace、指标；评测集构成与指标；测试层级；三版验收门槛与对照实验。
> 标注：`V1 now` / `V2 later` / `V3 later` / `optional`。

---

## 1. ID 体系与关联

| ID | 生成 | 贯穿 |
|----|------|------|
| `run_id` | UUID（每次审查） | 所有记录 |
| `task_id` | run 内序号 | 文件/角色/Agent 任务 |
| `tool_call_id` | 全局唯一 | V3 工具调用 |
| `finding_occurrence_id` | UUID | 本次 run 记录主键 |
| `fingerprint` | 内容哈希 | 稳定去重/跨 run 匹配/反馈/幂等关联 |

所有日志/trace/指标均带以上 ID 便于跨阶段关联。

## 2. 结构化日志与脱敏

- JSON Lines 格式；字段：`ts, level, run_id, stage, task_id, tool_call_id, finding_occurrence_id, fingerprint, event, detail`。
- 脱敏：`token/key/secret` 相关字段一律 `***`（`10` §5）。
- 禁止写入：raw CoT、模型隐含推理（只写动作/工具/结构化理由/证据）。
- 开发者调试轨迹（detail 级）与用户可见摘要（summary 级）分离：摘要只含 PR 概述、风险等级、评论计划、覆盖披露。

## 3. 跨阶段 Trace

```text
preflight → fetch → context → review(strategy) → pipeline → publish
```

每阶段写 `StageResult`（`05` §2）：status、耗时、tokens、错误。V3 增加 `agent` 子阶段（每轮/每工具调用事件）。

## 4. 指标（从 V1 开始采集）

| 类别 | 指标 | 版本 |
|------|------|------|
| 运行 | 成功/部分成功/失败次数、各阶段耗时 | V1 |
| 成本 | Token（入/出）、费用、延迟 | V1 |
| 覆盖 | 文件/角色/任务覆盖数、跳过数、截断标记 | V1/V2 |
| 质量 | Precision/Recall/F1、位置准确、严重度准确、重复率、Actionability、无缺陷 PR 噪声 | V1 起（评测集） |
| 反馈 | 误报反馈率、反馈命中率 | V2 |
| Agent（V3） | 工具调用效率（有效/总）、重复调用率、工具失败率、证据 groundedness、停止质量、预算遵守率、上下文获取成功率 | V3 |

## 5. 评测集设计

### 构成（V1 起，逐步扩充）

| 类别 | 说明 | 来源 | 版本 |
|------|------|------|------|
| 人工单缺陷 PR | 每个 PR 植入单个明确缺陷 | 人工构造 | V1 |
| 真实修复反向样本 | 公开 commit 反转为含缺陷样本 | 开源修复记录 | V1 |
| 无缺陷负样本 | 无问题 PR，测噪声 | 人工/真实 | V1 |
| 跨文件样本 | 缺陷需要跨文件上下文 | 人工构造 | V2 准备，V3 主用 |
| 提示注入样本 | 恶意描述/注释 | 构造 | V1 起安全组 |
| 稳健性样本 | 超大 diff、重命名、删除、二进制、生成文件 | 构造/真实 | V2 |

### 质量指标定义（口径）

- Precision = 有效 Finding / 全部输出 Finding（有效 = 标注命中且位置正确）。
- Recall = 命中缺陷 / 标注缺陷总数。
- F1 = 调和均值。
- 位置准确率 = 位置完全正确的 Finding 占比。
- 严重度准确率 = severity 与标注一致的占比。
- 重复率 = 聚类后保留/原始候选（越低越好）。
- Actionability = 有明确修复方向且可执行的占比。
- 无缺陷噪声 = 负样本上输出的 Finding 数（越低越好）。

## 6. 测试层级

| 层级 | 覆盖 | 版本 |
|------|------|------|
| 单元 | diff parser、行映射、文件过滤、预算、门控、Finding pipeline（claimed→canonical 重定位、fingerprint 去重）、状态机、路径沙箱、稳定指纹 | V1 |
| 组件 | Fake Git/LLM Provider、工具错误协议、Prompt 快照、Schema 修复重试、角色门控+并发、Saga 发布状态机 | V1/V2 |
| 集成 | 临时 Git 仓库 dry-run（先 fixture 后远程）、GitHub API 回放（录制的 fixture）、多角色部分失败、Agent 固定轨迹回放、发布中断恢复 | V2/V3 |
| 安全/故障注入 | 注入样本、越界路径、幻觉行号、发布中断、限流、超时、取消、重复 delivery、secret 脱敏、分支配置提权 | V1 起（安全组） |
| 线上冒烟 | 专用演示仓库、显式开启后发布（v1.0.0） | v1.0.0 |

- Fake Provider 是关键：`FakeGitProvider`（内存仓库+diff）、`FakeLLMProvider`（录制回放/脚本化响应）让测试确定、快速、无网络。
- Prompt 快照测试：任何 prompt/schema/规则变更触发快照 diff 与评测回归。

## 7. 版本对照实验方法

| 对照 | 实验 | 门槛 |
|------|------|------|
| V1 vs baseline(直拼 prompt) | 同一评测集 | V1 Precision/位置准确显著更优 |
| V2 vs V1 | 同集 + 角色子集 | 安全/静默失败 Recall 提升；成本/延迟对照报告 |
| V3 vs V2 | 跨文件样本 | Recall/groundedness 提升有报告背书；工具有效率 ≥ 0.7、重复调用率 < 0.3 |

原则：任何版本升级必须附"质量、成本、延迟"三张对照表，缺一不可（这是"不推倒重写"的验收证据）。

## 8. 三版验收门槛（DoD 摘要，详见 `12` §4）

### V1
- 评测集（≥20 样本）上：Precision ≥ 0.7，位置准确率 ≥ 0.8，无缺陷噪声 ≤ 1 条/PR。
- **per-file map-reduce 契约落地**：文件任务并发、单文件失败 PARTIAL、成本/耗时与 `file_tasks/model_requests` 配置一致。
- dry-run 与发布双路径端到端通过；幂等验证通过（同 PR 二次运行零增量）；Saga 发布故障注入后重跑恢复通过（marker + remote_comment_id）；supersede 清理失败不阻塞 watermark（cleanup_pending）。
- 覆盖清单（CoverageItem）、成本、Token、耗时落库；单文件失败 → PARTIAL 正确；**部分成功归并正确**：optional 任务失败 → COMPLETED+warnings，分析状态与 publish_status 分离（P1-R2-4）。
- 安全组：secret 脱敏、路径沙箱、注入样本不遵从（注入疑似记为遥测，不默认进评论）。

### V2
- 角色子集（general+security+silent-failure）门控命中率与 Recall 提升对照达标；Strategy 只产出候选，统一 Pipeline 为唯一生命周期所有者。
- 去重/Judge（keep/downrank）后重复率 < 0.25；**指纹两层键**（fingerprint/cross_run_match_key，最终命名）与"聚类先于指纹"正确实现；反馈记忆条件化匹配：标记后重审不再报告（撤销后恢复），代码移动后仍可命中（P0-R2-1）。
- watermark/Saga 发布故障注入恢复通过；needs_evidence 内部标记降级正确。

### V3
- 跨文件评测收益对照达标（§7）；工具预算（reserved finalize）/重复/取消/压缩/grace 全状态机测试通过，含 WAITING_TOOL 异常出口。
- 终局验证：所有保留 Finding 证据可追溯；非法终局（WAITING_TOOL 残留）被拦截。
- 保留确定性最小 L3 后再增量探索；工具方案 A/B 在同一评测集对比，方案以评测为准。

### v1.0.0
- GitHub Action 接入演示仓库；线上冒烟 10 次无失败发布；文档/评测/安全报告齐全。
