# V2-A 设计对齐稿第二轮复验

> 复验对象：`docs/architecture/18-v2a-alignment.md`  
> 对照意见：`docs/evidence/v2-a-design-review-round1.md`  
> 结论：**设计通过，可以进入 V2-A 实现，从 T1 开始。**

---

## 1. 第一轮四个 Blocking 复验

| 编号 | 上轮问题 | 新版方案 | 判定 |
|---|---|---|---|
| Blocking-1 | Strategy 拿不到 `ChangedFile` 门控事实 | CONTEXT 从 `ChangedFile` 抽取 `GateFeatures`，写入 `ReviewUnit.gate_features`；不改 `execute` 签名 | **通过** |
| Blocking-2 | required 只靠提交顺序，无法防止 optional 抢预算 | run 级 Phase 1 required 全部结束后，才创建 Phase 2 optional coroutine | **通过** |
| Blocking-3 | Gate miss 只在 stderr，无持久审计 | 新增 `gate_decisions` 表，v3→v4，所有已评估 hit/miss 幂等落库 | **通过** |
| Blocking-4 | Service 无法区分 required/optional 失败 | 新增通用 `StrategyHealth`，Service 不再根据 warnings 或 task target 推断 | **通过** |

---

## 2. Should-fix 复验

| 项目 | 新版处理 | 判定 |
|---|---|---|
| CancelledError | Service 落库 `CANCELLED` 后重新抛出，不吞取消 | 通过 |
| 超时契约 | Strategy 只用 budget 剩余墙钟；HTTP 超时留在 Provider/Settings | 通过 |
| Gate 指标 | security 偏 Recall、correctness 平衡、performance 偏 Precision | 通过 |
| `path_io` | 单独 `open`/`Path` 不启用 security，必须有组合特征 | 通过 |
| Registry 合并 | 已定义 builtin < user < trusted repo < CLI，并禁止 required 降级 | 通过 |

---

## 3. 确认的实现顺序

允许开始编码，但必须按设计稿 §13 的依赖顺序：

1. **T1 领域契约**：`GateFeatures` / `GateDecision` / `StrategyHealth` / `ReviewUnit.gate_features` / `FindingCandidate.role_id` / `SourceRunResult` 扩展。
2. **T2–T3 配置与 Registry**：先固定有效角色与合并规则。
3. **T4–T5 Gate 与 Prompt**：先有纯函数门控、标注数据和 prompt 黄金测试。
4. **T6 MultiRoleReviewer**：在上述契约已稳定后再实现三层并发和两阶段准入。
5. **T7–T9 Pipeline / Service / Storage / Logging**。
6. **T10–T11 评测对照与全量回归**。

不得跳过 T1–T5 直接编写 MultiRoleReviewer。

---

## 4. 实现时必须遵守的三条细节

以下不再阻塞设计通过，但必须进入代码审查清单。

### 4.1 `role_id` 必须由程序盖戳

`FindingCandidate.role_id` 是来源事实，不是模型自报字段。

- MultiRoleReviewer 知道当前 role，必须在程序中对模型返回 Candidate 统一设置/覆盖 `role_id`。
- 不能信任模型输出的 role 声明。
- Pipeline 只使用程序盖戳后的值构造 `LLM_ROLE` source。
- 测试：即使模型伪造其他 role_id，最终 source 仍是实际调用的角色。

### 4.2 Gate 审计表不存源码片段

`matched_features_json` 只保存规则 ID/规范化标签，例如 `exec_dyn`、`path_auth`、`path_io+user_input`，不保存原始新增行、字面密钥或源码摘要。

这保证 `gate_decisions` 表可持久审计，又不成为新的源码/Secret 泄漏面。

### 4.3 两阶段压力测不能误解结算语义

两阶段保证的是：**optional 不会在 required 获得准入之前抢预算**。

如果 required 调用结算后释放了未使用的最坏预留，Phase 2 使用真实剩余预算是正确行为。因此压力测试不应无条件断言“optional 调用数必须为 0”，除非测试保证 required 的实际结算已用尽预算。

必须分别测试：

1. Phase 1 完成前 optional 调用数为 0；
2. Phase 1 完成后若有真实剩余，optional 可使用；
3. 若无真实剩余，optional 被拒且不超发；
4. optional 无论如何不影响已完成的 required 结果。

---

## 5. 非阻塞文档问题

`18-v2a-alignment.md` 文件末尾多了一个独立的 `)`，实现开始前顺手删除即可，不影响设计验收。

---

## 6. 最终判定

| 方面 | 判定 |
|---|---|
| V1 复用与版本隔离 | 通过 |
| GateFeatures 输入契约 | 通过 |
| GateDecision 持久审计 | 通过 |
| required 两阶段准入 | 通过 |
| StrategyHealth 通用归并 | 通过 |
| 取消/超时/降级 | 通过 |
| Registry 配置契约 | 通过 |
| Gate 评测与分角色门槛 | 通过 |
| 是否允许开始编码 | **是，从 T1 开始** |

V2-A 设计阶段至此冻结。后续如遇到实现细节，优先按本文与 `18-v2a-alignment.md` 的已确认契约执行，不重开整体架构。
