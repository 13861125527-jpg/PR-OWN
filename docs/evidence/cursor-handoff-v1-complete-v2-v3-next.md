# RepoSage Cursor 交接文档：V1 完成，准备进入 V2/V3

> 交接日期：2026-08-15  
> 项目目录：`D:\vscodeprojects\PR-OWN`  
> 当前 Git HEAD：`0a8052a docs(v1-d): 记录基线提交 SHA 950b718`  
> 重要：当前 V1-E/V1-F 实现大部分还在未提交工作区中，**不要 reset、checkout 或覆盖这些修改**。

---

## 1. 给 Cursor 的首要指令

1. 先完整阅读本文档，再阅读 `docs/architecture/00` 至 `14` 中与当前任务相关的章节。
2. 将 V1 视为已通过基线，不要重写 V1 架构，不要破坏 344 项回归测试。
3. 不要一次同时实现 V2 和 V3。执行顺序必须是：`V2-A → V2-B → V2-C → V2-D → V2-E → V3-A → V3-B → V3-C → V3-D`。
4. 当前只启动 **V2-A 设计对齐**；先写设计文档、任务拆解和验收标准，经审查后再写代码。
5. 每个里程碑都要同时考虑：领域模型、架构、上下文、并发、预算、存储、安全、可观测性、评测、提示词、失败/降级和兼容性。
6. 任何新 Strategy/Role/Agent 都只能产出 `FindingCandidate`；`FindingPipeline` 仍是 Finding 正式生命周期的唯一所有者。

---

## 2. V1 最终状态

V1-a 至 V1-f 均已完成并验收：

| 里程碑 | 完成能力 | 状态 |
|---|---|---|
| V1-A | diff 解析、行号映射、文件过滤 | ✅ |
| V1-B | L0–L2 上下文、内置规则、Token 预算和分块 | ✅ |
| V1-C | OpenAI-compatible LLM Provider、结构化输出、修复重试 | ✅ |
| V1-D | SinglePassReviewer、per-file map-reduce、FindingPipeline | ✅ |
| V1-E | dry-run/正式发布、Saga/Outbox、幂等、恢复、cleanup、并发租约 | ✅ |
| V1-F | Run/Task/Usage/Stage/Coverage 落库、JSONL 日志、脱敏、配置快照 | ✅ |

当前最终门禁：

- Pytest：**344 passed**
- Ruff：**All checks passed**
- mypy strict：**Success: 46 source files**
- `git diff --check`：通过，仅有 Windows LF/CRLF 提醒

---

## 3. V1-E 已完成的重要能力

### 3.1 Publishing/Saga

- `reposage/publishing/publisher.py` 已实现发布计划生成、正式发布和恢复。
- 必要评论未全部成功时进入 `partial`，不错误推进 watermark。
- `published + committed_watermark + cleanup operation` 在同一事务中落库。
- cleanup 失败进入 `cleanup_pending`，可恢复重试。
- 稳定 marker + remote comment ID 实现跨 run 幂等。
- 新 head 不会被旧 head 的 partial plan 吞掉；旧计划可进入 obsolete。

### 3.2 并发租约与 fencing

- 发布前使用 CAS claim，只有抢占成功的 Worker 可进行远程副作用。
- 租约丢失后抛出 `LeaseLostError`。
- plan status、comment results、operation、finding status、remote ID 退役、cleanup 等主要写入均受 fencing 保护。
- 带 owner 条件的 UPDATE 检查 `rowcount == 1`，不再静默失败。
- `try/finally` 保证正常、partial、失败和 cleanup 路径释放租约。
- 已有真实重叠并发测试和 stale Worker 接管测试。

非阻断的未来增强：

- 引入 heartbeat 时，`assert_lease` 可同时检查 `lease_until`。
- 已经发出的 HTTP 请求无法靠数据库 fencing 物理撤回，真实 GitHub Provider 必须保留 marker 幂等和小于租约的请求超时。

---

## 4. V1-F 已完成的重要能力

### 4.1 持久化数据

- `runs`：运行状态、开始/结束时间、发布状态、警告、配置哈希。
- `tasks`：文件任务状态、Token、成本和错误。
- `usages`：模型、角色、输入/输出 Token、费用、延迟、重试、schema repair、outcome。
- `coverages`：CoverageManifest/CoverageItem 及 truncated 标记。
- `run_stages`：stage、sequence、status、required、duration、Token、cost、error 和 detail。

`record_run()` 使用事务内重写 stage 快照，同一 run 定稿不会无限增长，同一 stage 可多次出现并按 sequence 还原。

### 4.2 结构化日志与配置快照

- `reposage/observability/logging.py` 提供最小 JSONL 日志。
- 日志包含 run/stage/task/tool/finding/fingerprint 关联字段。
- token、API Key、Authorization、私钥和常见连接凭证会脱敏。
- 日志 sink 失败不会导致审查失败。
- `Settings.snapshot_hash()` 为每个 run 生成稳定 SHA-256，不保存实际密钥。

### 4.3 最后一次由 Codex 直接修改的内容

最后遗留问题是阶段计时边界错位，已修复：

- FETCH 包含 `get_changes + require_head_locked + get_diff`。
- CONTEXT 包含 diff 解析、文件过滤、`build_file_units` 和 coverage 汇总。
- REVIEW 只从 `strategy.execute` 开始计时。
- PIPELINE 包含 FindingPipeline 和相关持久化。
- 失败 StageResult 根据当前阶段起点计算 duration，不再默认为 0。
- CONTEXT 在 diff parse/filter 之前切换 `current_stage`，解析失败不会错误归因 FETCH。
- `tests/test_service.py` 新增真实延迟测试，确认 FETCH/CONTEXT/REVIEW 分别包含自己的工作，并确认延迟后失败的 FETCH 耗时能落库。

---

## 5. 当前工作区：务必保留

当前修改/新增文件包括：

```text
M  reposage/config/settings.py
M  reposage/domain/enums.py
M  reposage/domain/protocols.py
M  reposage/domain/run.py
M  reposage/providers/git/fake.py
M  reposage/review/service.py
M  reposage/review/single_pass.py
M  reposage/storage/sqlite.py
M  tests/test_config.py
M  tests/test_fake_git.py
M  tests/test_service.py
M  tests/test_storage.py
?? docs/architecture/17-v1e-alignment.md
?? docs/evidence/v1-e-status.md
?? docs/evidence/v1-f-status.md
?? reposage/observability/logging.py
?? reposage/publishing/publisher.py
?? tests/test_logging.py
?? tests/test_publisher.py
```

这些是用户已经多轮审查通过的 V1-E/V1-F 工作，不是可丢弃的临时文件。

在开始 V2 之前，建议由用户确认后创建 V1 基线提交/标签。Cursor 不应在未经用户授权时自动 push、开 PR 或发布 tag。

---

## 6. 如何重跑 V1 门禁

在 PowerShell 中：

```powershell
cd D:\vscodeprojects\PR-OWN
.\.venv\Scripts\python.exe -m pytest -q

$env:RUFF_CACHE_DIR = "$env:TEMP\reposage-ruff-cache"
.\.venv\Scripts\python.exe -m ruff check .

.\.venv\Scripts\python.exe -m mypy --strict --cache-dir "$env:TEMP\reposage-mypy-cache" reposage

git diff --check
```

预期结果：344 passed，Ruff 通过，mypy 46 个源码文件通过。

---

## 7. V2 必须按顺序实现

### V2-A：Role Registry + Gate + Barrier

目标：从单角色审查升级为“根据 diff 特征按需启用多角色”。

必须包含：

- `RoleSpec`/Role Registry：角色 ID、prompt/template、gate、model requirement、enabled、required、预算权重。
- 首批角色建议：`general`、`security`、`correctness`、`performance`；角色数不要过早膨胀。
- Gate 必须以确定性代码为主，基于文件语言、路径、import、diff 关键词和变更类型。
- 角色调用只产出 `FindingCandidate`，不直接创建/推进 Finding。
- 文件与角色并发需分层 Semaphore，并共享 `GlobalBudget` 的原子预留/结算。
- Barrier 等待本批已选角色，必须支持 required/optional 的归并语义。
- optional 角色失败：Run 可保持 completed + warning；required 角色失败：根据架构归并为 partial/failed。
- 为每个 role task 落库 ReviewTask/Usage/CoverageItem/Stage 可观测数据。
- 评测 gate precision/recall，并与 V1 single-pass 进行质量、成本、延迟对照。

V2-A DoD：

1. Registry 加载与配置验证通过；
2. Gate 在标注样本上有可说明的命中率；
3. 角色并发不超过 file/model/role 三层限制；
4. required/optional 失败语义正确；
5. 所有角色只输出 Candidate；
6. 预算超限时拒绝新请求，不超发；
7. V1 全量回归通过；
8. 生成 V1 vs V2-A 对照评测报告。

### V2-B：符号级检索 L3

- Tree-sitter 或先用可替换的过渡实现生成 symbol index。
- 支持函数/类定义、引用和相关代码检索。
- 缓存键必须绑定 repo + head SHA + path/content hash。
- 以评测证明 L3 改善跨函数/跨文件问题，而不只是增加 Token。

### V2-C：静态分析器融合

- 首发 Ruff 子集。
- Analyzer 输出转为 Candidate/Source，程序事实不让模型篡改。
- 与 LLM 发现统一进入后续融合管道。

### V2-D：聚类、去重和 Judge

- 先做确定性 fingerprint/cross-run key/cluster，再做语义层 Judge。
- Judge 只能 `keep/downrank`，`needs_evidence` 为内部状态；不允许模型伪造已验证证据。
- 评估重复率、误合并率、额外成本与质量收益。

### V2-E：反馈记忆 + Watermark

- 支持 accept/ignore/false-positive 等反馈记忆。
- 匹配必须有 repo/scope/path/symbol/category/rule/key 等条件，不能全局粗暴压制。
- 支持 revoke/replay 和可审计版本。
- watermark 仍只在发布 Saga 达到既定成功点后推进。

---

## 8. V3 必须在 V2 稳定后实现

### V3-A：Tool Registry + Sandbox + Schema

- 少量通用只读工具：读文件、搜索文本、查符号/引用、读 diff、运行已允许的静态检查。
- ToolDefinition 包含 name/description/JSON Schema/permission/result limit/timeout。
- 路径必须 resolve 后限制在 workspace，拒绝 `..`、绝对路径、symlink escape 和超大返回。
- 参数必须在执行前通过 schema 验证。

### V3-B：Agent Loop

- 状态机至少包含 reasoning/waiting_tool/observing/finalizing/completed/failed/cancelled。
- 支持 max rounds/tool calls/tokens/cost/wallclock。
- 为 finalize 保留不可侵占预算。
- 实现取消、超时、重复调用检测、无进展终止和强制收敛。
- DP-V4-PRO 如无可靠 native tool calling，使用已设计的受限 action JSON 协议，但必须做 A/B 评测。

### V3-C：会话压缩 + 证据索引

- 压缩轨迹时保留已验证事实、工具来源、定位和未解决问题。
- 压缩后的 Finding 必须可追溯回原始 tool call/result。
- 程序确定性的最小 L3 证据不能被模型摘要替代。

### V3-D：跨文件对照评测

- 使用同一批跨文件样本对比 V2 固定管道与 V3 Agent。
- 对比 Precision/Recall/F1、位置准确性、证据可追溯性、成本、延迟、工具调用数和失败率。
- 若收益不显著，不强行保留 Agent 方案；可降级为“增强 V2 + 工具验证”。

---

## 9. 当前立即任务：只写 V2-A 设计对齐稿

Cursor 下一步应创建：

```text
docs/architecture/18-v2a-alignment.md
```

该文档至少包含：

1. 现有 V1 可复用组件盘点；
2. V2-A 产品目标与非目标；
3. RoleSpec/RoleRegistry/GateDecision/RoleTaskResult 领域模型；
4. MultiRoleStrategy 与现有 ReviewStrategy 的关系；
5. 门控特征表与首批角色规则；
6. 三层并发和 Barrier 时序；
7. GlobalBudget 在多角色下的原子预留；
8. required/optional 失败矩阵；
9. prompt 分层与角色边界；
10. tasks/usages/coverage/stages/logs 落库方案；
11. 安全、隐私、超时、取消和降级；
12. 门控评测数据集和指标；
13. 分步实施任务卡；
14. DoD 和审查清单；
15. 尚待用户确认的决策点。

在设计稿通过之前，不要新增 MultiRole 生产代码。

---

## 10. 架构不可破坏的原则

- 引用 `docs/architecture/12-roadmap-adrs.md` 的“版本间不重写承诺”。
- 复用入口、领域、Provider、Storage、Publishing 和 FindingPipeline，禁止复制一份 V2 专用主流程。
- 事实字段由程序验证，模型只提交候选。
- 角色是一次受控模型调用，不等于 V3 Agent。
- V2 不得偷偷引入无边界 Agent loop。
- 新能力必须有 Fake/确定性测试，再考虑真实 API 测试。
- 默认 dry-run；未经用户授权不发布到真实 GitHub。
- 新增日志和评测报告继续执行 secret 脱敏。
- 每个版本保留 V1 回归，并提供质量/成本/延迟对照。

---

## 11. 最终交接结论

V1 不再是“原型骨架”，已包含完整的单角色审查、Finding 管道、发布 Saga、幂等恢复、并发租约和可观测闭环。后续工作是在该基线上增量扩展，不是推倒重写。

**Cursor 现在应先产出 `18-v2a-alignment.md`，等待审查，不要直接开始 V2-A 代码实现。**
