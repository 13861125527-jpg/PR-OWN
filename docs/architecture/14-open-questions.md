# 14 — 开放问题（Open Questions）

> 需用户确认或实测的事项；不得用无依据假设填补。每项含影响范围与决策触发点。
> 标注：`V1 now` / `V2 later` / `V3 later` / `optional`。

---

## OQ-1：DP-V4-PRO 能力实测（最高优先，V1 前必须完成）

| 项 | 验证方法 | 影响 |
|----|----------|------|
| OpenAI-compatible API 可用性 | 最小请求 | 全部（适配基础） |
| 异步调用与并发表现 | 并发 3 请求测延迟/429 | 并发设计（`04` §6） |
| 稳定 JSON / JSON Schema | 重复 20 次：解析成功率、字段漂移率 | V1 结构化策略（`09` §3） |
| tool calling 是否可用 | schema 往返成功率 | V3 方案 A/B（`08` §9） |
| 真实上下文长度 | 实测可用上限 | Token 预算 32k 校准（`06` §2） |
| 限流与价格 | 单价、速率实测 | 成本预算校准（`09` §5） |

## OQ-2：语言与评测集启动范围

- 首发只 Python：确认评测集规模（建议 ≥20 样本起步）与来源优先级：人工单缺陷 PR 优先，还是真实修复反向样本优先（影响 Phase 0 投入）？

## OQ-3：发布行为默认值确认

- `request_changes: false`（只 comment）是否接受为默认？
- 行内评论是否同样默认关闭，只发 summary comment（Action 场景）？
- 正式发布是否需要二次确认（CLI `--yes`）？

## OQ-4：误报反馈入口

- V2 反馈记忆的交互方式：CLI 子命令（`reposage feedback mark --id ... --kind wont-fix`）还是 GitHub Comment 指令（`/reposage-wontfix`），或两者？
- 影响：入口层与发布层各多一块工作；评论指令会引入"谁来评论可信"的问题（需鉴权）。

## OQ-5：静态分析器与角色子集选择

- 首发静态分析器确认 ruff（B/S 子集）？是否需要 bandit 单独引入？
- V2 角色子集：general + security + silent-failure + test 是否足够起步（concurrency/edge-case 延后）？
- 影响：`07` §2/§4 的角色与融合实现量。

## OQ-6：head SHA 漂移策略

- 审查期间 PR head 更新：**拒绝并提示重跑**（默认，简单可预测）还是**自动用新 SHA 重启一次**（体验好但复杂）？
- 影响：`10` §7 与 `04` §8。

## OQ-7：模型结果缓存策略

- 评测/回放模式显式缓存模型结果（默认关闭）是否可接受？生产默认不缓存（隐私与陈旧风险）——确认无异议。

## OQ-8：SQLite 数据保留与隐私默认

- 运行记录保留策略（无限期 vs 保留 N 天）；`retain_source_in_evals: false` 默认剥离源码——确认默认值。

## OQ-9：GitHub Action 触发范围（v1.0.0）

- Action 仅 `pull_request`（open/synchronize）事件，还是也支持 `pull_request_review_comment`（评论指令触发）？
- 是否需要在 Action 场景限制为"只发 summary comment"以控制成本？

## OQ-10：项目命名与仓库形态

- 项目名 `RepoSage` 是否最终确认？仓库与 Python 包名（`reposage`）一致即可。
- 本轮只交付设计文档；下一轮是否直接进入 Phase 0 工程骨架？

## OQ-11：tool calling 方案对比实验设计（V3 前）

- 确认实验协议：在**同一评测集**上对比方案 A（支持 native tool calling 的模型）与方案 B（DP-V4-PRO 的 Action JSON 协议）的可靠性与成本（`08` §9）。
- 若方案 B 的 JSON 可靠性不足，是否接受 V3 **暂缓**（先交付 V2 + 工具验证增强），而不是为保留首选模型牺牲 Agent 稳定性？

---

## 决策登记（更新后同步回对应文档）

| 问题 | 决策 | 影响文档 |
|------|------|----------|
| OQ-1 | **V1-c 部分落地**：OpenAICompatProvider + 严格结构化（schema_first/json_repair + 修复重试）已实现并有 MockTransport 边界测试；smoke 入口就绪（默认 20 轮/并发 3/漂移率/脱敏报告）。**真实 DP-V4-PRO 实测待配置 `MODEL_API_KEY`/`MODEL_BASE_URL` 后执行**（当前阻塞，见 `docs/evidence/v1-c-dp-v4-pro-smoke.md`）；tool calling 留 V3 方案 A/B 同集对比（OQ-11） | 09 §3 / 08 §9 / 12 V1-c |
| OQ-3 | 默认值待确认（当前按附录 B：dry_run=true、request_changes=false） | 07 §7 / 09 §6 |
| OQ-4 | 待定 | 07 §7 / 02 §3 |
| OQ-6 | 默认拒绝+提示（未确认前） | 10 §7 |
| 其余 | 待确认 | – |

## 第一轮审查修订登记（2026-08）

已按 `clipboard-20260813-231742` 审查报告完成修订，逐条回应见 `15-revision-notes.md`：

- P0-1~P0-6 全部修订（FindingPipeline 唯一所有权、Saga 发布、三身份、claimed/canonical、V1 map-reduce、reserved finalize budget）。
- P1-1~P1-10 全部修订（dry-run、V3 最小 L3、裁剪安全、WAITING_TOOL 出口、attempt 预算、decision_summary、注入遥测、信任边界、ER 修正、幂等实现）。
- 版本安排与过度设计收敛（C4 标注、边界流程、里程碑先本地）已并入 04/12/11/13。

> 未确认项均采用**安全可逆的默认值**先行设计；确认后仅需修改对应章节与配置默认，不改变架构。
