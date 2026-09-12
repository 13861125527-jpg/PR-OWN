# V3-A 对齐稿审查（Round 1）

> 审查对象：`docs/architecture/23-v3a-alignment.md`  
> 结论：**CHANGES REQUIRED / 暂不进入实现**  
> 日期：2026-08-16

## 1. 总体评价

切片方向正确：V3-A 只建设 Registry、Schema、Sandbox 和只读工具执行信封，不提前实现 Agent loop；默认仍走 V2 固定管道；控制工具只占位；读取锁定 SHA；增量、发布、反馈和 FindingPipeline 均不被工具层越权改写。这些边界可以保留。

但当前稿件还有 3 个 P1 和 2 个 P2 未闭合。它们会直接影响实现接口和安全测试，因此应先修订对齐稿，再按 T1–T8 编码。

## 2. 必须修改（P1）

### P1-1：`ToolWorkspace.snapshot` 的联合类型没有统一可执行接口

稿件写成：

```text
snapshot: HeadSnapshot | GitSnapshotProvider
```

但两者接口不兼容：

- `GitSnapshotProvider` 提供异步 `get_blob(sha, path)` / `list_paths(sha)`；
- `HeadSnapshot` 提供同步 `get(path)`，惰性读取还需要额外传入 provider 的 `fetch(git, path)`；它本身也没有 `list_paths()`。

因此 `read_file/find_files/search_code` 无法只依赖当前 Workspace 契约实现，编码时必然出现类型分支、隐藏依赖或无法读取未缓存文件。

要求：定义一个唯一的工具快照协议，例如：

```text
ToolSnapshot
  async list_paths() -> list[str]
  async get_blob(path: str) -> str | None
  repository_id: str
  head_sha: str
```

由 `GitSnapshotProvider + locked head_sha` 适配实现；需要缓存时在适配器内部组合 `HeadSnapshot`。所有五个只读工具只依赖该协议，不直接判断 union 类型。

同时明确：工具结果的 `source.sha` 必须等于 Workspace 锁定 SHA；请求 path 必须存在于该 SHA 的路径集合后才能读 blob。

### P1-2：`search_code` 的正则与超时设计不能保证安全

当前仅规定 pattern 长度、`re.compile` 和单文件 4k 字符，但 Python 回溯型正则即使输入很短也可能产生灾难性回溯。若 handler 在事件循环内同步执行 `re.search`，它会堵塞事件循环，`asyncio.wait_for` 的 15 秒超时也无法按时生效。

要求至少选择并写死一种可验证方案：

1. V3-A 将 `search_code` 定义为字面量/固定字符串搜索，正则后置；这是首选的最小安全切片；或
2. 使用具备硬超时/可终止能力的安全正则执行边界，并增加灾难回溯对抗测试。

仅放入 `asyncio.to_thread` 仍不能真正停止超时后的线程，不应宣称是硬超时。

另外必须增加整次调用的工作量上限，而不只是“单文件 4k”：至少规定 `max_files_scanned`、`max_total_chars_scanned`、稳定路径顺序，以及达到扫描上限时 `truncated=true`。否则大仓库会在得到 50 个命中前扫描无限多文件。

### P1-3：取消协议前后矛盾

§3.3 的错误协议包含 `cancelled`，§9 又同时写：

- “取消：传播 `CancelledError`”；
- “未完成 → `cancelled`”。

两者不能同时由同一 `invoke()` 完成。若异常向上传播，就不会正常返回 cancelled ToolResult；若转换成结果，就会吞掉父任务取消。

要求统一为：

- `invoke()` 不吞 `asyncio.CancelledError`，取消必须传播给父级；
- 若需要审计 cancelled 状态，在 `finally/except CancelledError` 中尽力记录 `ToolCall.status=CANCELLED`，随后重新抛出；
- 调用方不会收到常规 `(ToolCall, ToolResult)`；是否构造无 data 的取消结果仅用于审计，并明确落库失败不能阻止取消传播。

增加测试：取消慢工具后，父 task 确实进入 cancelled；handler 不继续产生可见结果；审计开启时状态最多记录为 cancelled，不能记录 ok/timeout。

## 3. 应在本轮一起明确（P2）

### P2-1：`result_limit` 的单位与截断规则不明确

现有 `ToolDefinition.result_limit` 是单个整数，但稿件同时使用“行、条、hunk、字符/token”等不同口径。通用信封无法仅凭这个整数安全截断任意字符串/结构化数据。

要求明确每个 handler 先按工具语义裁剪，再由信封执行统一的最终字节/字符硬上限：

- `read_file`：最大行数 + 最大字符数；
- `find_files/find_references/search_code`：最大条数 + 最大总字符数；
- `read_diff`：最大 hunk/行数 + 最大总字符数；
- 所有输出保持合法、可解析的结构，不能从 JSON 序列化字符串中间截断；
- 任一上限触发均设置 `truncated=true`，并保留稳定排序。

建议不要在 V3-A 宣称精确 token 上限，除非执行信封真的接入统一 tokenizer；字符硬上限即可作为安全边界。

### P2-2：工具调用与结果落库需要原子性及脱敏契约

`record_tool_invocation(call, result)` 应明确为同一 SQLite 事务：先验证 task FK，再同时写 call/result；失败整体回滚，避免孤立 `tool_calls`。同一 `tool_call_id` 重试的幂等策略也需写明（建议同值幂等，冲突值拒绝）。

此外 `args_json`、`data`、`error` 都可能包含源码、路径或疑似密钥。要求：

- `observability.save_tool_trace=false` 时不落 call/result 全文；
- 开启时仍先经过统一 secret redaction；
- `data` 遵守最终字符上限；日志不记录正文；
- `source` 若塞入 data 信封，要规定固定字段结构，避免未来 V3-B 二次解析不一致。

## 4. 已确认可保留的决策

- V3-A 不实现 Agent loop、`tool_loop`、AgenticReviewer。
- `strategy=agentic` 在尚未实现时失败快，不静默降级。
- 五个只读工具执行，两件控制工具 stub，不进入 Pipeline。
- 不把 Ruff/Judge/反馈包装成模型工具。
- 使用 Pydantic 生成 JSON Schema，args `extra="forbid"`。
- 默认 `agent.enabled=false`，V2-A～E 对照显式隔离 Agent。
- 文件内容只来自锁定 head 快照，不读工作区脏文件。
- 不改变 Publisher、watermark、反馈与 Finding 生命周期。
- 本阶段不做真实模型 tool calling；将其作为 V3-B 开工门禁。

## 5. 修订后验收条件

对齐稿补齐以下内容即可进入实现，无需重写整体架构：

1. 用单一 `ToolSnapshot` 协议替代不兼容 union，并写清 adapter/cache 关系。
2. 将 V3-A 搜索改成字面量搜索，或给出真正可终止的正则方案；增加总扫描上限。
3. 统一取消语义为“审计后重新抛出”，补取消测试。
4. 定义各工具语义上限 + 信封最终字符硬上限，保证结构化输出完整。
5. 定义工具 trace 的事务、幂等、脱敏和大小限制。

## 6. 最终判定

**V3-A 对齐稿 Round 1：暂不通过。**

问题集中且不需要改变产品方向。建议 Cursor 一次性修订上述 5 项，然后再开始 T1–T8；不要在接口仍含糊时直接编码。
