# V2-B 对齐稿设计审查（Round 1）

> 审查对象：`docs/architecture/19-v2b-alignment.md`  
> 结论：**暂不通过设计验收，禁止开始生产代码。** 方向正确、边界清楚，但快照读取编排、候选发现能力、生产 Provider 可用性和预算契约还没有闭合。

## 1. 总体评价

对齐稿已经正确确定了这些关键方向：

- L3 属于 CONTEXT，不藏在 MultiRole Strategy 内；
- SinglePass 与 MultiRole 共用同一批 ReviewUnit；
- V2-B 不引入 Agent、工具循环、Judge、embedding 或 Tree-sitter 强依赖；
- PR head 内容继续按 UNTRUSTED 处理；
- 用程序检索命中率作为主指标，不用 Fake 冒充真实模型增益；
- V2-A 历史对照显式关闭 L3，避免指标漂移。

这些可以保留。下面的问题必须在设计层先统一，否则实现到 T4/T7 时会被迫改变接口和读取策略。

## 2. Blocking：必须修订后再编码

### B1. 同步 ContextAssembler 与异步惰性 Snapshot 无法直接组合

当前事实：

- `ContextAssembler.build_file_units(...)` 是同步函数；
- 新增的 `GitProvider.get_blob/list_paths` 是异步接口；
- 设计又要求 assembler 内完成 modified symbol → import → test 的逐步惰性检索；
- Service 当前只是同步循环调用 assembler。

同步 assembler 无法在检索过程中按需 `await get_blob()`。仅增加 `snapshot=` 参数不能解决问题，除非 snapshot 在进入 assembler 前已经准备好全部需要的 blob；但“需要哪些 blob”又要先解析 changed file、imports 和测试候选才能知道。

必须选定一种唯一编排，建议采用：

```text
Service CONTEXT
  await L3ContextBuilder.prepare(req, changed_files, git_provider, limits)
      list_paths
      fetch changed blobs
      extract modified symbols/import candidates
      fetch bounded import/test candidates
      build immutable PreparedL3Snapshot + RetrievalDiagnostics
  ContextAssembler.build_file_units(..., prepared_l3=...)
```

也就是把异步 IO 放在独立的 `L3ContextBuilder.prepare()`，assembler 保持同步纯装配。不要让 HeadSnapshot 暗中进行异步 IO，也不要在同步 assembler 里启动事件循环。

对齐稿需补充：

- prepare 的输入、输出模型；
- warnings、files_read、truncated coverage 如何从 prepare 传到 Service/ReviewUnit；
- 取消在 prepare 中原样传播；
- 单文件失败如何只降级对应文件，而不是清空整个 run 的 L3。

### B2. “全仓同名定义”与“惰性读取 ≤40 文件”目前互相矛盾

要知道某个未读文件是否定义同名符号，必须先有索引或读取它。当前里程碑明确：

- 不落 SQLite 符号索引；
- 不使用 embedding/FTS；
- 不一开始读取全仓；
- 最多读取 40 个文件；
- 却要求查找“快照内其它 path 的同名定义”。

在没有路径提示或已有索引时，这个目标不可实现。需要收窄并写成可执行候选规则。建议 V2-B 定稿为：

1. changed file 本身；
2. changed file 全文中的直接 import 可解析目标；
3. 与模块/符号同名的路径候选，例如 `foo.py`、`foo/__init__.py`；
4. 测试路径启发式候选；
5. 只在上述已读候选内做同名定义冲突检测。

如果坚持“全仓任意同名定义”，就必须允许扫描全部 Python blob 或提前维护仓库级索引，这已经超出当前惰性/40 文件边界。

文档中的“同一 qualname 多个 path 全部纳入”也应改为“候选集内发现的多定义全部纳入，并披露搜索不完整”。不能声称枚举了全仓事实。

### B3. 测试文件“内容含名字后才读取”形成循环依赖

稿子一方面要求相关测试必须包含 symbol name，另一方面要求“只读命中文件”。但不读取文件就不知道内容是否含名字。

必须明确两段式策略，例如：

1. 仅按 path 排序候选：`tests/test_<module>.py`、`<module>_test.py`、文件名含 symbol/module；
2. 在剩余文件读取额度内，按稳定顺序最多读取 K 个候选；
3. 读取后再用 token/name 边界匹配内容；
4. 最终最多保留 3 个测试窗口。

需要同时定义：候选读取上限和最终命中上限不是同一个数；路径排序必须稳定，不能依赖 `list_paths()` 原始顺序。

### B4. 默认开启 L3，但当前没有可用的生产 GitProvider 实现

仓库目前只有 `FakeGitProvider`，`providers/git/base.py` 只是 Protocol 导出，并不存在 local_git/github_api blob 生产实现。对齐稿又同时裁决：

- `symbol_retrieval` 默认 True；
- 本里程碑只给 Fake 实现 `get_blob/list_paths`；
- 本地 Git Provider 可后置。

这意味着能力默认开启，但真实 CLI 路径没有数据源，只能无声降级为空 L3。这样 V2-B 在测试里完成、产品里不可用。

必须二选一：

- **推荐**：V2-B 同时实现最小 `LocalGitProvider.get_blob/list_paths`，只读锁定 head SHA；GitHub API 仍后置；或
- 本阶段默认 False，只对 Fake/明确具备 snapshot capability 的 Provider 开启，等本地 Provider 完成后再改默认 True。

不建议“默认 True + 所有真实运行静默为空”。降级必须在 stage detail/warning 中披露 provider capability miss。

### B5. 扩展 GitProvider Protocol 会破坏不需要 L3 的现有桩与 Provider

直接给 `GitProvider` 增加两个必需方法，会让现有测试桩和未来只负责 diff/publish 的实现不再满足结构类型，即使 `symbol_retrieval=false`。

建议拆成能力协议：

```text
GitProvider                 # 原有 get_changes/get_diff/publish/delete
GitSnapshotProvider         # list_paths/get_blob
```

Service 在 L3 开启时做显式 capability 判断；关闭时完全不要求 Snapshot Protocol。这样才能真正保证 V1 字节/行为兼容，也能清楚记录 capability miss。

如果坚持合并 Protocol，必须列出全部现有桩的迁移方案，并说明开关关闭时为何仍要求实现无关方法。

### B6. 缓存键定义前后不一致，且 repo identity 未定义

DoD 与 §2.1 写：

```text
repo + sha + path + content_hash + extractor_id
```

§8.3 又写：

```text
extractor_id + sha + path + content_hash
```

缺少 repo。虽然缓存当前是 run 内，但对齐稿明确把 repo 列为验收字段，必须统一。还需要定义 repo identity 从哪里取得：不能用用户可变 title，也不能假设 SHA 在跨仓库全局唯一。

建议 `RepositoryIdentity` 由 Provider/ChangeRequest 提供稳定 canonical id；在当前单 run 缓存仍保留字段，便于未来提升缓存作用域。缓存 key 应使用规范化 tuple/dataclass，不能靠字符串拼接。

另外 `PythonAstExtractor` 与 tokenize fallback 若共享一个 id，需要把二者共同算法版本写进复合 extractor id；否则 fallback 规则变化不会失效。

### B7. ContextBudget 的配置映射与文中百分比不一致

当前代码的 `ContextBudget` 是：

```text
L0 5% + L1 5% + L2 40% + L4 10% + reserve 40% = input 内部 100%
output reserve 另按 total window 的 15%
```

当前 `Settings.context` 则只有：

```text
diff 40% + related 25% + rules 10% + reserve 15% = 90%
```

对齐稿 §7 又写：

```text
L0 5 + L1 5 + L2 40 + L3 25 + L4 10 + 弹性 15 = 100
```

但没有说明这些比例基于 total window 还是 input_limit；同时 `reserve_ratio` 在 Settings 和 `output_reserve_ratio`/输入弹性 reserve 之间存在同名异义。

必须给出唯一公式和 Settings→ContextBudget 映射表，至少明确：

- output reserve 是否固定为 total window 的 15%；
- Settings 的 `reserve_ratio` 是输出预留还是输入弹性；
- L0/L1 的 10% 从哪里来；
- 六项是否必须等于 1；
- `related_code_ratio=0` 时命中如何记 coverage；
- L3 关闭时 25% 是归还 L2/输入弹性，而不是凭空缩小 input_limit。

建议避免同时存在两个含义不同的 `reserve_ratio`，必要时重命名为 `output_reserve_ratio` 与 `elastic_ratio`。

### B8. “L3 被完全省略”时无法同时要求块内写 TRUNCATED marker

设计写“有命中但塞不下，hit 记 CoverageItem.truncated，块内或省略处写 `[TRUNCATED: …]`”。若预算连一个 L3 marker chunk 都放不下，就不存在可写 marker 的块；强行加入又会挤占不可裁的 L2 或突破 input_limit。

应定稿为：

- CoverageItem 是丢失事实的权威记录；
- 有最小空间时可加入一个固定上限的 L3 summary marker；
- 无空间时允许只写 coverage，不生成 chunk；
- marker 本身也必须计 token，不能突破硬预算。

## 3. Should-fix：建议本轮一起定稿

### S1. “直接 import 目标”需要限定到实际使用名字

如果把当前文件所有 import 都取定义，常见模块会迅速耗尽 8 个目标并引入噪声。建议只选择：

- added 行或 modified symbol 源码中实际引用的 imported name；
- `module.symbol` 属性调用可解析到 module import；
- `import *` V2-B 明确不展开或只记 unsupported。

同时定义相对 import、alias、`__init__.py`、包/模块冲突的确定性解析顺序。

### S2. “同文件定义已完整出现在 L2”需要可计算定义

L2 是 diff hunk，不是 head 文件全文。仅凭路径相同不能认为完整定义已出现。应以该 unit 的 L2 新文件行区间是否完整覆盖 `SymbolDef.start_line..end_line` 判断；无法完整覆盖才补 L3。多 hunk/多 unit 时必须按每个 unit 单独判断。

### S3. 顶层 module ModifiedSymbol 的 name/检索种子不明确

顶层新增赋值、调用或装饰器不一定有自然 symbol name。需要规定 module modified symbol 的 qualname（如 `<module>`）以及检索种子来自 added AST names/import uses，而不是拿 `<module>` 去做同名检索。

### S4. 检索结果应携带“搜索范围是否完整”

建议 `L3Hit` 之外增加 retrieval diagnostics：

- candidate_paths_considered/read/skipped_by_cap；
- complete=false 的原因；
- extractor fallback 是否发生；
- provider capability 是否缺失。

“冲突”与“搜索不完整”是两回事，不应都塞进 `notes` 自由文本。

### S5. 文件读取上限需区分全局与单文件

`max_l3_files_read=40` 写成硬顶，但没有说明是 run 级、changed-file 级还是 unit 级。建议采用 run 级共享计数器，避免 40×changed_files；再设置 per-changed-file 的公平上限，防止第一个文件耗尽全部额度。

## 4. 建议重排任务卡

当前 T1–T10 的方向基本合理，但应先把异步准备层和候选规则落地：

| 建议顺序 | 内容 |
|---|---|
| T1 | 领域模型 + 独立 `GitSnapshotProvider` capability |
| T2 | Extractor + 明确版本化缓存键 |
| T3 | 纯函数 modified symbol / import candidate / test path candidate |
| T4 | `L3ContextBuilder.prepare()` 异步有界读取、诊断与取消 |
| T5 | 候选集内检索、去重、冲突和命中率数据集 |
| T6 | 唯一 ContextBudget 公式与 L3 packing |
| T7 | unit_to_messages + 开关关闭字节兼容 |
| T8 | Service 接 prepare，覆盖/日志/降级 |
| T9 | L3-off/on 对照 runner |
| T10 | 全量门禁与交付报告 |

这样不会出现先把 Service 接到一个尚未定义 IO 边界的 snapshot 上。

## 5. 修订后必须补入对齐稿的决策

请至少新增或改写以下 DP：

1. 异步 IO 在 `L3ContextBuilder.prepare`，assembler 保持同步；
2. 同名定义只保证候选集内发现，或明确允许全仓索引/扫描；
3. 测试候选先按路径选，再有界读取并做内容匹配；
4. Snapshot capability 与基础 GitProvider 分离；
5. 默认 True 的前提是至少一个真实 Provider 可用，否则默认 False；
6. 缓存键统一包含 repository identity；
7. Settings→ContextBudget 唯一公式；
8. L3 完全塞不下时 coverage 为权威，marker 可省略；
9. `max_l3_files_read` 的作用域、公平性与稳定读取顺序；
10. import alias/relative import/star import 的确定性范围。

## 6. 下轮设计通过条件

- B1–B8 在对齐稿正文中形成单一、可实现的契约；
- 不再同时承诺“全仓发现”和“没有索引且只读 40 文件”；
- 默认开关与真实 Provider 能力一致，不出现默认开启但产品恒为空；
- 预算公式能直接映射到当前 Settings/ContextBudget；
- 每个失败/截断路径都有结构化结果承载，不依赖自由文本猜测；
- 任务卡按确定的 IO 边界排序；
- 继续保持“审查通过前不编码”。

完成上述设计修订后再进行 Round 2 复验；本轮不建议开始 T1–T10 实现。
