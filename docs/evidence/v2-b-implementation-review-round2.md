# V2-B 实现复验报告（Round 2）

> 对照：`v2-b-implementation-review-round1.md`  
> 结论：**大部分 Blocking 已关闭，但暂不通过最终验收。** 当前剩 3 个阻塞问题和 2 个应修问题，范围已经明显收敛。

## 1. 独立门禁结果

| 检查 | 结果 |
|---|---|
| pytest | `416 passed` |
| Ruff | `All checks passed` |
| mypy strict | `Success: no issues found in 61 source files` |
| git diff --check | 无空白错误，仅 LF/CRLF 提示 |

第一次指定的 pytest 临时目录受本机 Windows 权限影响，涉及 `tmp_path` 的测试无法创建目录；改到可写的系统临时目录后，416 个测试全部通过。这不是项目测试失败。

## 2. 已确认关闭

以下 Round 1 问题已正确修复：

- GitSnapshotProvider 已与基础 GitProvider 解耦；
- capability miss 会产生 warning + coverage；
- cache key 已包含 repo/SHA/path/content/extractor 五项；
- extractor id 已包含 AST + indent fallback 版本；
- L3 执行名义 token cap，cap=0 时命中进入 truncated coverage；
- top-level module、ImportSpec alias、relative import、star import、per-unit L2 ranges 已有实现和测试；
- Snapshot IO failure 与 cap 分开，CancelledError 原样传播；
- 对照数据实际产生 L3：chunks `0→4`，估算 input tokens `645→808`；
- 位置准确率口径已修正；
- 检索指标改为按 symbol 计数，数据集 16 条，TP/FN/FP=`9/0/0`。

这些部分不需要再次重写。

## 3. Blocking：仍需修复

### B1. Snapshot prepare 仍会预加载所有 import 目标，无关 import 可能耗尽 cap

状态报告声称“只检索实际使用名”，`collect_l3_hits()` 最终产出阶段确实会过滤未使用 import；但异步预加载阶段并没有做到。

`prepare_l3_snapshot()` 当前先执行：

```python
for spec in parse_imports(src):
    for target in resolve_import_paths(...):
        extra.append(target)
```

这会把 changed file 全部 import 目标加入最高优先级候选。随后调用的 `used_import_targets()` 也没有检查 used names，仍返回全部非-star import 路径。因此，一个带大量未使用 import 的文件会先读取无关模块并消耗 40 文件上限，真正被新增代码使用的 import 或测试候选可能被挤掉。

现有 `test_unused_import_not_retrieved` 只测试最终 `collect_l3_hits()`，没有测试 prepare 阶段是否读取了 unused module，所以没有捕获该问题。

一次性修复要求：

- prepare 在 changed blob 上先计算 added-line used names；
- 只对 `ImportSpec.local_alias` 实际被 used names 引用的 spec 生成 import target；
- 删除“全部 specs 加 extra”这条路径；
- `used_import_targets()` 要真正按 used names 过滤，或更名并改变调用方式，避免名称与行为相反；
- 新增异步测试：设置多个 unused imports + 1 个 used import，cap 只够少量文件，断言请求列表不含 unused targets，used target 一定被读取。

### B2. collect_l3_hits 的程序异常仍被 ContextAssembler 静默吞掉

装配器当前：

```python
try:
    hits = collect_l3_hits(...)
except Exception:
    hits = []
```

这会把抽取器/检索器的程序错误无声转换成“没有命中”。Snapshot IO 已有 diagnostics，但同步检索阶段仍没有 diagnostics、warning 或 coverage，Round 1 B6 只关闭了一半。

一次性修复要求：

- 不要在 assembler 里无信息地 `except Exception`；
- 推荐让 `collect_l3_hits` 返回 `L3CollectionResult(hits, failures)`，预期的单文件抽取失败结构化降级；
- 非预期程序异常至少写入 unit coverage/warning carrier，或向 Service 返回 diagnostics，不能伪装为正常空命中；
- CancelledError/BaseException 继续不捕获；
- 增加测试：注入 extractor RuntimeError 后 run 仍可降级完成，但 warning/coverage 明确包含 `L3 extraction failed`，且不含源码。

### B3. LocalGitSnapshotProvider 已实现但没有任何应用装配路径使用它

仓库搜索结果显示，`LocalGitSnapshotProvider` 目前只被导出和单测引用；没有 CLI/bootstrap/factory 创建它并传入：

```python
ReviewService(..., snapshot_provider=LocalGitSnapshotProvider(repo_root))
```

ReviewService 的注入点是正确的，但“有一个类”不等于真实项目路径已接通。若实际 GitProvider 本身不带 get_blob/list_paths，默认开启后仍只会 capability miss。

一次性修复要求：

- 在真实本地运行的 composition root/CLI 创建 LocalGitSnapshotProvider，并传给 ReviewService；
- repo_root 使用已经解析验证的仓库根目录；
- 增加一条接近真实启动路径的集成测试：建立临时 git repo → 通过实际 factory/CLI composition 创建 service → 不手工传 Fake snapshot → CONTEXT 产生 L3；
- 如果本项目本阶段确实还没有真实 CLI composition root，则默认值必须暂时恢复 False，并在后续接入时再开启，不能在状态报告中声称真实 Local 路径完成。

## 4. Should-fix

### S1. cap 归因对启发式候选仍可能落到候选文件本身

`prepare_l3_snapshot()` 只为 import targets 填 `requested_by`。同名模块/测试启发式候选在 cap 跳过时使用候选 path 自身作为 owner，最终 `ContextAssembler` 却只检查 changed file path 是否在 `cap_affected_paths`。因此启发式候选被 cap 截断时，对应 changed unit 可能没有 truncated coverage。

建议候选模型显式携带 `owners: set[changed_path]`，排序和 cap 都保留 owner；测试断言相关测试因 cap 未读时，发起检索的 changed file unit 被标 truncated。

### S2. Local get_blob 每次重复执行完整 list_paths

`LocalGitSnapshotProvider.get_blob()` 每读取一个 blob 都先调用一次 `git ls-tree`。prepare 已经调用过 list_paths，读取 N 个候选将产生 N 次额外全树枚举，较大仓库会有明显开销。

建议 Provider 按 SHA 缓存规范化 path set，或为本次 snapshot 复用首次 list_paths 结果。补调用次数测试，多个 get_blob 不应重复执行 ls-tree。

另：`RetrievalDiagnostics.hits_kept` 当前被赋值为 `len(snap.blobs)`，实际是保留 blob 数，不是 L3 hit 数。建议改名为 `blobs_kept`，真正 hits 由装配结果统计，避免观测指标误导。

## 5. 对照报告评价

本轮对照报告已经具备验收价值：

- L3-off/on 确实存在消息差异；
- input token 使用 ReviewUnit messages 估算，不再依赖 Fake 固定 usage；
- `v2b-consume` 证明 L3 内容进入了模型输入链；
- Fake 限制披露清楚；
- 检索指标按 symbol 计数。

因此 B7/B8 不重开。完成 B1–B3 后只需重新跑报告确认数字未退化。

## 6. 下一轮通过条件

1. prepare 不再读取 unused import targets，并有 cap 压力测试；
2. 同步抽取/检索失败不再静默伪装成空命中；
3. LocalGitSnapshotProvider 真正进入应用装配路径，或默认开关恢复 False；
4. cap owner 归因正确；
5. Local provider 不对每个 blob 重复 ls-tree；
6. 全量 pytest/Ruff/mypy 继续通过；
7. L3 对照仍保持 on chunks/tokens > off。

在此之前，V2-B 保持 **IMPLEMENTED / NOT ACCEPTED**。
