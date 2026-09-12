# V2-C 设计对齐（静态分析器融合：ruff 子集）

> 状态：V2-C **ACCEPTED**（用户授权进入 V2-D；Round 1 B1/B2 已关，对照见 `docs/evidence/v2-c-status.md`）
> 前置：V2-A **ACCEPTED**；V2-B **ACCEPTED**（Round 3 + P2 覆盖聚合）
> 契约来源：`01` FR-17、`02` 静态分析器信任边界、`03` §2 `review/static/`、`04` §3 STAT par + Barrier、`05` FindingSource/EvidenceKind、`07` §4–§5、`09` §6 `review.static`、`10` 不可信输出与超时、`11` 重复率/对照、`12` V2-c / ADR-007、`14` OQ-5、交接 `docs/evidence/cursor-handoff-v1-complete-v2-v3-next.md` §7 V2-C
> DoD 摘要：**Analyzer 输出转为 Candidate/Source；程序事实不让模型篡改；与 LLM 发现进入同一 FindingPipeline；转换/去重正确；V1/V2-A/V2-B 回归通过**

---

## 0. 一句话与边界

V2-C 在 **REVIEW 阶段** 跑 1 个高价值静态分析器（ruff 的 B/S 子集），把诊断 **程序转换成** `FindingCandidate`，与 Strategy 的 LLM 候选一并交给现有 `FindingPipeline`。静态结果带来源 `static_analyzer`；路径/行号与模型候选同权校验；与 LLM 命中同一问题时 **来源合并、不重复计数**。

`ReviewStrategy.execute` 主签名不变。静态分析 **不是角色**，也不进 MultiRole 内部。SinglePass 与 MultiRole **共用** 同一批静态候选。

**本里程碑禁止**：Judge / Agent / 工具循环、bandit 第二分析器、把 ruff 原文塞进模型 prompt 让模型改写、为静态分析复制一套主流程、改 Publishing、改 V2-A/V2-B 默认对照开关。

---

## 1. 现有可复用组件盘点

| 层 | 组件 | V2-C 用法 | 缺口 |
|---|---|---|---|
| 编排 | `ReviewService.review()`：CONTEXT 之后 `strategy.execute` → Pipeline | REVIEW 内与 `execute` **并行**跑分析器；候选拼接后进 Pipeline | 无 analyzer 调用点；无静态 task/coverage |
| 策略 | `ReviewStrategy.execute(units, run, budget)` | **不改签名**；不负责 ruff | 若把 STAT 塞进 MultiRole，SinglePass 会漏 |
| 流水线 | `FindingPipeline`：schema → location → evidence → 聚类 → 去重 → 来源合并 → 置信度门槛 | 静态候选走同一管道 | 来源一律 `LLM_GENERAL`/`LLM_ROLE`；fingerprint 一律 `rule_or_issue_key="model"`；`_verify_evidence` 只认 `DIFF_LINE` 对新增行文本 |
| 领域 | `FindingSourceKind.STATIC_ANALYZER`、`EvidenceKind.STATIC_RESULT`、`FindingSource.analyzer_id` | 直接用 | `FindingCandidate` 无 `source_kind` / `rule_id`；`ReviewTaskKind` 无静态任务 |
| 快照 | `GitSnapshotProvider`（V2-B） | 读 **changed Python 文件** 的 head blob，供 ruff 分析完整文件 | 无 snapshot 时不能从 hunk 拼出可靠全文 |
| Git | `LocalGitSnapshotProvider` / Fake snapshot | 本地与测试路径可用 | GitHub blob API **仍非本里程碑**（V2-B 非目标保持） |
| 配置 | `ReviewConfig` extra=forbid | 增加 `static:` 子配置（`09` 已有草图） | Settings 尚无该字段 |
| 依赖 | `pyproject.toml` 已有 `ruff>=0.8` | `python -m ruff` 作为运行时分析器 | 生产路径不能假定「开发者碰巧装了 ruff」却静默跳过 |
| 评测 | `v2a_compare` / `v2b_compare` | 新增静态 off vs on；V2-A/B runner **钉死 static.enabled=false** | 无转换/去重标注集 |
| 安全 | `is_safe_repo_path`、`wrap_untrusted`、location 校验 | 分析器 JSON 的 path/code 视为不可信 | 无 subprocess 超时/取消契约 |

**明确不复用为 V2-C 生产路径**：Judge（V2-D）、反馈记忆（V2-E）、Agent `run_static_check` 工具（V3）、把 L3 符号当 Finding 来源。

---

## 2. 产品目标与非目标

### 2.1 目标（FR-17 / `07` §4 / 交接 V2-C）

1. 首发分析器 **ruff**，规则子集 **B（bugbear）+ S（bandit 移植）**；不单独引入 bandit。
2. 分析器输出 → `FindingCandidate`（`source_kind=static_analyzer`，`rule_id` 程序盖戳）→ 统一 Pipeline。
3. 程序事实（canonical 位置、fingerprint、`sources`、`verified`）模型不能写、不能改。
4. 与 LLM 候选重复时 **来源合并 + 证据合并**，不发两条评论。
5. 路径/行号校验与模型同权：幻觉路径 suppressed；不在新增行 → 不作为行内 Finding。
6. 失败可观测：缺能力、超时、非零退出、JSON 无法解析 → warning + coverage，**禁止静默当没跑过**。
7. 评测证明 **转换/去重正确**（主指标），不是 Fake LLM 的 Finding F1。
8. V1 / V2-A / V2-B 全量回归；既有对照脚本不被静态 Finding 污染。

### 2.2 非目标

| 不做 | 归属 | 理由 |
|------|------|------|
| Judge `keep/downrank` | V2-D | 本切片只做确定性融合 |
| 语义去重增强、跨 trigger 乱合并 | V2-D | 见 DP-8 残差 |
| 反馈记忆 / watermark | V2-E | 发布保持 V1-E |
| Agent / 只读工具 / 模型按需再跑 ruff | V3 | 静态分析是程序步骤，不是工具循环 |
| 第二个分析器（bandit/mypy/semgrep） | 后续 | ADR-007 + OQ-5：先 ruff |
| 把诊断塞进 L2/L3 让模型复述 | — | 交接：程序事实不让模型篡改 |
| GitHub REST blob API | 后置 | 与 V2-B 一致；无 snapshot 则 capability miss |
| 分析整个未改文件宇宙 / 全仓 `ruff check .` | — | 只分析本 run 保留的 changed Python 文件 |
| 改 `execute` 签名 / 复制 Service | — | `12` §7 |
| 用 ruff 替代 LLM 审查 | — | 融合，不是替换 |

### 2.3 与架构原文的显式差异

| 架构原文 | V2-C 裁决 | 处理 |
|----------|-----------|------|
| `04` §3 STAT 画在 MultiRole 内部 par | STAT 在 **ReviewService REVIEW**，与 `execute` 并行 | 与 V2-B「共享能力放 Service」同一原则；SinglePass 也能融合 |
| `04` Barrier 产出含 static candidates | Service 拼接 `strategy.candidates + static.candidates` 再进 Pipeline | Barrier 仍只属于 MultiRole 的角色聚合 |
| `07` §5 流水线含 Judge | V2-C 不启用 Judge | 置信度门槛保持 V1 |
| `09` `static.enabled: false` | **默认 False** | 对照与生产需显式打开；评测通过后再考虑默认 True |
| `12` ADR-007「待评测」 | 本里程碑 **选定 ruff、不选 bandit**；子集误报用 denylist 后置 | 关闭 OQ-5 的分析器选型半句；角色子集仍属 V2-A |
| `11` V2 DoD 含 Judge/反馈 | 不属于 V2-C | 本切片验收转换/去重 |
| `03` 目录 `review/static/` | 采用；适配器放这里，不进 `context.py` / `pipeline.py` 大文件 | Pipeline 只加来源/指纹/证据分支 |

---

## 3. 领域模型

### 3.1 扩展（domain，无 IO）

`FindingCandidate` 增加 **仅程序写入** 的字段：

| 字段 | 类型 | 含义 |
|------|------|------|
| `source_kind` | `FindingSourceKind \| None` | `None` → 沿用现逻辑（有 `role_id` 则 `LLM_ROLE` 否则 `LLM_GENERAL`）；静态转换器写 `STATIC_ANALYZER` |
| `rule_id` | `str \| None` | 静态：`ruff:B006` 这类稳定键；LLM：保持 `None`（指纹仍用 `"model"`） |
| `analyzer_id` | `str \| None` | 静态：`"ruff"` |

LLM 结构化输出 schema **不得**包含上述三字段。解析模型 JSON 之后由 Strategy 只盖 `role_id`（已有）；若模型夹带同名键，必须在 Strategy 边界显式丢弃，禁止依赖 Pydantic 默认行为。

实现约束：

- LLM 路径使用的 response schema 必须从「模型可写字段白名单」生成，不包含 `source_kind` / `rule_id` / `analyzer_id`。
- SinglePass / MultiRole 返回候选前必须做一次 `sanitize_llm_candidate()`：强制 `source_kind=None`、`rule_id=None`、`analyzer_id=None`，只允许补 `role_id`。
- StaticAnalyzer 转换器是 V2-C 内唯一允许写入 `source_kind=STATIC_ANALYZER` 的代码路径。
- Pipeline 仍需防御：若候选声明 `STATIC_ANALYZER` 但缺少合法 `analyzer_id` / `rule_id`，或带有 `role_id`，一律降级为 LLM 来源并丢弃 `STATIC_RESULT` 证据。

`ReviewTaskKind` 增加 `STATIC_ANALYZE = "static_analyze"`。

分析器内部结果（可放 `review/static/` 的 dataclass，不必进 domain）：

```text
AnalyzerDiagnostic
  analyzer_id: str          # ruff
  rule_id: str              # B006
  path: str                 # 仓库相对路径
  start_line: int
  end_line: int | None
  message: str
  severity_hint: str | None # ruff 的 severity，仅提示

AnalyzerRunResult
  candidates: list[FindingCandidate]
  diagnostics: list[AnalyzerDiagnostic]
  tasks: list[ReviewTask]
  coverage_items: list[CoverageItem]
  warnings: list[str]
```

`FindingSource` 已有 `analyzer_id` / `verified_by="program"`，不改形状。

### 3.2 不新增

不新增第二套 Finding 类型、不新增 Pipeline 所有者、不把 ruff JSON 当 Evidence 原文直接发布。

---

## 4. 放置：Service vs Strategy

```text
CONTEXT          装配 units（可含 L3）；可能已有 HeadSnapshot
REVIEW           asyncio.gather(
                   strategy.execute(units, run, budget),
                   static_runner.run(changed_files, snapshot, settings),
                 )
                 取消时两边一起取消；CancelledError 不吞
PIPELINE         process(strategy.candidates + static.candidates)
```

- 静态分析 **不消耗** `GlobalBudget` 的 LLM token/cost（不是模型调用）。
- 静态分析有自己的 **墙钟超时**（`review.static.timeout_seconds`）。
- `execute` 失败 vs 静态失败独立：静态失败 fail-soft（warning + coverage），不把已成功的 LLM 候选扔掉；`execute` 的 required 失败仍按 V2-A `StrategyHealth` 处理。
- 关闭 `static.enabled` 时：不创建 static task，不创建 subprocess，不创建 static coroutine，零静态候选；REVIEW 行为与今日字节级兼容（不计常规时间戳/耗时差异）。

---

## 5. 分析器协议与 ruff 子集

### 5.1 协议

```text
StaticAnalyzer
  id: str
  async analyze(files, blobs, *, timeout_s) -> AnalyzerRunResult
```

- `files`：本 run 过滤后保留的 `ChangedFile`。
- `blobs`：`path → head 正文`；只含将要分析的文件。
- 实现：`RuffAnalyzer`（生产）+ `FakeStaticAnalyzer`（单测/对照）。

### 5.2 输入范围

只分析同时满足：

1. 在 `filtered.kept` 内；
2. `language == python`（或 path 以 `.py` 结尾，与 V1 语言门一致）；
3. 非 binary / 非 generated / 非 deleted；
4. blob 可读。

无 `GitSnapshotProvider` 或 blob 缺失：**capability miss**（复用 V2-B 披露风格）+ `truncated=true`，该文件不跑 ruff，不伪造全文。

### 5.3 ruff 调用（生产）

- 入口：`sys.executable -m ruff check <repo-relative-files...>`（与当前 venv 绑定，避免 PATH 上另一份 ruff）。
- **`--isolated`**：不读取被审仓库的 `pyproject.toml` / `ruff.toml`，避免 PR 用配置关掉 S 规则，也避免误用本仓库自己的 lint 配置。
- `--output-format json --select` 钉死允许前缀（默认 `B,S`）。
- 工作目录：仅含 changed 文件的临时目录；写入前 `is_safe_repo_path`；禁止 `..`。临时目录内保持仓库相对路径结构，例如 `src/app.py` 写到 `<tmp>/src/app.py`。
- 输出路径映射：ruff JSON 的 `filename` 必须解析为临时目录内相对路径，再映射回仓库相对路径；若输出绝对路径、目录穿越、或不在本次 `filtered.kept` 集合内，丢弃并记 coverage，不信任 analyzer 输出路径。
- 超时：`asyncio.wait_for` / subprocess timeout；超时记 coverage + warning。
- 退出码：`0` = 无诊断成功；`1` = 有诊断成功；`>=2` 或进程无法启动 = analyzer 失败。若 stdout 不是合法 JSON，即使退出码为 0/1 也按 analyzer 失败处理。
- Windows：短路径临时目录；不依赖 shell；`CancelledError` 杀掉子进程。

### 5.4 规则子集

| 项 | 值 |
|----|-----|
| 允许前缀 | `B`, `S`（配置 `review.static.rule_subsets`） |
| 默认 denylist | 空列表；评测后可加码 | 
| 映射 | `S*` → `FindingCategory.SECURITY`；`B*` → `FindingCategory.CORRECTNESS`（枚举已有这两项，实现时钉死，不经模型重判） |
| 严重度 | 程序表：安全 S 默认 high；B 默认 medium；未知码 medium。**不**采用模型重判 |
| 置信度 | 位置校验通过后 `1.0`（程序来源）；仍走 Pipeline 门槛 |

本里程碑 **不**根据仓库 ruff 配置扩规则。

---

## 6. 诊断 → Candidate（转换契约）

转换器是纯函数，单测不启 subprocess。

1. `path` 必须安全且 ∈ `file_map`；否则丢弃并记 coverage（不可信路径）。
2. 行号必须落在该文件 **新增行**（可先用 `added_line_numbers` 预过滤）。不在新增行的诊断：**不当 Finding**，可记一条 skipped/truncated coverage，避免 `body_only` 噪声淹没正文。
3. `claimed_path/start/end` = 诊断位置；`title` = `ruff {rule_id}`；`explanation` = message（截断上限，例如 500 字）；`trigger_condition` = `rule_id`（稳定，供同码去重）。
4. `evidence` = 一条 `EvidenceKind.STATIC_RESULT`（location=`path:line`，content=message + 程序读到的新增行文本若有）。`verified` 由 Pipeline 重判，转换器写 `False`。
5. `source_kind=STATIC_ANALYZER`，`analyzer_id=ruff`，`rule_id=ruff:B006`，`role_id=None`。

删除文件、rename 的 old path：只分析 **new path** 的 head blob。

---

## 7. Pipeline 扩展（最小必要，不重写）

### 7.1 来源盖戳

`_validate_and_locate` 建 `FindingSource` 时：

```text
kind = cand.source_kind or (LLM_ROLE if cand.role_id else LLM_GENERAL)
analyzer_id = cand.analyzer_id if kind is STATIC_ANALYZER else None
```

禁止 LLM 候选带 `STATIC_ANALYZER`。若出现，降为 `LLM_GENERAL` 并丢掉 `STATIC_RESULT` 证据（防模型冒充分析器）。

### 7.2 证据重判

现逻辑：先把所有 `verified` 清零，再对 `DIFF_LINE` 比对新增行原文。

V2-C 增加：

- `STATIC_RESULT` **且** Finding 来源含 `STATIC_ANALYZER`：canonical 行是新增行 → `verified=True`（程序跑过分析器 + 位置已确认）。内容不要求等于源码行。
- 其它来源提交的 `STATIC_RESULT`：保持 `verified=False`（或直接丢弃）。
- 静态候选在 location_valid 后若没有任何 verified 证据：可用新增行补一条 `DIFF_LINE`（现有补证逻辑已覆盖）。

### 7.3 指纹

单条候选的规则键：

```text
rule_or_issue_key = cand.rule_id or "model"
```

聚类/融合后的规则键必须按合并结果重算，不能简单取「保留文案的 Finding」：

- 纯 LLM cluster：`model`。
- 纯静态 cluster：该静态 `rule_id`。
- LLM + 静态融合：若只含一个静态 `rule_id`，使用该静态 `rule_id`；即使 DP-8b 选择保留 LLM 文案，fingerprint / cross_run_match_key 仍用静态规则键。
- 不同静态 `rule_id` 不进入同一融合 cluster，因此不会出现多静态 rule key 需要二选一的情况。

同文件同行同 category：

| 左 | 右 | 结果 |
|----|----|------|
| 两条 `ruff:B006` | 去重为 1 | 转换/去重 DoD |
| `ruff:B006` 与 `ruff:S110` | **两条** Finding | 不同规则 |
| LLM（key=`model`）与 `ruff:B006` 同行同 category | **融合为 1**，fingerprint key=`ruff:B006`（见 §7.4） | `07` §4 不双计，且跨 run 稳定 |

### 7.4 跨来源融合（V2-C 核心）

现有聚类键含 **归一化 trigger**，LLM 文案与 `ruff:B006` 不会自然聚在一起。V2-C 在现有 `_cluster` **之后**增加一步确定性融合：

- 仅当一对 Finding 满足：同一 `canonical_path`、行区间重叠（沿用 `_CLUSTER_LINE_TOLERANCE`）、同一 `category`、且一方 `STATIC_ANALYZER`、另一方 `LLM_*`。
- 保留置信度较高者（静态 1.0 通常赢，若 LLM 同为 1.0 则保留 LLM 文案、吸收静态来源与证据——**DP-8b**：并列时保留 LLM 的 title/explanation，静态进 `sources` + `evidence`，但 fingerprint key 仍用静态 `rule_id`，避免评论变成「ruff B006」一行码，同时保留跨 run 稳定性）。
- 不同 `rule_id` 的两条静态 Finding **不**因同行而融合。
- 两条 LLM Finding 仍只走现有 trigger 聚类（V2-A 行为不变）。

残差（相邻行不同缺陷被跨来源融掉）交给 V2-D Judge，本里程碑用评测样本钉住「应融 / 不应融」各若干条，不在 V2-C 上语义模型。

### 7.5 门槛

静态 Finding `confidence=1.0` ≥ `min_confidence` → accepted（若 location/evidence 通过）。关闭静态不影响 LLM 门槛。

---

## 8. 配置

`ReviewConfig` 增加（`extra=forbid`）：

```yaml
review:
  static:
    enabled: false              # DP-4：默认关
    analyzers: [ruff]           # 本里程碑只实现 ruff；其它名 → 配置错误
    rule_subsets: [B, S]
    timeout_seconds: 30
    denylist: []                # 例如 ["S101"] 后置用
```

- V2-A / V2-B compare runner **强制** `static.enabled=false`。
- V2-C compare / 转换评测 **强制** `true`。
- 未知 `analyzers` 项：配置错误，preflight 失败；这是用户配置错误，不是运行时能力缺失。
- 已知 analyzer（ruff）但运行时缺包/无法启动：capability miss + warning + coverage truncated，不当成「零诊断」。

---

## 9. 安全、超时、取消、降级

| 主题 | 契约 |
|------|------|
| 不可信 | ruff JSON 的 path/message 是数据；path 必须通过 `is_safe_repo_path` 且属于本 run 变更文件 |
| 不读 PR 分支配置来开关分析器 | 与 Settings 铁律一致；`--isolated` 同时忽略目标仓 lint 配置 |
| 临时目录 | 进程结束删除；只写 changed 文件正文；限制单文件大小（复用过滤阈值） |
| 超时 | 超限：该分析器失败，LLM 候选仍进入 Pipeline |
| 取消 | `CancelledError` 传播；杀掉 ruff 子进程 |
| 缺 ruff 包 | capability miss + truncated；**禁止**当成「零问题」 |
| 日志 | 可记 `rule_id`、path、exit code、耗时；禁止把整份源码和完整 JSON dump 进默认日志 |
| 隐私 | 评测默认 `retain_source_in_evals: false` 仍剥离源码 |

---

## 10. 任务、覆盖、阶段、日志

- 每个分析器每次 run 一条 `ReviewTask(kind=STATIC_ANALYZE, status=completed|failed|cancelled)`。
- Coverage：分析过的文件 `COVERED`；跳过的文件写清原因（no snapshot / not python / timeout / cap）。
- REVIEW stage `detail` JSON 增加 `static_candidates`、`static_diagnostics`、`static_status`。
- 结构化日志 event：`static_analyze`（analyzer_id、n_diag、n_candidates、elapsed_ms）。
- 不新增 SQLite 表；task/coverage/finding.sources 够用。

---

## 11. 测试与评测

### 11.1 必须单测（Fake 分析器，不强制每条都起 ruff 进程）

- 转换：合法诊断 → candidate 字段与 source 盖戳。
- 转换：`../` path、未知 path、非新增行 → 无 Finding。
- Pipeline：两条相同 `ruff:B006` → 一条 Finding、一个 fingerprint。
- Pipeline：同行 `B006` 与 `S110` → 两条。
- Pipeline：LLM + 静态同行同 category → 一条，`sources` 含两种 kind，证据含 `STATIC_RESULT`。
- Strategy：LLM 候选若夹带 `source_kind/rule_id/analyzer_id` → 边界清洗后归零。
- Pipeline：模型候选若伪造 `source_kind=static_analyzer` → 不能变成程序静态来源。
- Service：`enabled=false` 零静态候选；无 snapshot 时 warning + `truncated`。
- 取消：静态任务可被取消且不留下孤儿进程（至少 Fake/超时路径）。

### 11.2 一条真实 ruff 集成（可选但建议）

用含 B006（可变默认参数）的最小 Python 文件 + 本地 snapshot，断言至少一条 `rule_id` 含 `B006` 的 accepted/body 路径符合 §6。跳过条件：环境不能 `python -m ruff` 则 skip 并在报告写明，**不能**在生产路径同样 skip。

### 11.3 对照评测 `v2c_compare`

主表不是 Fake LLM 的 P/R/F1，而是：

| 表 | 内容 |
|----|------|
| 转换 | 期望诊断码/路径/行 → 实际 candidate/Finding |
| 去重 | 重复诊断与 LLM 重叠样本的保留条数、sources 种类 |
| 成本 | 相对 V2-B：模型 calls / input tokens 应近似不变；墙钟允许 ruff 增量 |

V2-B compare 钉 `static.enabled=false`。披露 Fake LLM 限制，与 V2-A/B 同一口径。

---

## 12. 分步实施任务卡（审查通过后才编码）

| 卡 | 内容 | 验收 | 依赖 |
|----|------|------|------|
| T0 | 本对齐稿审查；DP 默认生效 | 用户确认或默认 | — |
| T1 | `FindingCandidate` 程序字段；`ReviewTaskKind`；Settings `static` | 旧 LLM JSON 仍可解析；模型不能盖 source | T0 |
| T2 | `StaticAnalyzer` 协议 + Fake + 转换器纯函数 | §11.1 转换测 | T1 |
| T3 | Pipeline：来源盖戳、STATIC_RESULT 重判、fingerprint `rule_id`、跨来源融合；LLM source 字段防伪 | 去重/融合测 | T1 |
| T4 | `RuffAnalyzer` subprocess + isolated + 超时/取消 | 集成测或 skip 标明 | T2 |
| T5 | Service REVIEW gather；coverage/task/日志；无 snapshot miss | capability miss 测 | T2 T3 T4 |
| T6 | `v2c` 转换/去重数据集 + compare；钉死 V2-A/B 关静态 | `docs/evidence/v2-c-compare.md` | T5 |
| T7 | 全量回归、Ruff、mypy、`git diff --check` | CI | T6 |

**T0 完成前禁止改 Pipeline / Service。** 不要先接 subprocess 再补融合语义。

目录（建议）：

```text
reposage/review/static/__init__.py
reposage/review/static/protocol.py
reposage/review/static/convert.py
reposage/review/static/ruff.py
reposage/evals/datasets/v2c_static.yaml
reposage/evals/v2c_compare.py
```

---

## 13. DoD 与审查清单

### 13.1 交接 DoD

| # | 条目 | 落点 |
|---|------|------|
| 1 | 首发 Ruff 子集 | T4 + 配置 |
| 2 | Analyzer → Candidate/Source；程序事实模型不能改 | T1 T2 T3 |
| 3 | 与 LLM 进入同一融合管道 | T3 T5 |
| 4 | 转换/去重正确 | T6 |
| 5 | V1/V2-A/V2-B 回归 | T7；对照关静态 |

### 13.2 架构自查

- [ ] 未改 `execute` 主签名
- [ ] 未引入 Agent / 工具 / Judge / 反馈记忆
- [ ] STAT 在 Service，不在 MultiRole 独占
- [ ] 分析器 JSON 当不可信数据
- [ ] 默认 dry-run；不提交、不 push
- [ ] `static.enabled=false` 时无静态 Finding
- [ ] 日志无密钥、无整文件源码

### 13.3 实现期测试清单

- Fake 诊断转换与拒绝
- 同码去重 / 异码并存 / LLM+静态融合
- 模型冒充 static_analyzer
- 开关关闭行为兼容
- 无 snapshot capability miss + truncated
- ruff 有诊断时 exit 1 不算 runner 崩溃
- V2-A/B compare 仍关静态

---

## 14. 决策点（未遭反对则按此实现）

| ID | 结论 | 理由 |
|----|------|------|
| **DP-1** | STAT 在 ReviewService REVIEW，与 `execute` 并行 | SinglePass 与 MultiRole 共享；不改 execute |
| **DP-2** | 分析器不是角色；`ReviewTaskKind.STATIC_ANALYZE` | ADR-004 |
| **DP-3** | 只做 ruff；B+S 前缀；不引入 bandit | 关闭 OQ-5 分析器选型；`07` §4 |
| **DP-4** | `static.enabled` 默认 **False** | 与 `09` 一致；Finding 会进评论流，先评测再考虑默认开 |
| **DP-5** | 无 snapshot 则 skip + capability miss，不从 hunk 拼文件 | 避免残缺文件导致假阴性/假阳性 |
| **DP-6** | 只把 **新增行** 上的诊断变成 Finding | PR 审查范围；旧债不刷屏 |
| **DP-7** | `source_kind`/`rule_id` 仅程序盖戳；LLM schema 不含它们；Strategy 边界必须清洗同名字段 | 交接：程序事实不可篡改 |
| **DP-8** | 跨来源融合：同 path+category+行重叠，静态与 LLM 合一条 | `07` §4 不双计 |
| **DP-8b** | 融合并列时保留 LLM 文案，静态进 sources/evidence | 评论可读；静态仍可追溯 |
| **DP-9** | fingerprint 使用 `rule_id or "model"`；LLM+静态融合时优先使用静态 `rule_id` | 同码去重；异码不并；融合后跨 run 稳定 |
| **DP-10** | ruff `--isolated` + 只写 changed 文件到临时目录 | 不被目标仓配置劫持 |
| **DP-11** | 静态不占 LLM 预算；独立超时 | 预算模型仍只描述模型调用 |
| **DP-12** | 主评测是转换/去重正确性；Finding F1 只披露 | 对标 V2-B 检索命中率，不假装模型变强 |
| **DP-13** | 不把 ruff 输出喂给模型 | 防篡改 |
| **DP-14** | 不做 GitHub blob API | 与 V2-B 非目标一致 |
| **DP-15** | 本阶段不改 Publishing | 融合后的 Finding 走现有发布规则 |

若需推翻某条，只改本节与对应章节。

---

## 15. 冻结声明

设计冻结已解除（用户授权实现）。仍禁止：

1. 不引入 Judge / Agent / bandit / GitHub blob API。
2. 不提交、不 push、不打 tag（除非用户明确要求）。
3. V2-A / V2-B compare 必须继续钉死 `static.enabled=false`。

---

## 16. Codex 审查修订登记

本节记录 Codex 对第一版 V2-C 对齐稿的直接修订，修订后本稿可作为实现输入。

| 编号 | 修订 | 位置 |
|------|------|------|
| R1 | 明确 LLM schema 不包含 `source_kind/rule_id/analyzer_id`，且 Strategy 边界必须清洗同名字段；Pipeline 仍做防伪兜底 | §3.1、§7.1、§11.1、DP-7 |
| R2 | `AnalyzerRunResult` 增加 `candidates`，避免 runner 结果缺少交给 Pipeline 的候选列表 | §3.1 |
| R3 | ruff 临时目录保持仓库相对路径结构，ruff JSON filename 必须安全映射回 repo path | §5.3、§9 |
| R4 | ruff 退出码口径固定：0/1 是成功语义，>=2 或 JSON 非法才是 analyzer 失败 | §5.3、§13.3 |
| R5 | unknown analyzer 是配置错误；已知 ruff 缺包/无法启动才是 capability miss | §8、§9 |
| R6 | LLM+静态融合时，即使保留 LLM 文案，fingerprint/cross_run_match_key 仍优先使用静态 `rule_id` | §7.3、§7.4、DP-9 |
