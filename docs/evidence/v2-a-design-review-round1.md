# V2-A 设计对齐稿第一轮审查

> 审查对象：`docs/architecture/18-v2a-alignment.md`  
> 审查结论：**设计方向正确，但暂不建议进入生产代码；需先关闭 4 个实现级缺口并确认决策点。**

---

## 1. 总体评价

这份对齐稿已经正确覆盖：

- V1 组件复用和“不重写”原则；
- V2-A 与 V2-B/C/D/E、V3 的能力边界；
- Role Registry、确定性 Gate、三层并发和 per-file Barrier；
- required/optional 失败矩阵；
- Candidate-only 与 FindingPipeline 唯一生命周期所有权；
- Prompt 分层、预算、安全、取消、可观测和评测；
- 分步任务卡与 DoD。

文档不存在“推倒 V1 重写”或“提前偷做 Agent”的问题。下面是编码前必须闭环的实际契约问题。

---

## 2. Blocking-1：`MultiRoleReviewer` 拿不到 Gate 所需的 `ChangedFile`

### 问题

文档定义 `GateFeatures` 来源为 `ChangedFile`，需要：

- path / language / status；
- added imports；
- added lines 关键词；
- 是否纯删除；
- 新增行中的结构特征。

但现有 `ReviewStrategy.execute()` 的输入只有：

```python
execute(units: list[ReviewUnit], run: ReviewRun, budget: GlobalBudget)
```

`ReviewUnit` 不是 `ChangedFile`，Strategy 也拿不到 `ReviewService.file_map`。文档又明确禁止 Strategy 重做 diff 解析。因此，按当前接口无法依照 §5 实现纯函数门控。

### 建议方案

不建议为 V2 修改 `ReviewStrategy.execute` 主签名，否则 SinglePass、EvalRunner 和自定义 Strategy 都受影响。

建议在 CONTEXT 阶段一次性生成可序列化的 `GateFeatures`，然后使其随 `ReviewUnit` 或文件级 `ReviewInput` 传入 Strategy。可选形式：

1. **推荐：** `ReviewUnit.gate_features: GateFeatures | None`，同文件多 unit 复用相同值；SinglePass 忽略该字段。
2. 引入 `FileReviewBundle(file_path, units, gate_features)` 作为新 Strategy 输入，但这会扩大通用接口改动面。
3. 在 `ReviewContext` 中增加最小的程序事实，但不要让 Gate 反向解析 prompt 文本。

必须在文档中明确选定一种形式，并加入 T1/T4 的测试和兼容性要求。

---

## 3. Blocking-2：required 优先不能靠“先提交任务”保证

### 问题

§7 计划“每文件先提交 required，再提交 optional”，期望预算将尽时 general 能先获得预算。

但如果所有 coroutine 仍一次性交给 `asyncio.gather`，提交顺序不等于：

- Semaphore 获取顺序；
- `GlobalBudget.reserve()` 的顺序；
- required 必然在 optional 之前得到 Token/费用。

不同文件的 optional 任务可能抢先获取 role/model semaphore，把剩余预算消耗掉，导致 required general 被拒绝。

### 建议方案

V2-A 先采用最容易验证的 **两阶段准入**：

```text
Phase 1: 所有 kept 文件的 required role 进入调度/预留
Barrier-required
Phase 2: 再调度 optional role
Barrier-optional
```

这会降低一部分并发度，但语义清晰、测试可靠，适合 V2-A 第一版。

如果必须保持单阶段高并发，则需实现真正的优先级调度器或 required 预留水位，不能只依赖 list 顺序。

必须添加压力测试：总预算只够 required，大量 optional 已创建且并发竞争时，所有 required 仍先完成，optional 才被拒绝。

---

## 4. Blocking-3：Gate 决策只写 stderr 日志，无法持久审计

### 问题

DP-3 提案是：Gate miss 不写 CoverageItem，只写 `gate_decision` 结构化日志和评测 sidecar。

当前 `StructuredLogger` 默认写 stderr，不是可查询的长期存储。运行结束后，数据库只能看到“哪些 role 运行了”，无法回答：

- 某角色是 gate miss、disabled、lang miss 还是 no added lines？
- 门控规则在生产 run 中为什么没启用 security？
- 该次“未覆盖”是设计跳过，还是任务失败？

门控本身是 V2-A 的核心产品决策，不应只存在短期日志中。

### 建议方案

不建议把每个 miss 当成“失败 Coverage”；它是有意识的调度决策。可选：

1. **推荐：新建 `gate_decisions` 表**，保存 `run_id, file_path, role_id, enabled, reason, matched_features_json, gate_version`。
2. 在 `coverages.items_json` 中增加 `gate_miss/role_not_selected/role_disabled` reason，但会让 coverage 清单变大。
3. 将整个 GateDecision sidecar 作为 run 级 JSON 落库，比新表简单，但查询能力较弱。

建议选 1；Gate eval 可直接复用该数据结构。应按 `run_id + file_path + role_id` 幂等 upsert，并保存 gate 规则版本/哈希，否则后续无法还原旧决策。

DP-3 建议改为：**Gate miss 不建 ReviewTask，但 GateDecision 全部落库。**

---

## 5. Blocking-4：Run 状态归并所需的结构化结果未进入通用契约

### 问题

文档正确指出当前 Service 使用“只要有 warning 就 PARTIAL”的方式不能支持 optional 失败。

但现有 `StrategyResult/SourceRunResult` 主要包含：

- candidates；
- tasks；
- usages；
- warnings。

Service 若只看 warnings 或遍历 task，并不知道某失败角色是 required 还是 optional，也不知道哪个 kept 文件的 required coverage 缺失。

如果让 ReviewService 反向查 Registry 并解析 `task.target="path::role"`，会把 MultiRole 细节泄漏到通用编排层。

### 建议方案

扩展 `SourceRunResult` 或 `StrategyResult` 的通用归并契约，例如：

```python
required_failed: bool = False
required_failure_count: int = 0
optional_failure_count: int = 0
coverage_complete: bool = True
```

或定义通用 `StrategyOutcome`/`StrategyHealth`。Service 只根据通用结果定稿：

```text
fatal exception                         -> FAILED
required_failed or !coverage_complete  -> PARTIAL
only optional failures                 -> COMPLETED + warnings
all success                            -> COMPLETED
```

SinglePassReviewer 必须同步填充该通用结果，以保留“文件审查失败导致 PARTIAL”的 V1 语义。

同时必须明确：

- optional warnings 不降级；
- 普通 skip warning 是否降级，不再通过“有 warning”间接决定；
- publish partial 只改 `publish_status`，不改分析 status；
- CancelledError 不能被 `except Exception` 或 gather 当作 role failure。

T1/T6/T8 必须加入该契约，而不是只在 T8 修一个 `if warnings`。

---

## 6. 需在文档中同步修正的 should-fix

### 6.1 取消状态尚未对齐现有 Service

失败矩阵写了 `CancelledError → CANCELLED`，但当前 `ReviewService.review()` 不会在取消时定稿/落库 CANCELLED，而是让 `CancelledError` 穿透。

设计必须二选一：

- V2-A 补充 Service 取消持久化；或
- 明确 CANCELLED 运行定稿后置，V2-A 只保证不吞取消。

不能在 DoD 中宣称已支持 CANCELLED，但任务卡没有对应实现。建议 V2-A 直接闭环运行状态落库，并保留 `raise`。

### 6.2 超时口径需与 Provider 一致

§11 写“单次调用 `min(llm.timeout_seconds, budget.remaining)`”，但 `LLMProvider` Protocol 不暴露 `timeout_seconds`。

建议沿用 V1 契约：Strategy 用 `asyncio.timeout(budget.remaining_runtime_seconds)` 控制全局剩余墙钟，Provider 内部使用 Settings 配置的请求超时。不要让 MultiRole 读取 Protocol 中不存在的属性。

### 6.3 Gate 目标口号与指标倾向相互矛盾

文档写“Recall ≥ 0.8、Precision ≥ 0.6（宁漏叫不可乱花）”。

“宁漏叫不可乱花”是偏 Precision，而当前阈值更偏 Recall。建议改为：

- 如果优先不漏安全角色：Recall ≥ 0.8、Precision ≥ 0.6，文案写“安全角色优先 Recall”；
- 如果优先成本：Precision 门槛应高于 Recall。

可按角色分开：security 偏 Recall，performance 偏 Precision，不必所有角色共用一个阈值。

### 6.4 `path_io` 规则过宽

`open(` / `Path(` / `os.path` 几乎能在大量普通文件处理中启用 security，很可能拉低 Gate Precision 并显著增加成本。

建议不将单独 `open(`/`Path(` 作为安全角色充分条件，而是与外部输入、上传/解压路径、Web handler、用户控制参数或敏感目录路径组合命中。

### 6.5 Role Registry 配置语义需只有一个权威来源

文档同时出现：

- `review.roles` 启用列表；
- 内置 spec 的 `enabled`；
- per-role overlay 的 enabled/required/weight；
- “注册但默认关闭”占位角色。

需明确唯一合并规则，例如：

```text
builtin spec defaults
  < trusted base-branch repository config
  < explicit CLI/Action overrides
effective enabled = id in review.roles AND overlay.enabled != false
```

角色 required 不得被 PR 分支配置降级；不得出现 `review.roles` 说启用、spec.enabled 说关闭但没有确定结果的情况。

---

## 7. 决策点建议

| ID | 建议结论 | 理由 |
|---|---|---|
| DP-1 | **确认** general/security/correctness/performance | 角色数适中；correctness 可吸收 silent-failure/edge-case |
| DP-2 | **确认** per-file Barrier | 与 V1 map-reduce 和文件失败隔离一致 |
| DP-3 | **修改**：miss 不创建 Task，但全部 GateDecision 落库 | 保留审计性且不污染任务语义 |
| DP-4 | **确认**：V2-A 不缩放 max_output | 先降低预算策略复杂度 |
| DP-5 | **修改**：按角色定门槛；security 偏 Recall，correctness/performance 平衡/偏 Precision | 角色风险与成本不同 |
| DP-6 | **确认**：只有 general required | 符合 optional 角色失败不降级的目标 |
| DP-7 | **确认**：默认仍 single_pass | 允许对照、保护成本与 V1 兼容 |
| DP-8 | **新增**：GateFeatures 如何随通用输入传入 Strategy | Blocking-1 |
| DP-9 | **新增**：required 优先采两阶段准入还是优先级调度器 | Blocking-2；推荐两阶段 |
| DP-10 | **新增**：Strategy 通用归并契约 | Blocking-4 |

---

## 8. 要求 Cursor 一次性修改的清单

请只修改 `docs/architecture/18-v2a-alignment.md`，仍不写生产代码：

1. 选定 GateFeatures 进入 Strategy 的具体契约；
2. 将 required 优先从“提交顺序”改为可证明的准入/调度策略；
3. 将 GateDecision 全量持久化纳入存储契约、迁移和测试；
4. 定义 Strategy 向 Service 返回 required/optional/coverage 结果的通用结构；
5. 明确 CancelledError 的 Run 持久化语义；
6. 修正 timeout 口径，不依赖 Protocol 不存在的属性；
7. 修正 Gate 指标文案/分角色门槛；
8. 收紧 `path_io` 安全规则；
9. 补充 Registry 配置的唯一合并优先级；
10. 更新任务卡、DoD、测试矩阵和 §15 决策点。

修订后再进行一次设计复验。设计通过后，再从 T1 开始实现；不要跳到 T6 直接写 MultiRoleReviewer。

---

## 9. 最终判定

| 方面 | 判定 |
|---|---|
| V1 复用与版本边界 | 通过 |
| Role/Prompt/Gate 概念模型 | 基本通过 |
| 三层并发和 Barrier | 方向通过，required 优先需修订 |
| Gate 输入契约 | **未闭环** |
| Gate 持久审计 | **未闭环** |
| required/optional 通用归并契约 | **未闭环** |
| 评测与 DoD | 基本通过，需修正门槛口径 |
| 是否可进入编码 | **否，先修订设计稿** |
