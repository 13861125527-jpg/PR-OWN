# V2-A 实现复验报告（Round 3）

> 对照：`v2-a-review-round1.md`、实现者 `v2-a-review-round2.md`  
> 结论：**暂不通过最终验收，但只剩 2 个 Gate 阻塞点和 1 个评测计数修正。** B1、B4、S1 已关闭；B2、B3 仅部分关闭。

## 1. 独立门禁结果

| 检查 | 复验结果 |
|---|---|
| pytest | `388 passed` |
| Ruff | `All checks passed`（使用 `--no-cache` 绕开本机旧缓存目录权限） |
| mypy strict | `Success: no issues found in 54 source files`（缓存放到可写目录） |
| diff check | 无空白错误；只有 LF/CRLF 提示 |

说明：第一次复验指定了仓库内临时目录，但该目录受 Windows 权限影响，导致 pytest 临时文件创建失败；改用独立可写临时目录后，388 个测试全部通过。这是本机目录权限，不是代码测试失败。

## 2. 已确认关闭

### B1 两阶段 fail-closed：已关闭

Phase 1 批次出现结构异常时不再创建 Phase 2，并会生成 required failure。新增测试确认 optional LLM 调用数为 0。

### B4 三张对照表：已关闭

`reposage.evals.v2a_compare` 可以生成 JSON 和 Markdown，已经包含 Finding 质量、Fake 成本和 Fake 延迟三张数值表，并明确披露真实 API 未运行。

### S1 占位角色：已关闭

占位角色增加 `implemented=False`，配置试图启用时会在 Registry 阶段失败，不再出现“配置通过、加载 prompt 才失败”。

## 3. Blocking：B2 仍未完全关闭——只扫描 added 行会丢失语法上下文

当前 `_code_added_lines()` 只遍历 added 行，然后在这些行上维护 docstring/缩进状态。这无法判断由 unchanged context 行开启的结构。

### 复现 A：docstring 开头是上下文行，新增内容被误判为代码

```diff
 def f():
     """docs
+    never call eval(user)
     """
     return 1
```

当前实际结果：`keyword_hits == ['exec_dyn']`，会错误启用 security。新增行实际上仍在 docstring 内。

### 复现 B：外层循环是上下文行，新增内层循环被漏判

```diff
 def f(xs):
     for x in xs:
+        for y in x:
             use(y)
```

当前实际结果：没有 `loop_nested`，会漏掉 performance。新增循环实际上嵌套在未修改的外层循环中。

### 一次性修复要求

- 扫描每个 hunk 的 context + added 行来维护词法/缩进状态；
- 只有 added 行可以贡献 keyword/import/matched feature，但 context 行必须影响“是否在 docstring 内”和“当前父级结构”；
- deleted 行不能当作新代码命中，但需要谨慎处理 old/new 两侧结构；建议构造 hunk 的新文件视图：`context + added`，忽略 deleted；
- 不要把不同 hunk 的缩进栈盲目连续；每个 hunk 使用自身上下文重新建立状态，无法确定时应采取明确、可测试的保守策略；
- 加入上述两个精确回归测试：A 不启用 security，B 启用 performance；
- 重新跑 Gate 数据集并生成报告。

## 4. Blocking：B3 的 manifest 仍没有覆盖全部决定行为的正则

当前 `gate_manifest()` 包含 `_KEYWORD_PATTERNS`，但没有包含这些同样决定行为的正则内容与 flags：

- `_IMPORT_RE`
- `_COMMENT_RE`
- `_DOC_OPEN`
- `_EXCEPT_HEAD`
- `_PASS_CONT`
- `_LOOP_LINE`

`EXTRACT_ALGO = "v2-indent-scan"` 只是人工标签。如果开发者修改上述任一 regex 却忘记同步修改标签，`GATE_VERSION` 仍不会变化。因此它还不是设计要求的完整规则集 content hash。

一次性修复要求：

- 把所有行为正则统一收进 manifest（pattern + flags）；
- 最好让运行逻辑和 manifest 引用同一组规则对象，避免维护两份；
- 测试除 keyword 外，再变异 docstring 或 loop regex manifest，断言版本变化。

## 5. Should-fix：预算拒绝次数存在重复计数风险

`reposage/evals/v2a_compare.py::_budget_rejects()` 同时：

1. 统计预算失败的 task；
2. 再统计 `CoverageReason.TRUNCATED`。

同一次预算拒绝通常既生成 failed task，也生成 truncated coverage，因此可能被算两次。当前报告恰好是 0/0，没有污染现有数字，但一旦加入预算受限样本，指标会失真。

修正建议：按唯一 task target/request id 去重，或明确只从一个权威来源统计；增加一个恰好发生一次预算拒绝的测试，断言结果为 1。

## 6. 对对照报告的解释限制

当前 V1 与 V2-A 质量均为 1.0，是因为 scripted Fake 把相同候选放在 V1 和 V2-A 的 general 角色，其他角色返回空结果。这张表可以证明“执行器、Pipeline 和报告生成可重复”，但不能证明多角色提高了 Finding 质量。

本项不阻塞 V2-A，因为报告已明确标注 Fake，设计也未要求本阶段全局质量必须超过 V1。建议在报告补一句“质量相同是脚本化候选构造结果，不代表模型效果相同”，防止简历或演示时被误读。

## 7. 下一轮通过条件

1. 使用新文件侧 hunk 视图解决 B2 的两个精确复现；
2. manifest 包含全部行为正则，关闭 B3；
3. 预算拒绝计数去重并有测试；
4. 全量 pytest、Ruff、mypy 继续通过；
5. 重新生成 `v2-a-compare.json/.md`。

完成以上内容后，如无新回归，可通过 V2-A 最终验收并进入下一阶段。
