# V2-B 设计对齐（符号预检索 → L3）

> 状态：V2-B **ACCEPTED**（Round 3：`docs/evidence/v2-b-implementation-review-round3.md`；P2 覆盖聚合已修）
> 前置：V2-A **ACCEPTED**（`docs/evidence/v2-a-final-acceptance.md`）
> 契约来源：`01` FR-16、`03` §2 context、`04` §1、`05` ContextChunk/EvidenceKind、`06` §1–§3/§6–§8、`09` §7、`10` §3–§4、`11` §5–§7、`12` V2-b / 风险表、交接文档 `docs/evidence/cursor-handoff-v1-complete-v2-v3-next.md` §7 V2-B
> DoD 摘要：**从 changed hunk 抽出修改符号；确定性检索定义 / 直接 import / 相关测试；写入 L3 且带来源标记；缓存键绑定 repo+SHA+path+content hash；未命中不增加噪声 Token；评测证明跨函数/跨文件命中，而不只是上下文变长；V1/V2-A 回归通过**

---

## 0. 一句话与边界

V2-B 在 **CONTEXT 阶段** 给每个 `ReviewUnit` 装配确定性最小 L3：当前 diff 改到的符号的定义、其直接 import 目标、以及按文件名启发式找到的相关测试片段。L3 是程序检索，不是模型调用，也不是 V3 工具循环。

`ReviewStrategy.execute` 主签名不变。`FindingPipeline`、Publishing、默认 `single_pass` 行为不变。SinglePass 与 MultiRole **共用** 同一批带 L3 的 unit。

**本里程碑禁止**：Agent / 只读工具、ruff、Judge、反馈记忆、embedding、把 L3 当 Finding 来源、为 L3 复制一套主流程。

---

## 1. 现有可复用组件盘点

| 层 | 组件 | V2-B 用法 | 缺口 |
|---|---|---|---|
| 编排 | `ReviewService` CONTEXT：`build_file_units` | 装配前准备 head 快照/blob；失败降级为空 L3，不让 CONTEXT 整段失败 | 只把 `ChangedFile`（diff）交给 assembler，**读不到未改文件正文** |
| 策略 | `ReviewStrategy.execute(units, run, budget)` | **不改签名**；L3 已在 unit.context.chunks 里 | 无 |
| 上下文 | `ContextAssembler` + `ContextBudget` + `unit_to_messages` | 插入 L3 块；未用满的 L3 预算还给 L2 | 现实现把 L3 的 25% 并进 reserve；`unit_to_messages` 只组装 L0/L1/L2/L4 |
| 配置 | `context.symbol_retrieval`（默认 False）、`related_code_ratio=0.25` | 作为总开关与名义预算 | assembler **未读** 这两项；`ContextBudget` 无 `l3_ratio` |
| Git | `GitProvider.get_changes` / `get_diff`；`FakeGitProvider` 内存快照 | 快照已含未改文件，可做 blob 源 | Protocol **没有** `get_blob` / `list_paths` |
| 领域 | `ContextLayer.L3`、`ContextSourceKind.SYMBOL`/`FILE`、`EvidenceKind.SYMBOL_*` | 直接用 | 无 `SymbolDef`/`SymbolRef`/`SymbolIndex` 模型 |
| 存储 | SQLite run 记忆 | V2-B **不** 把源码正文写入 DB | 无符号表；本里程碑用 run 内内存缓存即可 |
| 评测 | Fake Git/LLM、`v2a_compare` | 新增 L3-off vs L3-on；V2-A 对照脚本钉死 `symbol_retrieval=false` | 无跨文件/跨函数标注集 |
| 安全 | `wrap_untrusted`、路径校验 | L3 来自 PR head，按不可信包装 | 新读路径必须限制在 git 列出的 path |

**明确不复用为 V2-B 生产路径**：V2-C ruff、V2-D Judge、V2-E 反馈、V3 `find_references` 工具。

---

## 2. 产品目标与非目标

### 2.1 目标（FR-16 / `06` 最小确定性 L3）

1. 从每个 changed file 的 **新文件侧 hunk**（context+added）识别**修改符号**（包围 added 行的函数/类；顶层 added 则取模块级名字）。
2. 预检索并写入 L3（按优先级，见 §5）：
   - 该符号在 **head** 上的定义（可跨文件）；
   - **直接 import** 目标的定义（只解析本快照内路径，不联网）；
   - **相关测试** 中含该名字的短片段（文件名启发式，有上限）。
3. 每个 L3 块有 `source.kind + ref + sha`；超预算裁剪时 `truncated=true` 且正文含 `[TRUNCATED: …]`。
4. 同一符号多定义时全部列出并写 `notes`（证据冲突），禁止静默只留一个。
5. 缓存键：`repo + sha + path + content_hash + extractor_id`；blob 变则失效。
6. 评测：检索命中率（程序指标）+ Token 增量；用跨文件样本证明 L3 不是纯堆 Token。
7. V1 / V2-A 全量回归；V2-A 对照数字不被 L3 污染。

### 2.2 非目标

| 不做 | 归属 | 理由 |
|------|------|------|
| 模型按需 `read_file` / `find_references` | V3-A/B | 角色 ≠ Agent；L3 必须确定性 |
| 本里程碑强制 Tree-sitter 原生依赖 | 后置 | `12` 允许正则过渡；Windows 原生轮子是风险 |
| 全仓 FTS5 / embedding | `06` §6 信号未到 | 名称索引内存即可 |
| 用 L3 在 Pipeline 里做 evidence 校验 | V2-D / 后续 | 本阶段 Finding 仍按 diff 行验证 |
| 反馈记忆 L4 | V2-E | 内置规则保持 V1 |
| 多语言抽取 | 后续 | 与 V1 一致，首发 Python |
| 改 `execute` 签名 / 复制 Service | — | `12` §7 |

### 2.3 与架构原文的显式差异

| 架构原文 | V2-B 裁决 | 处理 |
|----------|-----------|------|
| `04` §1「版本差异只在 execute 内」 | L3 属于 CONTEXT，所有 Strategy 共享 | 与 V2-A 把 GateFeatures 盖在 CONTEXT 同一原则；**不**把检索藏进 MultiRole |
| `12` V2-b 标题写 Tree-sitter | 抽 **SymbolExtractor 协议**；默认 stdlib（`ast` + `tokenize` 回退） | Tree-sitter 作为可选实现，不阻塞 DoD |
| `06` 缓存进 SQLite 符号表 | 本里程碑 **run 内内存缓存** | 不把源码/符号正文落库；SQLite 表可后置 |
| `06` L3 25% 固定切块 | 名义 25% 上限；**实际未用部分还给 L2** | L3 关闭或未命中时，L2 预算与 V1 相同 |
| `11` V2 DoD 含 Judge/反馈 | 不属于 V2-B | 本切片只验收 L3 |

---

## 3. 领域模型

### 3.1 新增（domain，无 IO）

```text
SymbolKind    = function | class | method | module
SymbolDef     = { name, qualname, kind, path, start_line, end_line, signature }
SymbolRef     = { name, path, line, enclosing }          # 可选，检索排序用
ModifiedSymbol = { name, kind, path, new_line }          # 由 diff 行映射
L3Hit         = { def: SymbolDef, reason: definition|import|test, notes: str | None }
```

`matched_features` 式约束：**L3 块 content 可以含代码（审查需要），但日志/评测报告不得dump 密钥字面量**；报告只写 path/qualname/行号。

### 3.2 已有字段怎么用

| 对象 | 用法 |
|------|------|
| `ContextChunk.layer = L3` | 相关代码 |
| `source.kind = SYMBOL` 或 `FILE` | 定义用 `symbol:<path>:<qualname>`；测试片段可用 `file:<path>#L<n>` |
| `source.sha` | head blob hash（content hash 即可，不强制 git blob id） |
| `ReviewUnit.context.chunks` | L0, L1, L2…, L3…, L4；顺序稳定便于测试 |
| `CoverageItem` | 某符号因预算被裁：`reason=truncated`，`target=symbol:<qualname>`，`stage=CONTEXT` |

不新增 ReviewTask kind。L3 不是模型调用，不进 `tasks`/`usages`。

### 3.3 配置

沿用并接上（现已有字段）：

| 键 | V2-B 语义 |
|----|-----------|
| `context.symbol_retrieval` | 总开关。默认 **True**（DP-5）；`False` 时 assembler 行为与 V1 字节级兼容（无 L3 块） |
| `context.related_code_ratio` | L3 名义占比，默认 0.25（相对 **input_limit**） |
| `context.diff_ratio` / `rules_ratio` / `reserve_ratio` | 保持；assembler 的 `ContextBudget` 必须与 Settings 对齐 |

V2-A 对照 runner **显式** `symbol_retrieval=false`，避免历史三表被 L3 Token 污染。

---

## 4. 与骨架的关系

```text
preflight → fetch → parse/filter
  → 构建 HeadSnapshot（get_blob / list_paths）
  → ContextAssembler.build_file_units(..., snapshot=)
       extract modified symbols from ChangedFile
       retrieve L3 hits (cached)
       pack chunks under token cap
  → strategy.execute(units, run, budget)     # 签名不变
  → FindingPipeline → publish
```

- `ContextAssembler.build_file_units` 增加可选 `snapshot: HeadSnapshot | None`。`None` 或开关关闭 ⇒ 无 L3。
- Service / EvalRunner / `v2a_compare` 在开关打开时传入 snapshot。
- **禁止** Strategy 内部再读仓、再 parse AST。

---

## 5. 修改符号与最小 L3 内容

### 5.1 修改符号（程序，不靠模型）

对每个 hunk 使用与 Gate 相同的 **新文件侧视图**（context+added，忽略 deleted）：

1. 用 head 文件全文建该 path 的符号表（定义的行区间）。
2. 每个 **added** 行的 `new_ln`：落入哪个最内层 `SymbolDef`，该 def 即为修改符号。
3. 若 added 行在模块顶层（无包围函数/类）：若是 `def`/`class` 自身，则该新定义为修改符号；否则记 `module`。
4. 只 added 注释/docstring：**不**产生修改符号（与 Gate 一致，避免噪声 L3）。

### 5.2 检索集合（有上限，可测）

对每个修改符号，按序收集，**去重 (path, start_line, end_line)**：

| 优先级 | 命中 | 上限（单 file unit） |
|--------|------|----------------------|
| 1 定义 | 同文件 enclosing def 的完整源（已在 L2 的可跳过，避免重复） | 每个符号 1 段 |
| 2 跨文件同名定义 | 快照内其它 path 的同 `qualname` 或同 `name`（类.方法优先 qualname） | 每个名字 ≤2 个 path |
| 3 直接 import | 本文件 `import` / `from … import` 解析到快照 path 后，只取被引用名字的 def | 每文件 ≤8 个 import 目标 |
| 4 相关测试 | `test_*.py` / `*_test.py` / `tests/**` 中出现该 **name** 的窗口（命中行 ±N，N=15） | ≤3 个文件，每文件 1 窗 |

同文件定义已完整出现在当前 unit 的 L2 中 → **不**再复制进 L3（减少 Token）。跨 unit 分块时：只对「本 unit 的 L2 未覆盖的 enclosing def」补 L3。

### 5.3 冲突

同一 `qualname` 多个 path：全部纳入（受上限约束），chunk `notes` 或块首行写 `证据冲突: Foo 定义于 a.py 与 b.py`。模型被 L0 要求声明冲突而非猜测。

### 5.4 语言

`file.language != python`（或无法检测）：跳过抽取，L3 为空，不算失败。非 `.py` 测试文件不进入启发式。

---

## 6. 抽取器协议（可替换，默认无原生依赖）

```text
SymbolExtractor
  id: str                    # 进入缓存键，如 "python-ast-v1"
  extract(path, source) -> list[SymbolDef]
```

| 实现 | 何时 |
|------|------|
| `PythonAstExtractor` | `ast.parse` 成功 |
| 同一 id 的 **tokenize 回退** | 语法错误文件：用 `tokenize` 识别 `def`/`class` 行 + 缩进块，不求完美 |
| `TreeSitterPythonExtractor` | **可选 extra**，同一协议；本里程碑不作为 DoD 必项 |

`12` 风险表「正则过渡、Tree-sitter 后置」按此落地：默认 stdlib，行为可测，Windows 无原生编译。

禁止在 `review/` 里直接 `subprocess` 调 `git`；只通过 `GitProvider`。

---

## 7. 预算与裁剪

`ContextBudget` 与 `Settings.context` 对齐：

```text
input_limit = total_window - output_reserve(15%)
名义：L0 5% / L1 5% / L2 40% / L3 25% / L4 10% / 弹性 15%
```

装配顺序（与 `06` §2 一致，并保持 V1 硬约束）：

1. L0 不可裁；超 input_limit → `ContextBudgetError`。
2. L1 / L4 仍先裁到各自上限。
3. L2 **永不裁当前任务**；超则多 unit（现逻辑）。
4. L3 使用 `min(名义 L3, 装配完 L0/L1/L2/L4 后的剩余)`。
5. 剩余仍空：L3 为空；若有命中但塞不下 → 那些 hit 记 `CoverageItem.truncated`，块内或省略处写 `[TRUNCATED: …]`。
6. **L3 实际占用 < 名义时，差额保持在 L2 可用余额中**（先切 L2 再填 L3，或先算 L2 组再把剩余给 L3——实现选一种并用测试钉死）。推荐：**先按现逻辑分组 L2，再用每 unit 剩余填 L3**，这样 V1 在 L3 为空时 unit 切分不变。

裁剪 L3 时保留优先级 1→4；先丢测试窗，再丢额外同名定义，最后才丢 import 定义。

---

## 8. GitProvider 与缓存

### 8.1 Protocol 扩展

```text
async get_blob(sha: str, path: str) -> str | None
async list_paths(sha: str) -> list[str]
```

- `get_blob`：无路径 / 二进制 → `None`，不抛。
- `list_paths`：该 sha 下全部文件 path（Fake=快照 keys）。调用方再过滤 `*.py` 与测试启发式，避免把过滤逻辑放进每个 Provider。
- `FakeGitProvider`：已有 `add_snapshot`，直接实现。
- 测试里只实现了 `get_diff` 的桩：补默认方法或继续继承 Fake。

**不在本里程碑实现 GitHub REST blob API**（当前仓库也没有 github_api 生产实现）。本地 git 若尚未作为 Provider 落地，可后置；评测与单测走 Fake。

路径安全：`path` 规范化后拒绝 `..`、盘符、绝对路径（与 `ChangedFile.path` 校验一致）。只读取 `list_paths` 返回的集合。

### 8.2 HeadSnapshot

Service 在 CONTEXT 构造：

```text
HeadSnapshot
  sha: str
  blobs: dict[path, str]          # 惰性：先 list_paths，按需 get_blob
  content_hash(path) -> str       # sha256 of bytes
```

惰性策略：先取全部 changed `.py`；再解析 import 目标 path；再 list 测试 path 并只读命中文件。禁止一上来把整个仓库读进内存（大仓）。设硬顶：`max_l3_files_read`（默认 40）。

### 8.3 缓存

```text
key = extractor_id + sha + path + content_hash
value = list[SymbolDef]     # 结构，不是全文
```

作用域：单 run 内存。不写 SQLite，不写模型结果。内容 hash 变 → 失效。

---

## 9. Prompt 装配与安全

`unit_to_messages`：

- L3 与 L1/L2 一样走 **user** 侧，`wrap_untrusted`（PR head 代码不可信，`06` §5 / `10` §3）。
- 块前缀标明来源：`L3 symbol:src/util.py:Foo.bar sha=…`。
- L0 增加一句（仅开关打开时，避免无故改 V1 快照）：允许引用 L3 来源；禁止把 L3 当已验证证据；冲突时声明冲突。
- `symbol_retrieval=false` 时消息字节与今日 V1 兼容（无新 L0 句、无 L3 块）。

不把 L3 放进 system。不在 L3 里放治理指令。

---

## 10. 落库、日志、覆盖

| 项 | V2-B |
|----|------|
| tasks/usages | 不因 L3 增加行 |
| coverages | L3 裁剪 → CONTEXT + `truncated` |
| stages | CONTEXT `detail` 可含 `l3_hits=N l3_truncated=M files_read=K` |
| 日志 | `event=l3_retrieve`：path、qualname、reason、tokens；**无源码** |
| config 快照 | 已有 `snapshot_hash`；开关变化会使 run 配置哈希变 |

---

## 11. 失败与降级

| 场景 | 行为 |
|------|------|
| 开关关闭 / 无 snapshot | 无 L3，CONTEXT 成功 |
| `get_blob` 失败或 None | 该 path 跳过，继续 |
| `ast.parse` 失败 | tokenize 回退；仍失败则该文件无符号表 |
| 超 `max_l3_files_read` | 停止新读；已有 hit 照装；coverage truncated |
| 单文件抽取异常 | 记录 warning + 该文件空 L3，**不**让 run FAILED |
| 取消 | 与现 CONTEXT 相同，向上传 `CancelledError` |

L3 降级不得改变 Gate / 两阶段预算 / StrategyHealth 语义。

---

## 12. 评测

### 12.1 主指标：检索命中（程序，不靠模型）

数据集 `reposage/evals/datasets/v2b_l3.yaml`（≥12 条）：

| 类 | 期望 |
|----|------|
| 同文件内层函数 added，外层 def 已在 L2 | L3 **不**重复整段外层 |
| 调用未改模块中的 `helper` | L3 含 `helper` 定义 path |
| `from pkg.mod import Foo`，改动使用 Foo | L3 含 `pkg/mod.py` 中 Foo |
| 同名符号两个 path | 两处都出现或 notes 含冲突 |
| 仅 docstring/注释 added | L3 空或无该伪符号 |
| 语法不完整文件 | 不崩溃；能抽到的 def 仍可用 |
| 测试文件 `tests/test_foo.py` 引用名字 | L3 含测试窗（若预算够） |
| 非 Python | L3 空 |
| 预算极小 | truncated 覆盖；L2 仍完整 |

门槛（建议，审查可改）：

- 标注 `expected_symbols` 的样本：**hit rate ≥ 0.8**
- 负例（不应检索）：**false symbol rate ≤ 0.1**
- 相对 L3-off：跨文件真值样本的 L3 Token **> 0**；负例 Token 增量 ≈ 0

### 12.2 对照三表（L3-off vs L3-on）

同一 Fake、同一 `single_pass`（或同时跑 multi_role，但主表钉 single_pass 以免和 V2-A 角色成本缠在一起）：

- 质量：脚本化 Fake **不能**证明模型因 L3 更聪明；表上必须写清。可用「仅当 L3 含标记符号才吐跨文件 Finding」的 Fake 分支，证明 **管道吃到了 L3**，不宣称模型增益。
- 成本：input tokens 增量、`files_read`
- 延迟：墙钟（Fake）

输出：`docs/evidence/v2-b-compare.md` + `.json`。真实 API：本里程碑 **not_run**。

### 12.3 回归

全量 pytest / Ruff / mypy。`symbol_retrieval=false` 时 `test_context` 原断言保持。V2-A compare 钉死关闭 L3。

---

## 13. 分步实施任务卡（审查通过后才编码）

| 卡 | 内容 | 验收 | 依赖 |
|----|------|------|------|
| T0 | 本对齐稿审查；DP 默认生效 | 用户确认或默认 | — |
| T1 | 领域：`SymbolDef` 等；`GitProvider.get_blob`/`list_paths`；Fake 实现 | 单测；旧 Fake 子类不炸 | T0 |
| T2 | `PythonAstExtractor` + tokenize 回退；缓存键 | 语法坏文件不崩溃；hash 变则失效 | T1 |
| T3 | 修改符号映射（hunk 新文件侧 + added 行） | 与 §12.1 同文件/注释负例 | T2 |
| T4 | 检索：定义 / import / 测试窗 / 去重 / 冲突 notes | YAML 命中率门槛 | T3 |
| T5 | `ContextBudget` 接 Settings；L3 填剩余；空 L3 时 L2 切分不变 | `test_context` + 新预算测 | T4 |
| T6 | `unit_to_messages` 纳入 L3（UNTRUSTED）；开关关闭字节兼容 | 快照测 | T5 |
| T7 | Service CONTEXT 建 HeadSnapshot；惰性读 + 上限；coverage/日志 | 读文件数 ≤ cap | T1 T6 |
| T8 | `v2b_l3.yaml` runner + 命中率断言 | T4 门槛 | T4 |
| T9 | `v2b_compare` 三表；V2-A runner 钉 `symbol_retrieval=false` | `docs/evidence/v2-b-compare.md` | T7 T8 |
| T10 | 全量回归、Ruff、mypy、`git diff --check` | CI | T9 |

**T0 完成前禁止 T7。** 不要先改 Service 再补抽取器。

目录（建议）：

```text
reposage/review/symbols/extract.py      # Extractor 协议 + Python 实现
reposage/review/symbols/retrieve.py     # 修改符号 + L3 hits
reposage/review/symbols/snapshot.py     # HeadSnapshot
reposage/evals/datasets/v2b_l3.yaml
reposage/evals/v2b_compare.py
```

`context.py` 保持装配器，不把 AST 逻辑堆进去。

---

## 14. DoD 与审查清单

### 14.1 交接 DoD

| # | 条目 | 落点 |
|---|------|------|
| 1 | 符号索引可替换 | T2 协议 |
| 2 | 函数/类定义与引用向检索 | T3–T4（引用以 import + 名字匹配为 V2-B 范围，不做全仓 dataflow） |
| 3 | 缓存键绑定 repo/SHA/path/content | T2 |
| 4 | 评测证明跨文件命中而非只加 Token | T8–T9 |
| 5 | V1/V2-A 回归 | T10；V2-A 对照关 L3 |

### 14.2 架构自查

- [ ] 未改 `execute` 主签名
- [ ] 未引入 Agent / 工具 / ruff / Judge / 反馈记忆
- [ ] L3 在 CONTEXT，不在 Strategy 内读仓
- [ ] PR head 代码 UNTRUSTED
- [ ] 默认 dry-run；不提交、不 push
- [ ] 空 L3 时 L2 分块与 V1 一致
- [ ] 日志无源码/密钥

### 14.3 实现期测试清单

- Fake `get_blob` / 缺失 path
- ast 成功 vs 语法错误回退
- docstring added 不造符号
- import 解析到快照内 def
- 双定义冲突 notes
- L3 预算 0 → truncated，L2 仍在
- `symbol_retrieval=false` 消息与今日相同
- 惰性读取不超过 cap
- 命中率数据集门槛
- V2-A compare 仍可生成且关 L3

---

## 15. 决策点（未遭反对则按此实现）

| ID | 结论 | 理由 |
|----|------|------|
| **DP-1** | L3 在 CONTEXT，SinglePass 与 MultiRole 共用 | 检索与角色调度解耦；V3 仍要「最小确定性 L3」 |
| **DP-2** | 默认 stdlib 抽取器；Tree-sitter 可选后置 | 无原生依赖；协议已留替换点 |
| **DP-3** | `get_blob` + `list_paths` 进 GitProvider | 否则无法读未改文件 |
| **DP-4** | 未用 L3 预算还给 L2；空 L3 时 unit 切分不变 | V1 兼容 |
| **DP-5** | `symbol_retrieval` 默认 **True**；V2-A runner 强制 False | 能力默认可用，历史对照不被污染 |
| **DP-6** | L3 只进 user + UNTRUSTED | 与 L1/L2 同一不可信边界 |
| **DP-7** | 本阶段不做 Pipeline 符号 evidence 门 | 避免和 V2-D 缠在一起 |
| **DP-8** | 只做 run 内内存缓存 | 够用且不把源码落库 |
| **DP-9** | 仅 Python；其它语言空 L3 | 与 V1 语言范围一致 |
| **DP-10** | 主评测是检索命中率；Finding 三表披露 Fake 限制 | 对标 V2-A Gate 评测，不假装模型变强 |
| **DP-11** | 相关测试用路径启发式 + 名字窗口，上限 3 | 可测、有界 |
| **DP-12** | 全仓引用图 / dataflow 不做 | V2-B 是预检索不是分析器 |

若需推翻某条，只改本节与对应章节。

---

## 16. 冻结声明（ACCEPTED 后）

V2-B 核心已 ACCEPTED。仍禁止把下列非目标当成遗留 blocker：GitHub blob API、Pipeline 符号 evidence 门、全仓任意同名定义。

1. 不接 Tree-sitter 依赖、不接工具、不把 L3 当 Finding 来源。
2. 不提交、不 push、不打 tag（除非用户明确要求）。
3. V2-A compare runner 必须继续钉死 `symbol_retrieval=false`。
