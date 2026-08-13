# 13 — 竞品学习映射

> 四个参考项目的借鉴点、舍弃点、落入版本；明确"学习什么、不照抄什么"。
> 标注：`V1 now` / `V2 later` / `V3 later` / `optional`。

---

## 1. Codedog — 最小闭环（V1 认知起点）

| 维度 | 内容 |
|------|------|
| 借鉴 | PR 获取 → 内部数据模型 → 文件筛选 → 模型审查 → 报告的主链清晰；适合作为 V1 链路骨架 |
| 舍弃 | 固定字符截断 diff；只看单文件 diff、无跨文件上下文；自由文本 Finding 无严格验证；把固定 Chain 宣称为自主 Agent |
| 落入 | V1：主链结构、数据模型思路（`03` §4 / `05`） |

转化原则：**保留"主链清晰"的工程美德，替换"单文件+截断+自由文本"三个缺陷**。V1 就引入 hunk 级分块与 Finding Schema。

## 2. AI PR Review — V2 质量流水线

| 维度 | 内容 |
|------|------|
| 借鉴 | 条件化多角色（不无脑全跑）；角色异步并发 + semaphore 限流；LLM 与静态分析统一转 Finding；去重/来源合并/交叉印证/轻量 Judge；增量 SHA watermark；发布状态机；误报反馈 |
| 舍弃 | "多角色并行单次调用"被包装成自主 Agent 的说法——本项目明确角色=调用、Agent=执行单元（`07` §2） |
| 落入 | V2：角色 registry/门控/并发（`07` §2-3）、静态融合（`07` §4）、聚合/去重/Judge（`07` §5-6）、watermark 事务（`07` §7）、反馈记忆（`06` §4） |

## 3. PR-Agent — 产品化与抽象

| 维度 | 内容 |
|------|------|
| 借鉴 | Git Provider 与模型 Provider 抽象；大 PR 的 token-aware diff 分块/压缩；Prompt 与配置治理；动态加载仓库规则、语言规则与关联 Issue 上下文；CLI/Action/未来 App 多入口共用核心业务 |
| 舍弃 | 首期不复制其庞大平台兼容层（多 Git 平台、大量产品命令、自部署复杂度） |
| 落入 | Provider 抽象（`03` §6）、token-aware 分块（`06` §2）、配置治理（`09` §6）、多入口复用（`02` §3）、规则动态加载（`06` §1 L4） |

## 4. OpenCodeReview — V3 真正 Agent

| 维度 | 内容 |
|------|------|
| 借鉴 | LLM → tool call → tool result → 再推理 → submit/finish 多轮循环；read_file/read_diff/find_files/search_code/submit_finding/finish_review 受控工具；工具预算/最大轮数/超时/取消/安全点/grace round；会话压缩、覆盖 manifest、行号重新定位、部分成功；模型负责发现、程序负责权限/路径/行号/预算/发布约束 |
| 舍弃 | 不复制全部 Go CLI 规模；只实现评测可证明有价值的核心循环 |
| 落入 | V3 全部：工具清单（`08` §1）、loop（`08` §4）、预算/取消/压缩（`08` §5-6）、终局验证（`08` §7） |

## 5. 综合映射表

| 学习点 | 来源 | 落入版本 | 对应文档 |
|--------|------|----------|----------|
| 主链结构、内部数据模型 | Codedog | V1 | 03/05 |
| hunk/符号级分块（替代截断） | PR-Agent | V1 | 06 |
| 严格 Finding Schema 与验证 | AI PR Review + 自研 | V1 | 05 |
| Provider 抽象 | PR-Agent | V1 | 03 |
| 多入口共用核心 | PR-Agent | V1/v1.0.0 | 02 |
| 条件化多角色+并发限流 | AI PR Review | V2 | 07 |
| 静态分析融合 | AI PR Review | V2 | 07 |
| 去重/来源合并/Judge（keep/downrank） | AI PR Review | V2 | 07 |
| watermark/Saga 发布/反馈 | AI PR Review | V1（Saga 基础）/V2（watermark+反馈） | 07/10 |
| 符号级上下文检索 | OpenCodeReview/PR-Agent | V2（V3 保留最小确定性 L3 + 工具增量） | 06 |
| 工具循环/预算（reserved finalize）/取消/压缩 | OpenCodeReview | V3 | 08 |
| 覆盖 manifest/部分成功 | OpenCodeReview | V1/V3 | 05/08 |
| claimed→canonical 行号重定位 | OpenCodeReview | V1 起 | 05 |
| 模型发现/程序约束 | OpenCodeReview | V3（贯穿） | 05/10 |

## 6. 明确不照抄清单

1. 不把固定 Chain 叫 Agent（Codedog 教训）。
2. 不用固定字符截断（三家都有此缺陷，本项目禁止）。
3. 不复制 PR-Agent 的平台兼容层与命令膨胀。
4. 不复制 OpenCodeReview 的全部 Go 工具面，只做评测证明有价值的 7 个工具。
5. 任何"Agent 化"都必须有评测收益证据，否则停在 V2 形态（`11` §7 对照门槛）。
