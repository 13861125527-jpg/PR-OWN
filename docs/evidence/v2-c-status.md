# V2-C 状态说明

> **V2-C 里程碑状态：ACCEPTED**（用户授权进入 V2-D；Round 1 B1/B2 已关）
>
> 对齐稿：`docs/architecture/20-v2c-alignment.md`（含 Codex R1–R6）
> 对照报告：`docs/evidence/v2-c-compare.md` / `.json`

## 本轮实现

| 卡 | 落点 |
|---|---|
| T1 | `FindingCandidate.source_kind/rule_id/analyzer_id`；`ReviewTaskKind.STATIC_ANALYZE`；`review.static`；`sanitize_llm_candidate` / Strategy 边界清洗；LLM schema 不含这三字段 |
| T2 | `reposage/review/static/`：协议、Fake、转换纯函数 |
| T3 | Pipeline 来源盖戳、STATIC_RESULT 重判、fingerprint 用 `rule_id`、LLM+静态融合（并列保留 LLM 文案，fingerprint 用静态 rule_id） |
| T4 | `RuffAnalyzer`：`--isolated`、临时目录保持仓库相对路径、exit 0/1 成功、≥2 或非法 JSON 失败、超时/取消杀进程 |
| T5 | ReviewService REVIEW 与 `execute` 并行；`enabled=false` 不创建静态任务/协程；无 snapshot → capability miss + truncated；未知 analyzer 是配置错误 |
| T6 | `v2c_static.yaml` + `v2c_compare`：转换 3/3、去重/融合含 B2 负例；V2-A/B 钉死 `static.enabled=false` |
| T7 | pytest / Ruff / mypy strict 全绿 |

## Round 1 Blocking 修复

| 项 | 落点 |
|---|---|
| B1 | 空 stdout 不再当成 `[]`；exit 1 无合法 JSON → failed；stderr 含 `No module named` → `capability miss: ruff` |
| B2 | `_clusters_fuseable` 若两侧静态 `rule_id` 并集超过 1 个则拒绝融合；Pipeline + `no-fuse-two-static-via-llm` 评测 |

## 对照（程序指标）

转换 passed=3/3；去重/融合 passed=5/5。不宣称真实模型质量收益。静态分析不占 LLM 预算。

## 明确非目标

- Judge / Agent / bandit
- 把 ruff 输出喂给模型
- GitHub blob API
- 默认打开 `static.enabled`（仍为 False）

## 门禁

pytest / Ruff / mypy strict 全绿。未提交。
