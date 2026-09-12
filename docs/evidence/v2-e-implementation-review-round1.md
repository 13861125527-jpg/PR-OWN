# V2-E 实现验收报告（Round 1）

> 结论：**ACCEPTED / 通过**  
> 验收时间：2026-08-16  
> 对齐依据：`docs/architecture/22-v2e-alignment.md`

## 1. 验收结论

V2-E 的反馈记忆与增量审查已形成完整闭环，未发现阻塞发布或破坏既有 V1/V2 能力的问题。本轮可以验收，不需要再为非阻塞项追加一次大模型返工。

核心成立项：

- 反馈写入禁止“仅 repo 的全局压制”，路径、scope、category、pattern 均有校验。
- 反馈条件采用 AND 匹配；默认从 Finding 复制 path/category/rule_id，不默认复制行号相关的 cross-run key。
- 压制发生在统一 Pipeline 内、Judge 之前，只处理 `MERGED/BODY_ONLY`，不会把 `ACCEPTED` 逆向改为 `SUPPRESSED`。
- 反馈撤销采用软删除，撤销后不再参与匹配。
- L4 反馈上下文按文件预过滤、限制条数、受上下文预算约束；仅 cross-run key 的记忆不会注入；生成文本不暴露用户提供的 path/scope。
- 增量 FETCH 只在存在稳定 PR identity 和已提交 watermark 时启用；无身份、无 watermark 或增量 diff 失败时安全回退到全量 diff。
- `run.base_sha` 仍保留原 PR base，FETCH 明确记录实际 `from/to` 区间。
- dry-run 不推进 watermark；正式增量发布设置 `allow_supersede_cleanup=False`，不会误删本轮未审文件的旧评论。
- V2-E 默认开启反馈、默认关闭增量，符合渐进上线策略。

## 2. 验证结果

| 门禁 | 结果 |
|---|---|
| V2-E/Service/Storage/Publisher 针对性测试 | PASS（90 项） |
| 全量 pytest | PASS（479 项） |
| Ruff | PASS |
| MyPy strict | PASS（78 source files） |
| V2-E scripted compare | PASS（4/4，global reject=true） |
| V2-A 回归 compare | PASS |
| V2-B 回归 compare | PASS |
| V2-C 回归 compare | PASS |
| V2-D 回归 compare | PASS |

说明：V2-E 对照是脚本化 Fake 验证，证明反馈状态、撤销、代码移动匹配和开关隔离正确，不宣称真实模型质量收益。

## 3. 非阻塞后续项

### P2：feedback revoke 的反馈信息与仓库边界可进一步收紧

当前 CLI 按全局数字 ID 调用 `revoke_feedback(id)`，不存在的 ID 也会输出 `revoked id=...`。在常规“一仓库一个 SQLite 文件”配置下不影响主流程，但如果多个项目共享同一数据库，最好改为：

- 撤销时同时校验 `repo=settings.project.name`；
- 返回是否实际更新；
- ID 不存在或不属于当前 repo 时以非零状态退出。

该项不影响反馈匹配、Pipeline 生命周期、增量 FETCH 或发布安全，因此不阻塞 V2-E。

## 4. 最终判定

**V2-E Round 1：验收通过。**

可以进入下一阶段；上述 P2 可进入统一技术债清单，不建议单独消耗一次 DP/Cursor 上下文返工。
