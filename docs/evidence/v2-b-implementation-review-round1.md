# V2-B 实现审查报告（Round 1）

> 审查对象：跳过设计 Round 2 后直接完成的 V2-B 实现  
> 结论：**暂不通过 V2-B 验收。** 代码主链路和全量门禁可运行，但上一轮未闭合的设计问题已经转化为生产能力、预算、缓存和评测证据缺陷。

## 1. 独立门禁结果

| 检查 | 结果 |
|---|---|
| pytest | `404 passed` |
| Ruff | `All checks passed` |
| mypy strict | `Success: no issues found in 60 source files` |
| git diff --check | 无空白错误，仅 LF/CRLF 提示 |

说明：测试通过证明现有测试覆盖的路径能运行，不等于 V2-B DoD 已满足。以下问题均来自已批准对齐稿或上一轮设计审查中的明确契约。

## 2. Blocking：必须修复

### B1. 默认开启，但真实项目路径仍没有 L3 数据源

当前实现：

- `Settings.context.symbol_retrieval=True`；
- 只有 `FakeGitProvider` 实现 `get_blob/list_paths`；
- `providers/git/base.py` 仍只是 Protocol 导出，没有 LocalGitProvider/GitHub Provider；
- Service 使用 `hasattr(self.git, "get_blob")`，缺能力时直接跳过，不记录 warning/capability miss。

因此 V2-B 在 Fake 测试中有效，在真实 CLI Provider 尚不存在的情况下默认开启却静默为空。这正是设计审查 B4 的问题，状态报告也把它写成“已知边界”，但它与“能力默认可用”的 DP-5 冲突，不能作为完成态验收。

一次性修改要求二选一：

1. **推荐**：实现最小只读 LocalGitSnapshotProvider，基于锁定 head SHA 提供安全的 `list_paths/get_blob`；或
2. 暂时把默认值恢复为 False，只有明确提供 Snapshot capability 时开启。

无论选哪种，开关为 True 但 Provider 不具备能力时，CONTEXT stage 必须结构化披露 capability miss，不能静默跳过。

### B2. 缓存键没有满足 repo + SHA + path + content hash + extractor id

`SymbolIndex.cache_key()` 当前只有：

```text
extractor.id + content_digest + path
```

缺少 repository identity 和 head SHA；状态报告也明确写成 `extractor_id:sha256:path`。这不符合对齐稿 DoD 及 §8.3。

一次性修改要求：

- 使用结构化 `SymbolCacheKey(repository_id, head_sha, canonical_path, content_hash, extractor_id)`；
- repository_id 的来源必须稳定、明确；
- `HeadSnapshot`/prepare 层将 repo、SHA 传给 index；
- 测试分别改变 repo、SHA、path、content、extractor id，五项任一变化都导致 key 变化；
- fallback 算法版本进入 extractor id，例如 `python-ast+indent-v2`。

### B3. L3 预算上限未执行，完全塞不下时还会静默丢失

`ContextBudget.l3_tokens` 已定义，但装配时 `_pack_l3(max_tokens=remaining)` 使用的是 unit 全部剩余空间，没有执行：

```text
min(名义 L3 预算, 实际剩余)
```

因此 L3 可以超过 `related_code_ratio=0.25`，侵占弹性预算，配置项没有真实约束。

另一个更严重的分支：当 `hits` 非空但 `remaining <= 0` 时，代码不会调用 `_pack_l3`，`l3_dropped` 仍为空，最终既没有 L3 chunk，也没有对应 truncated coverage，形成静默丢失。

一次性修改要求：

- `l3_cap = min(budget.l3_tokens, max(0, remaining))`；
- hits 非空且 cap=0 时，全部 hits 进入 `l3_dropped`；
- coverage 是权威丢失记录；有空间才加入 marker，无空间允许无 marker；
- 测试断言 L3 token 总量不超过 l3_tokens；cap=0 时每个丢弃 hit 有 TRUNCATED coverage，L2 完整、unit 不超 input_limit。

### B4. 修改符号与 import 检索没有实现对齐稿的关键语义

存在四个实际缺口：

1. **顶层 added 永远没有 module modified symbol**：Extractor 不产出 `SymbolKind.MODULE`，`_modified_symbols()` 对不落入 def 的 added 行直接跳过；对齐稿 §5.1 要求顶层 added 记 module。
2. **所有 import 都会检索**：实现遍历全文全部 import，并未限制为 added 行/modified symbol 实际使用的 imported name，容易把 8 个配额消耗在无关依赖上。
3. **alias 语义丢失**：`parse_imports()` 对 `import pkg.mod as pm` 返回 `(pkg.mod, None, 0)`，后续随意取目标模块第一个定义，而不是根据 `pm.symbol` 使用解析目标。
4. **同文件完整覆盖按整个 hunk 判断，而不是当前 unit**：`_hunk_new_ranges()` 使用整个 hunk 的 min/max；hunk 被拆成多个 ReviewUnit 后，某个 unit 未包含完整 def，也可能被认为“L2 已完整覆盖”，导致该 unit 缺少必要 L3。

一次性修改要求：

- 显式构造 module modified symbol，并从 added AST names/import uses 生成检索种子；
- import 模型保存 module、imported_name、local_alias、level；只检索实际使用名字；`import *` 明确不展开；
- 相对 import、alias、module attribute 增加测试；
- `_fully_covered` 必须针对当前 ReviewUnit 的 L2 行区间计算；因此 L3 hits/packing 至少需要 per-unit 过滤，不能只按整文件一次判断。

### B5. Snapshot hydration 仍是“顺序扫描测试文件直到 40 个”，会产生不稳定漏检

`hydrate_head_snapshot()` 对 `paths` 中所有测试文件依次 fetch，读取后才判断是否包含名字。问题包括：

- 没有先按 module/symbol 路径启发式选候选；
- 仓库测试较多时，前 40 个无关测试就会耗尽 cap，真正相关测试可能永远不读；
- 读取顺序依赖 Provider 返回顺序，hydrate 内没有统一排序；
- `names` 来自 changed file 的全部 definitions，不是 modified symbols，进一步扩大噪声；
- 不匹配的测试从 `blobs` 删除但 `files_read` 保留，日志中的“读取”和“保留”语义混在一起。

一次性修改要求：

- 先按路径生成稳定候选并排序，再在独立的 candidate-read cap 内读取；
- 候选名称只来自 modified symbols/module added refs；
- 分开统计 `requests_attempted / blobs_read / candidates_considered / hits_kept / skipped_by_cap`；
- 测试：40 个无关测试排在相关测试前时，路径启发式仍优先读到相关候选；Provider 返回乱序时结果保持一致。

### B6. 异步准备失败被静默吞掉，缺少按文件诊断和取消纪律证明

`hydrate_head_snapshot()` 内部捕获 `list_paths/get_blob` 的普通异常并返回空结果；`HeadSnapshot.fetch()` 也吞掉异常。Service 只有 hydrate 整体抛出时才 warning，因此大多数 IO 失败不会留下 warning、coverage 或 diagnostics。

此外 Service 只检查 `hasattr(get_blob)`，不检查 `list_paths`，能力判定不完整。直接扩展基础 `GitProvider` 也让所有非 L3 桩承担无关方法。

一次性修改要求：

- 拆出 `GitSnapshotProvider` capability，显式检查两项能力；
- prepare 返回 `PreparedL3Snapshot + RetrievalDiagnostics`，而不是把失败压成空 dict；
- 普通单文件失败降级并记录 path/reason（不记录源码）；
- `CancelledError` 原样传播并有测试；
- cap 截断与 IO failure 分开，不能都只用 `snapshot.truncated` 一个 bool。

### B7. 对照报告没有证明 L3-on 链路实际吃到 L3

当前 `v2-b-compare.md` 的主表显示：

```text
L3 chunks: off=0, on=0
input tokens: off=2100, on=2100
```

这意味着所谓 L3-off/on 对照数据集没有任何 L3 内容进入模型消息。报告只能证明“开关为空时结果相同”，无法证明 L3-on 管道有效，也没有满足对齐稿 §12.2 的“跨文件真值样本 L3 Token >0”。

同时还有两个指标错误：

- 位置准确率显示 0.0/0.0，而 V1/V2-A 同数据是 1.0；`v2b_compare` 没有像 v2a runner 那样给 metrics.details 补 `n_expected`，导致 `_avg_position()` 过滤为空；
- FakeLLMProvider 使用固定 usage，因此即使加入 L3，也不能用当前 usage 证明 input token 增量。

一次性修改要求：

- 对照 runner 使用至少包含跨文件 import/definition 的同一数据集，确保 L3-on `chunks > 0`、off=0；
- 直接从 ReviewUnit/messages 的估算 token 统计 context/input 增量，或让 Fake usage 按消息确定性计算；不要用固定 100 tokens；
- 修复 `n_expected/sample_kind` details，使位置准确率口径与 V2-A 一致；
- 加入“只有消息含指定 L3 symbol 才输出 scripted finding”的 Fake 分支，证明链路消费了 L3；明确它仍不代表真实模型增益；
- 重新生成 JSON/MD，报告不得再以 0 个 L3 chunk 作为 V2-B 完成证据。

### B8. L3 命中率计算是按样本，不是按 expected symbol

`evaluate_l3_dataset()` 对一个样本的多个 `expected_symbols`：只要缺一个就 `fn += 1`，全部命中才 `tp += 1`。这不是 symbol hit rate，且会让不同标注数量的样本权重异常。`forbidden_symbols` 同样按样本只加一个 FP。

一次性修改要求：

- 每个 expected symbol 分别计 TP/FN；每个 forbidden symbol 分别计 FP/TN（或至少 FP 分母写清）；
- 报告给出 expected symbol 总数和 forbidden symbol 总数；
- 数据集至少覆盖 top-level、alias、relative import、cap 截断、per-unit split 和 provider failure，不要只覆盖最顺利的 6 个正命中。

## 3. Should-fix

### S1. Snapshot cap 的计数名不准确

`files_read` 在调用 `get_blob` 前递增，即使返回 None 或抛异常也计为已读。建议改为 `requests_attempted`，成功正文另计 `blobs_read`。

### S2. 全局 truncated 被复制到每个文件/unit

只要 snapshot 在任意候选上触顶，所有 unit 都得到 `L3 file read cap` coverage。应根据受影响的 changed file/retrieval request 归因，避免无关文件被标截断。

### S3. 状态报告不能把未满足 DoD 的项目降为“已知边界”

生产 Provider、候选范围和缓存键都是设计稿的验收内容，不属于可以直接后置的说明项。修复后应让 `v2-b-status.md` 区分：已完成、明确非目标、未完成阻塞。

## 4. 已确认正确、建议保留

- L3 放在 user 侧并包裹 UNTRUSTED；
- 只有存在 L3 时才增加 L0 说明，关闭开关保持消息兼容；
- Strategy.execute 主签名未改；
- AST 失败有无原生依赖回退；
- Fake blob 路径有基本安全校验；
- V2-A compare 显式关闭 L3；
- 全量 V1/V2-A 回归目前通过。

## 5. 建议一次性修复顺序

1. 先修 B1/B6：能力协议、真实/默认策略、异步 prepare diagnostics；
2. 修 B2：结构化缓存键与 extractor 版本；
3. 修 B4/B5：modified refs、import 模型、per-unit 覆盖、稳定候选读取；
4. 修 B3：L3 cap 与零空间 coverage；
5. 修 B7/B8：换成真正含 L3 的对照数据和 symbol-level 指标；
6. 全量 pytest/Ruff/mypy/diff check，重新生成状态与三表。

## 6. 下轮通过条件

- 默认开启时至少存在一个真实可用 Snapshot Provider，或默认恢复 False；
- cache key 五个维度全部有测试；
- L3 不超名义预算，零空间不静默丢失；
- top-level、alias、relative import、per-unit split 测试通过；
- bounded candidate 策略不因前 40 个无关测试漏掉高优先级相关测试；
- IO failure/cap/capability miss 有结构化诊断；
- 对照报告 L3-on 实际 chunks/tokens >0，位置准确率口径正确；
- 检索指标按 symbol 计数；
- 全量门禁继续通过。

在这些条件满足前，V2-B 状态应保持 **IMPLEMENTED / NOT ACCEPTED**。
