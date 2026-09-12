# V2-A 实现复验报告（Round 1）

> 复验对象：`D:\vscodeprojects\PR-OWN` 当前工作区  
> 结论：**暂不通过 V2-A 最终验收**。主链路已经形成，V1 回归与静态检查通过；但 required/optional 阶段隔离、门控可信度和版本对照证据仍有阻塞问题。

## 1. 本轮实际验证结果

| 检查项 | 结果 |
|---|---|
| 全量测试 | `380 passed` |
| Ruff | 通过（扫描临时目录时出现一次“拒绝访问”警告，但退出码为 0） |
| mypy strict | 通过，检查 53 个源文件 |
| diff check | 无空白错误；仅有 LF/CRLF 提示 |
| V1 默认策略 | 仍为 `single_pass`，兼容路径保留 |
| V2-A 主体 | Registry、确定性 Gate、MultiRole、StrategyHealth、GateDecision 持久化均已落地 |

这些结果说明代码并非不可用；当前问题集中在“是否严格满足已批准的 V2-A 契约”和“交付证据是否足以验收”。

## 2. Blocking：必须修复后再验收

### B1. Phase 1 发生非取消异常时，仍会创建并执行 Phase 2

位置：`reposage/review/reviewers/roles/multi_role.py`，约 131–199 行。

当前流程：

1. `phase1 = await asyncio.gather(..., return_exceptions=True)`；
2. `_reraise_cancelled(phase1)` 只立即传播 `CancelledError`；
3. 随即创建并运行 Phase 2 optional；
4. 到最终遍历 `(*phase1, *phase2)` 时，才重新抛出 Phase 1 中的其他异常。

因此，只要 `_run_file_roles` 在 Phase 1 批次层出现未被内部转换的异常，optional 调用就可能在 required 批次已结构性失败后继续消耗预算。这不符合设计稿 18 §6.2 的约束：**Phase 1 未完成时 Phase 2 coroutine 不创建**。

一次性修改要求：

- Phase 1 gather 返回后，在创建 Phase 2 前检查所有结果；
- `CancelledError` 继续原样传播；
- 其他批次级异常应立即失败关闭，或转换成 required failure 后直接结束 Strategy，但不得创建 optional coroutine；
- 增加回归测试：人为让 Phase 1 的 `_run_file_roles` 抛出非取消异常，断言 optional 的 LLM 调用数严格为 0。

### B2. Gate 特征提取与设计语义不一致，当前 1.000 指标不能证明门控可靠

位置：`reposage/review/reviewers/roles/gates.py`、`reposage/evals/datasets/v2a_gate.yaml`、`tests/test_gates.py`。

至少存在以下三个边界错误：

1. **多行 docstring**：当前只跳过以三引号开头的那一行，没有维护“正在 docstring 内”的状态。docstring 后续行中的 `eval` 等文本可能误启用 security。
2. **多行 except/pass**：`except_swallow` 主要按单行模式识别，常见的两行写法 `except Exception:` 后缩进 `pass` 可能漏报。
3. **非嵌套循环**：当前用文件中 loop 命中次数 `>= 2` 近似“双层循环”。两个顺序执行、互不嵌套的循环也可能误启用 performance。

这会直接影响 Gate Precision/Recall 和角色调用成本。当前 17 条数据与实现模式高度同构，所以三个角色得到 1.000；该结果没有覆盖上述真实代码形态。另外总体样本数低于通用评测文档中“≥20 样本”的基准。

一次性修改要求：

- 使用最小的状态/缩进感知扫描，正确跳过完整多行 docstring；
- `except_swallow` 支持相邻缩进行的 `pass/continue`；
- `loop_nested` 根据缩进/结构判断嵌套，顺序循环不得命中；保留 `itertools.product` 的明确规则；
- 数据集和单元测试加入：多行 docstring 负例、多行 except/pass 正例、两个顺序循环负例、真实嵌套循环正例；
- 扩充后重新生成分角色 Precision/Recall，不要硬写结果。

### B3. `gate_version` 不是规则集 content hash

位置：`reposage/review/reviewers/roles/gates.py`，约 61 行。

当前 `GATE_VERSION` 只对规则名称列表做 hash。修改正则内容、路径集合、组合判断、角色映射或提取算法时，只要名称不变，版本号就不会变化。这无法满足设计稿 18 §4/§10 中“规则集 content_hash、可还原旧决策”的审计目标。

一次性修改要求：

- 将所有决定 Gate 行为的内容组成规范化 manifest：规则名、regex pattern 与 flags、路径集合、组合规则版本、角色映射、提取算法语义版本；
- 对稳定序列化后的 manifest 求 hash；或者使用明确的人工语义版本，并把更新纪律写进测试；
- 增加测试证明规则 manifest 改变会导致版本改变，避免只测试字符串非空。

### B4. V1 vs V2-A 三张对照表尚未真正完成

位置：`docs/evidence/v2-a-compare.md`。

设计稿 11 §7 与 18 §12.4/T10 要求同集上的**质量、成本、延迟**三张数值对照表。当前报告存在这些缺口：

- 质量部分主要是 Gate 自身指标，不是 V1 与 V2-A Finding 质量对照；
- 成本只有“1 次/≥2 次”和文字说明，没有双方 input/output/total tokens、调用数、预算拒绝数等数值；
- 延迟只有“V2-A ≥ V1”的定性判断，没有统一测量条件下的总耗时或 p50/p95；
- 没有一个可重复运行的 V2-A compare runner 生成报告，当前数字和文字容易漂移。

真实 API **不是本里程碑通过的必要条件**，可以先用脚本化 Fake 完成可重复的数值对照；真实 API 单列为“未运行”。但 Fake 也必须基于同一数据集、同一计量口径，并真正输出三表。

一次性修改要求：

- 增加可执行的 V1/V2-A compare runner；
- 同一评测集分别运行 `single_pass` 与 `multi_role`；
- 输出三张数值表：
  - 质量：Finding Precision / Recall / F1、位置准确率、噪声；
  - 成本：模型调用数、input/output/total tokens、预算拒绝次数（Fake 值明确标注）；
  - 延迟：总耗时及至少一种稳定统计口径（建议 repeats 后 p50/p95）；
- 报告必须由 runner 生成或写明原始 JSON 证据路径，避免手工结论；
- Gate Precision/Recall 作为独立辅助表保留，不要替代 Finding 质量表。

## 3. Should-fix：建议本轮一起处理，避免下一轮再返工

### S1. 注册了四个无法实际加载的占位角色

Registry 中有 `silent-failure`、`edge-case`、`concurrency`、`test`，但 `reposage/prompts/roles/` 目前只有 general/security/correctness/performance 四个 prompt。占位角色默认关闭没有问题，但它们仍属于“已注册 ID”；受信任配置若启用，配置校验可通过，随后初始化时可能因 prompt 缺失失败。

建议二选一并加测试：

- Registry 增加 `implemented/available` 标记，禁止配置启用未实现角色；或
- 补齐 prompt 和对应最低行为测试。

V2-A 若明确只实现四角色，优先采用第一种，更符合“占位但不可启用”的含义。

### S2. 清理或隔离测试临时目录权限警告

Ruff 本轮虽然通过，但出现 `.tmp-pytest` 一类目录的访问拒绝警告。建议在 Ruff exclude 中明确排除项目测试临时目录，或让测试固定使用可清理的临时路径，保证 CI 输出无歧义。

## 4. 已确认正确、不要无谓重写的部分

- 默认 V1 `single_pass` 未被替换；V2-A 需要显式选择。
- 角色输出会由程序覆盖 `role_id`，没有信任模型伪造角色身份。
- GateDecision 已进入持久化链路，且 run 先落库，外键顺序合理。
- Service 已通过通用 `StrategyHealth` 归并 required/optional，不再解析 task 名称猜语义。
- optional 普通失败保持 COMPLETED + warning，required 失败进入 PARTIAL 的总体方向正确。
- 取消传播与 cancelled run 落库主路径已有覆盖。

## 5. 给实现者的建议执行顺序

1. 先修 B1 并补 fail-closed 测试；
2. 一次完成 B2+B3（Gate 语义、数据集、版本 hash 同时变化）；
3. 完成 B4 的 compare runner 和自动报告；
4. 处理 S1 占位角色可用性；
5. 全量执行 pytest、Ruff、mypy、diff check；
6. 把命令结果、三表和变更摘要追加到本报告下方或新建 Round 2 修复报告。

## 6. 下轮通过条件

- B1–B4 均关闭且有针对性自动测试；
- 全量 V1/V2 测试继续通过；
- `v2-a-compare.md` 含可追溯的质量/成本/延迟三张数值表；
- Gate 新边界样本达到角色门槛；
- 占位角色不会出现“配置合法、运行时缺 prompt”的不一致；
- 无需真实模型 API 才能通过 V2-A；真实 API 结果可以继续标注为未运行。
